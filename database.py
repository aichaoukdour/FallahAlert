import os
import logging
import time
import psycopg2
import psycopg2.extras
import psycopg2.pool
from contextlib import contextmanager
from datetime import datetime, timezone

logger = logging.getLogger("fellahalert.db")

DATABASE_URL = os.getenv("DATABASE_URL")
_pool: psycopg2.pool.ThreadedConnectionPool | None = None

# ── Blocklist cache (O(1) lookups, refreshed every 30s) ──────────────────────
_block_cache: set[str] = set()
_block_cache_ts: float = 0.0
_BLOCK_TTL = 30.0


def init_pool():
    global _pool
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set — provision a PostgreSQL database first.")
    _pool = psycopg2.pool.ThreadedConnectionPool(minconn=1, maxconn=8, dsn=DATABASE_URL)
    logger.info("DB pool ready (min=1 max=8)")


@contextmanager
def _conn():
    """Borrow a connection from the pool, commit on success, rollback on error."""
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call init_pool() first.")
    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


def init_db(retries: int = 5, delay: float = 3.0):
    """
    Initialise DB pool and run all schema migrations.

    Retries up to `retries` times with `delay` seconds between attempts.
    This handles Render's cold-start race where the DB container isn't
    ready the instant the web service starts.
    """
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            init_pool()
            break
        except Exception as e:
            last_err = e
            logger.warning("DB connection attempt %d/%d failed: %s", attempt, retries, e)
            if attempt < retries:
                time.sleep(delay)
    else:
        raise RuntimeError(f"Could not connect to DB after {retries} attempts: {last_err}")

    with _conn() as conn:
        with conn.cursor() as cur:
            # ── Core interactions table ─────────────────────────────────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS interactions (
                    id           SERIAL PRIMARY KEY,
                    phone        TEXT,
                    crop         TEXT,
                    city         TEXT,
                    lang         TEXT DEFAULT 'fr',
                    user_message TEXT,
                    ai_response  TEXT,
                    nasa_ok      BOOLEAN,
                    created_at   TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            # ── Additive migrations (idempotent) ────────────────────────────
            cur.execute("""
                ALTER TABLE interactions
                    ADD COLUMN IF NOT EXISTS problem_type TEXT DEFAULT 'général',
                    ADD COLUMN IF NOT EXISTS urgency      TEXT DEFAULT 'normale',
                    ADD COLUMN IF NOT EXISTS rag_used     BOOLEAN DEFAULT FALSE
            """)
            # ── Blocklist ───────────────────────────────────────────────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS blocked_phones (
                    id         SERIAL PRIMARY KEY,
                    phone      TEXT UNIQUE NOT NULL,
                    reason     TEXT,
                    blocked_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
    logger.info("Database schema ready")


# ── Interactions ──────────────────────────────────────────────────────────────
def log_interaction(phone, crop, city, user_message, ai_response, nasa_ok, lang="fr",
                    problem_type="général", urgency="normale", rag_used=False):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO interactions
                    (phone, crop, city, lang, user_message, ai_response, nasa_ok,
                     problem_type, urgency, rag_used, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (phone, crop, city, lang, user_message, ai_response, nasa_ok,
                 problem_type, urgency, rag_used, datetime.now(timezone.utc)),
            )


def get_stats():
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM interactions")
            total = cur.fetchone()["cnt"]
            cur.execute("SELECT COUNT(*) AS cnt FROM blocked_phones")
            blocked_count = cur.fetchone()["cnt"]
            cur.execute(
                """
                SELECT phone, crop, city, lang, ai_response, created_at
                FROM interactions
                ORDER BY id DESC
                LIMIT 10
                """
            )
            recent = [dict(r) for r in cur.fetchall()]

    for row in recent:
        if row.get("created_at"):
            row["created_at"] = row["created_at"].isoformat()

    return {"total": total, "blocked_count": blocked_count, "recent": recent}


# ── Blocklist ─────────────────────────────────────────────────────────────────
def _refresh_block_cache(force: bool = False):
    global _block_cache, _block_cache_ts
    now = time.time()
    if not force and now - _block_cache_ts < _BLOCK_TTL:
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT phone FROM blocked_phones")
            _block_cache = {row[0] for row in cur.fetchall()}
    _block_cache_ts = now


def is_blocked(phone: str) -> bool:
    _refresh_block_cache()
    return phone.strip() in _block_cache


