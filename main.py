import logging
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator

from cities import get_coords
from nasa import get_weather_summary
from ai import get_advice, check_gemini_status, detect_language, diagnose_plant
from database import (
    init_db, log_interaction, get_stats, get_analytics,
    get_interactions_for_export,
    is_blocked, block_phone, unblock_phone, get_blocked_phones,
)
from crop_calendar import get_calendar
from parser import parse_message as structured_parse
from rag import init_rag, retrieve_context

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("fellahalert")

# ── Rate limiting (in-memory, per IP) ────────────────────────────────────────
_rl: dict[str, list[float]] = defaultdict(list)
_RL_WINDOW = 60
_RL_MAX = 15


def _is_rate_limited(ip: str) -> bool:
    now = time.time()
    cutoff = now - _RL_WINDOW
    _rl[ip] = [t for t in _rl[ip] if t > cutoff]
    if len(_rl[ip]) >= _RL_MAX:
        return True
    _rl[ip].append(now)
    return False


# ── Status cache ──────────────────────────────────────────────────────────────
_status_cache: dict = {"data": None, "ts": 0.0}
_STATUS_TTL = 60


# ── Admin auth ────────────────────────────────────────────────────────────────
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")


def require_admin(request: Request):
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="Admin not configured — set ADMIN_TOKEN secret.")
    token = request.headers.get("X-Admin-Token") or request.query_params.get("token")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid admin token.")


# ── App lifecycle ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting FellahAlert — initialising database…")
    init_db()
    logger.info("Initialising RAG knowledge base…")
    try:
        init_rag()
    except Exception as e:
        logger.warning("RAG init failed (non-fatal): %s", e)
    logger.info("Startup complete")
    yield
    logger.info("Shutting down FellahAlert")


app = FastAPI(title="FellahAlert", lifespan=lifespan)

BASE_DIR = os.path.dirname(__file__)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


