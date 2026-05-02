# Deploy FellahAlert on Render.com (Free)

## Prerequisites
- GitHub account
- Render.com account (free)
- Google AI Studio API key (free) → https://aistudio.google.com/app/apikey

---

## Step 1 — Get a free Gemini API key

1. Go to https://aistudio.google.com/app/apikey
2. Click **Create API key**
3. Copy the key — you'll paste it as `GEMINI_API_KEY` in Render

---

## Step 2 — Push code to GitHub

From Replit, download your project (⋯ → Download as zip), then:

```bash
cd artifacts/fellahalert   # this folder
git init
git add .
git commit -m "FellahAlert initial deploy"
gh repo create fellahalert --public --push
```

Or just drag-and-drop the folder contents into a new GitHub repo.

---

## Step 3 — Create a free PostgreSQL database on Render

1. Go to https://dashboard.render.com → **New → PostgreSQL**
2. Name: `fellahalert-db`
3. Plan: **Free**
4. Click **Create Database**
5. Copy the **Internal Database URL** (starts with `postgresql://`)

---

## Step 4 — Deploy the web service

1. Go to https://dashboard.render.com → **New → Web Service**
2. Connect your GitHub repo
3. Settings:
   - **Name**: `fellahalert`
   - **Root directory**: *(leave blank if you pushed the fellahalert folder contents directly)*
   - **Runtime**: Python 3
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `python main.py`
   - **Plan**: Free

---

## Step 5 — Add environment variables

In the Render web service → **Environment** tab, add:

| Key | Value |
|-----|-------|
| `DATABASE_URL` | Internal DB URL from Step 3 |
| `GEMINI_API_KEY` | Your Google AI Studio key |
| `WHATSAPP_PHONE_ID` | `1083605124836928` |
| `WHATSAPP_ACCESS_TOKEN` | Your Meta access token |
| `WHATSAPP_VERIFY_TOKEN` | Your chosen verify token |
| `ADMIN_TOKEN` | Any strong secret string |
| `SESSION_SECRET` | Any strong secret string |

---

## Step 6 — Configure WhatsApp webhook

Once deployed, your stable URL will be:
```
https://fellahalert.onrender.com/whatsapp
```

In Meta Developer Console → WhatsApp → Configuration → Webhooks:
- **Callback URL**: `https://fellahalert.onrender.com/whatsapp`
- **Verify token**: your `WHATSAPP_VERIFY_TOKEN` value
- Click **Verify and save**
- Subscribe to: **messages**

---

## Notes

- Free Render services spin down after 15 min of inactivity (cold start ~30s).
- For always-on, upgrade to Render's Starter plan ($7/mo) or use a free uptime monitor
  like https://uptimerobot.com to ping `/status` every 5 minutes.
- Free PostgreSQL on Render expires after **90 days** — export your data before then.