def block_phone(phone: str, reason: str | None = None):
    phone = phone.strip()
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO blocked_phones (phone, reason)
                VALUES (%s, %s)
                ON CONFLICT (phone) DO UPDATE SET reason = EXCLUDED.reason,
                                                  blocked_at = NOW()
                """,
                (phone, reason),
            )
    _refresh_block_cache(force=True)
    logger.info("Phone blocked: ...%s | reason: %s", phone[-4:], reason or "—")


def unblock_phone(phone: str) -> bool:
    phone = phone.strip()
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM blocked_phones WHERE phone = %s", (phone,))
            deleted = cur.rowcount > 0
    _refresh_block_cache(force=True)
    if deleted:
        logger.info("Phone unblocked: ...%s", phone[-4:])
    return deleted


def get_interactions_for_export() -> list[dict]:
    """Return all interactions as plain dicts, safe for CSV export (phone masked)."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    created_at AT TIME ZONE 'UTC' AS date_utc,
                    CONCAT('...', RIGHT(phone, 4))  AS telephone,
                    crop   AS culture,
                    city   AS ville,
                    lang   AS langue,
                    CASE WHEN nasa_ok THEN 'oui' ELSE 'non' END AS nasa_ok
                FROM interactions
                ORDER BY created_at DESC
            """)
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_analytics() -> dict:
    from datetime import date, timedelta
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '24 hours') AS last_24h,
                    COUNT(*) FILTER (WHERE nasa_ok = TRUE) AS nasa_ok_count,
                    COUNT(*) FILTER (WHERE lang = 'ar') AS arabic_count,
                    COUNT(*) FILTER (WHERE lang = 'fr') AS french_count
                FROM interactions
            """)
            totals = dict(cur.fetchone())

            cur.execute("""
                SELECT DATE(created_at AT TIME ZONE 'UTC') AS day, COUNT(*) AS cnt
                FROM interactions
                WHERE created_at >= NOW() - INTERVAL '30 days'
                GROUP BY day ORDER BY day
            """)
            daily_raw = {str(r["day"]): r["cnt"] for r in cur.fetchall()}

            cur.execute("""
                SELECT COALESCE(lang, 'fr') AS lang, COUNT(*) AS cnt
                FROM interactions
                GROUP BY COALESCE(lang, 'fr') ORDER BY cnt DESC
            """)
            by_lang = [dict(r) for r in cur.fetchall()]

            cur.execute("""
                SELECT LOWER(TRIM(crop)) AS crop, COUNT(*) AS cnt
                FROM interactions
                WHERE crop IS NOT NULL AND TRIM(crop) != ''
                GROUP BY LOWER(TRIM(crop)) ORDER BY cnt DESC LIMIT 10
            """)
            top_crops = [dict(r) for r in cur.fetchall()]

            cur.execute("""
                SELECT INITCAP(city) AS city, COUNT(*) AS cnt
                FROM interactions
                WHERE city IS NOT NULL AND TRIM(city) != ''
                GROUP BY INITCAP(city) ORDER BY cnt DESC LIMIT 10
            """)
            top_cities = [dict(r) for r in cur.fetchall()]

            cur.execute("""
                SELECT lang, DATE_TRUNC('day', created_at AT TIME ZONE 'UTC') AS day,
                       COUNT(*) AS cnt
                FROM interactions
                WHERE created_at >= NOW() - INTERVAL '30 days'
                GROUP BY lang, day ORDER BY day
            """)
            lang_daily_raw = cur.fetchall()

    today = date.today()
    daily = []
    for i in range(29, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        daily.append({"day": d, "cnt": daily_raw.get(d, 0)})

    total = totals["total"] or 0
    return {
        "total": total,
        "last_24h": totals["last_24h"],
        "nasa_success_rate": round(totals["nasa_ok_count"] / total * 100) if total else 0,
        "arabic_pct": round(totals["arabic_count"] / total * 100) if total else 0,
        "french_pct": round(totals["french_count"] / total * 100) if total else 0,
        "daily": daily,
        "by_lang": by_lang,
        "top_crops": top_crops,
        "top_cities": top_cities,
    }


def get_blocked_phones() -> list[dict]:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT phone, reason, blocked_at FROM blocked_phones ORDER BY blocked_at DESC"
            )
            rows = [dict(r) for r in cur.fetchall()]
    for row in rows:
        if row.get("blocked_at"):
            row["blocked_at"] = row["blocked_at"].isoformat()
        # Mask all but last 4 digits for privacy
        row["phone_masked"] = "..." + row["phone"][-4:] if len(row["phone"]) > 4 else "****"
    return rows
