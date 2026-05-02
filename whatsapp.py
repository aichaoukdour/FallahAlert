"""
WhatsApp Business API webhook handler for FellahAlert.

Handles:
  - Webhook verification (GET)
  - Incoming text messages → structured parse + RAG + AI advice
  - Incoming image messages → Gemini Vision crop disease diagnosis
  - Outgoing replies via Cloud API v20.0
"""
import logging
import os
import io

import requests as _req

from ai import get_advice, diagnose_plant
from database import log_interaction, is_blocked
from cities import get_coords
from parser import parse_message as structured_parse
from rag import retrieve_context

logger = logging.getLogger("fellahalert.whatsapp")

GRAPH_URL = "https://graph.facebook.com/v20.0"


def _access_token() -> str:
    return os.getenv("WHATSAPP_ACCESS_TOKEN", "")


def _phone_id() -> str:
    return os.getenv("WHATSAPP_PHONE_ID", "")


def _verify_token() -> str:
    return os.getenv("WHATSAPP_VERIFY_TOKEN", "")


# ── Outgoing ──────────────────────────────────────────────────────────────────

def send_text(to: str, body: str) -> bool:
    """Send a plain text WhatsApp message. Returns True on success."""
    token = _access_token()
    phone_id = _phone_id()
    if not token or not phone_id:
        logger.error("WhatsApp credentials not configured")
        return False
    try:
        r = _req.post(
            f"{GRAPH_URL}/{phone_id}/messages",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": body[:4000]},
            },
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        logger.error("send_text to %s failed: %s", to[-4:], e)
        return False