# ── Request models ────────────────────────────────────────────────────────────
class AskRequest(BaseModel):
    phone: str
    message: str

    @field_validator("phone")
    @classmethod
    def sanitise_phone(cls, v: str) -> str:
        return v.strip()[:20]

    @field_validator("message")
    @classmethod
    def validate_message(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 2:
            raise ValueError("Message trop court")
        if len(v) > 500:
            raise ValueError("Message trop long (max 500 caractères)")
        return v


class BlockRequest(BaseModel):
    phone: str
    reason: Optional[str] = None

    @field_validator("phone")
    @classmethod
    def sanitise_phone(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Numéro de téléphone requis")
        return v[:30]


class DiagnoseRequest(BaseModel):
    image_url: str
    lang: Optional[str] = "fr"

    @field_validator("image_url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL invalide")
        if len(v) > 2000:
            raise ValueError("URL trop longue")
        return v


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    cal = get_calendar("meknès")
    return templates.TemplateResponse(request=request, name="index.html", context={"cal": cal})


@app.post("/ask")
async def ask(request: Request, body: AskRequest):
    client_ip = request.client.host if request.client else "unknown"

    if _is_rate_limited(client_ip):
        logger.warning("Rate limit hit for IP %s", client_ip)
        return JSONResponse(
            status_code=429,
            content={"error": "Trop de requêtes — réessayez dans une minute."},
        )

    if is_blocked(body.phone):
        logger.warning("Blocked phone attempted access: ...%s", body.phone[-4:])
        return JSONResponse(
            status_code=403,
            content={"error": "Accès refusé."},
        )

    try:
        # ── 1. Structured parse (Gemini JSON mode) ────────────────────────
        parsed = structured_parse(body.message)
        lang = parsed.language
        crop = parsed.crop
        problem_type = parsed.problem_type
        urgency = parsed.urgency

        coords, city_name, used_default = get_coords(parsed.city)

        # ── 2. Weather data (NASA POWER) ──────────────────────────────────
        weather = get_weather_summary(coords[0], coords[1])
        nasa_ok = weather is not None

        # ── 3. RAG context retrieval ──────────────────────────────────────
        rag_context = ""
        rag_used = False
        try:
            rag_context = retrieve_context(crop, problem_type)
            rag_used = bool(rag_context)
        except Exception as rag_err:
            logger.warning("RAG retrieval skipped: %s", rag_err)

        # ── 4. AI advice (grounded with RAG) ─────────────────────────────
        try:
            advice = get_advice(
                crop, city_name, weather,
                lang=lang,
                problem_type=problem_type,
                rag_context=rag_context,
            )
        except Exception as e:
            logger.error("Gemini error for crop=%s city=%s: %s", crop, city_name, e)
            return JSONResponse(
                status_code=503,
                content={"error": "Service IA temporairement indisponible. Réessayez plus tard."},
            )

        # ── 5. Log ────────────────────────────────────────────────────────
        log_interaction(
            phone=body.phone,
            crop=crop,
            city=city_name,
            user_message=body.message,
            ai_response=advice,
            nasa_ok=nasa_ok,
            lang=lang,
            problem_type=problem_type,
            urgency=urgency,
            rag_used=rag_used,
        )

        logger.info(
            "Advice served — crop=%s city=%s lang=%s problem=%s urgency=%s rag=%s",
            crop, city_name, lang, problem_type, urgency, rag_used,
        )

        return {
            "response": advice,
            "crop": crop,
            "city": city_name,
            "lang": lang,
            "problem_type": problem_type,
            "urgency": urgency,
            "rag_used": rag_used,
            "used_default_city": used_default,
            "nasa_data": weather,
        }
    except Exception as e:
        logger.error("Unhandled error in /ask: %s", e)
        return JSONResponse(
            status_code=500,
            content={"error": "Erreur interne. Réessayez plus tard."},
        )


@app.post("/diagnose")
async def diagnose(request: Request, body: DiagnoseRequest):
    client_ip = request.client.host if request.client else "unknown"
    if _is_rate_limited(client_ip):
        return JSONResponse(status_code=429, content={"error": "Trop de requêtes."})
    try:
        result = diagnose_plant(body.image_url, lang=body.lang or "fr")
        logger.info("Plant diagnosis — healthy=%s disease=%s", result.get("healthy"), result.get("disease"))
        return result
    except Exception as e:
        logger.error("Diagnose error: %s", e)
        return JSONResponse(status_code=503, content={"error": "Diagnostic IA indisponible. Réessayez plus tard."})


@app.get("/stats")
async def stats():
    return get_stats()


@app.get("/calendar")
async def crop_calendar(city: str = "meknès", month: Optional[int] = None):
    return get_calendar(city, month)


@app.get("/analytics/export")
async def analytics_export():
    import csv
    import io
    from datetime import datetime, timezone

    rows = get_interactions_for_export()

    def generate():
        buf = io.StringIO()
        writer = csv.DictWriter(
            buf,
            fieldnames=["date_utc", "telephone", "culture", "ville", "langue", "nasa_ok"],
            extrasaction="ignore",
        )
        writer.writeheader()
        yield buf.getvalue()
        for row in rows:
            buf = io.StringIO()
            writer = csv.DictWriter(
                buf,
                fieldnames=["date_utc", "telephone", "culture", "ville", "langue", "nasa_ok"],
                extrasaction="ignore",
            )
            row["date_utc"] = str(row["date_utc"])
            writer.writerow(row)
            yield buf.getvalue()

    filename = f"fellahalert_export_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.csv"
    return StreamingResponse(
        generate(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    return templates.TemplateResponse(request=request, name="analytics.html")


@app.get("/analytics/data")
async def analytics_data():
    return get_analytics()


@app.get("/status")
async def status():
    now = time.time()
    if now - _status_cache["ts"] < _STATUS_TTL and _status_cache["data"] is not None:
        return _status_cache["data"]
    nasa_ok = get_weather_summary(33.8935, -5.5473) is not None
    gemini_ok = check_gemini_status()
    result = {"nasa": nasa_ok, "gemini": gemini_ok}
    _status_cache["data"] = result
    _status_cache["ts"] = now
    return result


@app.get("/ping")
async def ping():
    """Ultra-fast keep-alive for UptimeRobot — no DB or AI calls."""
    return {"pong": True}


@app.get("/health")
async def health():
    """
    Lightweight health-check for uptime monitors and Render's health-check config.
    Checks DB connectivity, Gemini AI, and NASA POWER.
    Returns HTTP 200 when all critical services are up, 503 otherwise.
    """
    import importlib
    checks: dict[str, bool] = {}

    # DB check
    try:
        from database import get_stats
        get_stats()
        checks["db"] = True
    except Exception as e:
        logger.warning("Health check — DB failed: %s", e)
        checks["db"] = False

    # Gemini check (cached, at most one real call per minute)
    try:
        checks["gemini"] = check_gemini_status()
    except Exception:
        checks["gemini"] = False

    # NASA check (cached via /status)
    try:
        checks["nasa"] = get_weather_summary(33.8935, -5.5473) is not None
    except Exception:
        checks["nasa"] = False

    all_ok = checks["db"] and checks["gemini"]   # NASA is non-critical
    status_code = 200 if all_ok else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ok" if all_ok else "degraded",
            "checks": checks,
            "version": "1.0.0",
        },
    )


# ── Admin — blocklist management ──────────────────────────────────────────────
@app.post("/admin/block", dependencies=[Depends(require_admin)])
async def admin_block(data: BlockRequest):
    block_phone(data.phone, data.reason)
    return {"status": "blocked", "phone": data.phone, "reason": data.reason}


@app.delete("/admin/block/{phone}", dependencies=[Depends(require_admin)])
async def admin_unblock(phone: str):
    deleted = unblock_phone(phone)
    if not deleted:
        raise HTTPException(status_code=404, detail="Phone not found in blocklist.")
    return {"status": "unblocked", "phone": phone}


@app.get("/admin/blocked", dependencies=[Depends(require_admin)])
async def admin_list_blocked():
    return {"blocked": get_blocked_phones()}


# ── WhatsApp webhook ──────────────────────────────────────────────────────────
_WA_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
_WA_ENABLED = bool(
    os.getenv("WHATSAPP_ACCESS_TOKEN")
    and os.getenv("WHATSAPP_PHONE_ID")
    and _WA_VERIFY_TOKEN
)
_wa_logger = logging.getLogger("fellahalert.whatsapp")


@app.get("/whatsapp")
async def whatsapp_verify(request: Request):
    """Meta webhook verification handshake."""
    params = dict(request.query_params)
    if params.get("hub.verify_token") == _WA_VERIFY_TOKEN and _WA_VERIFY_TOKEN:
        _wa_logger.info("WhatsApp webhook verified successfully")
        return int(params.get("hub.challenge", 0))
    _wa_logger.warning("WhatsApp webhook verification failed")
    return JSONResponse(status_code=403, content={"error": "Forbidden"})


@app.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    """Receive and process incoming WhatsApp messages (text + images)."""
    if not _WA_ENABLED:
        return JSONResponse(
            status_code=503,
            content={"error": "WhatsApp not configured — set WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_ID, WHATSAPP_VERIFY_TOKEN secrets."},
        )
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    from whatsapp import process_webhook
    status = process_webhook(payload)
    _wa_logger.info("Webhook processed: %s", status)
    return {"status": status}


@app.get("/whatsapp/status")
async def whatsapp_status():
    return {
        "enabled": _WA_ENABLED,
        "webhook_url": "/whatsapp",
        "phone_id_set": bool(os.getenv("WHATSAPP_PHONE_ID")),
        "access_token_set": bool(os.getenv("WHATSAPP_ACCESS_TOKEN")),
        "verify_token_set": bool(_WA_VERIFY_TOKEN),
        "supports": ["text", "image"],
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 24258))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
