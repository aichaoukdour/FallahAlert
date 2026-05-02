# Deploy FellahAlert — Free Hosting Guide

## Option 1 — Railway (Recommended, Free)

Railway gives $5/month free credit — enough for a small app + PostgreSQL database.
No credit card required to start. No sleep on free tier.

### Step 1 — Create account
Go to [railway.app](https://railway.app) → **Sign up with GitHub**

### Step 2 — New project from GitHub
1. Dashboard → **New Project → Deploy from GitHub repo**
2. Select **aichaoukdour/FallahAlert**
3. Railway auto-detects Python and reads `railway.json`

### Step 3 — Add PostgreSQL
1. In your project → **New → Database → PostgreSQL**
2. Railway automatically injects `DATABASE_URL` into your app — no manual copy needed

### Step 4 — Add environment variables
Go to your service → **Variables** tab → add:

| Key | Value |
|-----|-------|
| `GEMINI_API_KEY` | Your Google AI Studio key → https://aistudio.google.com/app/apikey |
| `WHATSAPP_PHONE_ID` | `1083605124836928` |
| `WHATSAPP_ACCESS_TOKEN` | Your Meta access token |
| `WHATSAPP_VERIFY_TOKEN` | Any secret word (e.g. `fellahalert2024`) |
| `ADMIN_TOKEN` | Any strong password |
| `SESSION_SECRET` | Any random string |

### Step 5 — Deploy
Click **Deploy** — Railway builds and starts the app.
Your URL will be something like `https://fallahalert-production.up.railway.app`

### Step 6 — WhatsApp webhook
In Meta Developer Console → WhatsApp → Webhooks:
- **Callback URL**: `https://your-app.up.railway.app/whatsapp`
- **Verify token**: your `WHATSAPP_VERIFY_TOKEN`
- Subscribe to: **messages**

### Auto-deploy
Railway auto-deploys every time you push to the `main` branch — no extra setup needed.

---

## Option 2 — Koyeb (Free, no credit card)

### Step 1
Go to [koyeb.com](https://koyeb.com) → **Sign up**

### Step 2
1. **Create App → GitHub**
2. Select `aichaoukdour/FallahAlert`
3. **Builder**: Dockerfile
4. **Port**: 8000

### Step 3 — Free PostgreSQL (Neon)
1. Go to [neon.tech](https://neon.tech) → **Sign up** (free, no card)
2. Create a project → copy the **Connection string**
3. Paste it as `DATABASE_URL` in Koyeb environment variables

### Step 4 — Environment variables
Same as Railway table above.

---

## Option 3 — Google Cloud Run (Free tier: 2M requests/month)

Requires a Google account. Free tier is very generous but has ~2s cold starts.

```bash
# Install Google Cloud CLI, then:
gcloud run deploy fellahalert \
  --source . \
  --platform managed \
  --region europe-west1 \
  --allow-unauthenticated \
  --set-env-vars GEMINI_API_KEY=xxx,DATABASE_URL=xxx,...
```

Use **Neon** (neon.tech) for the free PostgreSQL database.

---

## Free PostgreSQL options (if needed separately)

| Service | Free Storage | Notes |
|---------|-------------|-------|
| **Neon** | 0.5 GB | Best option, serverless, fast |
| **Supabase** | 500 MB | Pauses after 1 week inactive (free tier) |
| **Railway** | Included | Only with Railway hosting |

---

## Keep-alive tip (Railway / Koyeb)

Both Railway and Koyeb don't sleep on the free tier, but if you move to another platform that does, add `https://your-app/ping` to [UptimeRobot](https://uptimerobot.com) (free) — pings every 5 min.