def _download_media(media_id: str) -> tuple[bytes, str]:
    """
    Download a WhatsApp media object.
    Returns (image_bytes, mime_type).
    """
    token = _access_token()
    # Step 1: get the download URL
    meta = _req.get(
        f"{GRAPH_URL}/{media_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    meta.raise_for_status()
    info = meta.json()
    url = info["url"]
    mime_type = info.get("mime_type", "image/jpeg")

    # Step 2: download actual bytes
    data = _req.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    data.raise_for_status()
    return data.content, mime_type


# ── Incoming message processing ───────────────────────────────────────────────

def _handle_text(phone: str, text: str) -> str:
    """Process a text message and return the reply string."""
    parsed = structured_parse(text)
    lang = parsed.language
    crop = parsed.crop
    problem_type = parsed.problem_type
    urgency = parsed.urgency

    coords, city_name, _ = get_coords(parsed.city)
    from nasa import get_weather_summary
    weather = get_weather_summary(coords[0], coords[1])

    rag_context = ""
    rag_used = False
    try:
        rag_context = retrieve_context(crop, problem_type)
        rag_used = bool(rag_context)
    except Exception as e:
        logger.warning("RAG skipped: %s", e)

    advice = get_advice(
        crop, city_name, weather,
        lang=lang,
        problem_type=problem_type,
        rag_context=rag_context,
    )

    log_interaction(
        phone=phone,
        crop=crop,
        city=city_name,
        user_message=text,
        ai_response=advice,
        nasa_ok=weather is not None,
        lang=lang,
        problem_type=problem_type,
        urgency=urgency,
        rag_used=rag_used,
    )

    logger.info(
        "WA text handled — ...%s crop=%s city=%s lang=%s problem=%s rag=%s",
        phone[-4:], crop, city_name, lang, problem_type, rag_used,
    )
    return advice


def _handle_image(phone: str, media_id: str, caption: str = "") -> str:
    """Download image from WhatsApp and run Gemini Vision diagnosis."""
    # Detect language from caption if provided
    from ai import detect_language
    lang = detect_language(caption) if caption else "fr"

    try:
        image_bytes, mime_type = _download_media(media_id)
    except Exception as e:
        logger.error("Media download failed for %s: %s", media_id, e)
        return (
            "Impossible de télécharger votre image. Réessayez ou envoyez l'URL de la photo."
            if lang == "fr"
            else "تعذّر تحميل الصورة. أعد المحاولة أو أرسل رابط الصورة."
        )

    try:
        result = diagnose_plant(
            image_bytes=image_bytes,
            mime_type=mime_type,
            lang=lang,
        )
    except Exception as e:
        logger.error("Vision diagnosis failed: %s", e)
        return (
            "Diagnostic impossible pour cette image. Assurez-vous que c'est une photo de plante."
            if lang == "fr"
            else "لا يمكن تشخيص هذه الصورة. تأكد أنها صورة نبات."
        )

    log_interaction(
        phone=phone,
        crop=result.get("disease", "vision"),
        city="—",
        user_message=f"[IMAGE] {caption}" if caption else "[IMAGE]",
        ai_response=str(result),
        nasa_ok=False,
        lang=lang,
        problem_type="maladie",
        urgency=result.get("urgency", "normale"),
        rag_used=False,
    )

    logger.info("WA image handled — ...%s disease=%s healthy=%s", phone[-4:], result.get("disease"), result.get("healthy"))

    if lang == "fr":
        if result.get("healthy"):
            return (
                f"✅ Plante saine\n\n"
                f"Confiance : {result.get('confidence','?')}\n"
                f"Observations : {result.get('symptoms','—')}"
            )
        return (
            f"🔴 Diagnostic : {result.get('disease','Non identifié')}\n\n"
            f"Confiance : {result.get('confidence','?')}\n"
            f"Symptômes : {result.get('symptoms','—')}\n"
            f"Traitement : {result.get('treatment','Consultez un agronome')}\n"
            f"Urgence : {result.get('urgency','?')}"
        )
    else:
        if result.get("healthy"):
            return (
                f"✅ النبات بصحة جيدة\n\n"
                f"الثقة : {result.get('confidence','?')}\n"
                f"الملاحظات : {result.get('symptoms','—')}"
            )
        return (
            f"🔴 التشخيص : {result.get('disease','غير محدد')}\n\n"
            f"الثقة : {result.get('confidence','?')}\n"
            f"الأعراض : {result.get('symptoms','—')}\n"
            f"العلاج : {result.get('treatment','استشر مهندسًا زراعيًا')}\n"
            f"الاستعجال : {result.get('urgency','?')}"
        )


def process_webhook(payload: dict) -> str:
    """
    Process an incoming WhatsApp webhook payload.
    Dispatches to text or image handler. Returns status string.
    """
    try:
        entry = payload["entry"][0]["changes"][0]["value"]
    except (KeyError, IndexError):
        return "no_entry"

    if "messages" not in entry:
        return "ignored"

    msg = entry["messages"][0]
    phone: str = msg.get("from", "")
    msg_type: str = msg.get("type", "")

    if is_blocked(phone):
        logger.warning("Blocked phone via WhatsApp: ...%s", phone[-4:])
        return "blocked"

    reply: str | None = None

    if msg_type == "text":
        text = msg.get("text", {}).get("body", "").strip()[:500]
        if not text:
            return "empty"
        try:
            reply = _handle_text(phone, text)
        except Exception as e:
            logger.error("Text handler error for ...%s: %s", phone[-4:], e)
            reply = "Service temporairement indisponible. Réessayez plus tard."

    elif msg_type == "image":
        media_id = msg.get("image", {}).get("id", "")
        caption = msg.get("image", {}).get("caption", "")
        if not media_id:
            return "no_media_id"
        try:
            reply = _handle_image(phone, media_id, caption)
        except Exception as e:
            logger.error("Image handler error for ...%s: %s", phone[-4:], e)
            reply = "Erreur lors du diagnostic. Réessayez avec une autre photo."

    else:
        logger.info("Unsupported WA message type: %s from ...%s", msg_type, phone[-4:])
        return f"unsupported_type:{msg_type}"

    if reply:
        send_text(phone, reply)

    return "ok"
