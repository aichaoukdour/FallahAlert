import os
import re
import time
import logging
from google import genai

logger = logging.getLogger("fellahalert.ai")

_client = None

_status_cache: dict = {"ok": None, "ts": 0.0}
_STATUS_TTL = 60


def get_client():
    """
    Build a Gemini client.

    Priority:
      1. Replit AI proxy  (AI_INTEGRATIONS_GEMINI_BASE_URL set)  — used when running on Replit
      2. Direct Google API (GEMINI_API_KEY set)                   — used on Render / any other host
    """
    global _client
    if _client is None:
        replit_base_url = os.getenv("AI_INTEGRATIONS_GEMINI_BASE_URL")
        replit_api_key  = os.getenv("AI_INTEGRATIONS_GEMINI_API_KEY")
        gemini_api_key  = os.getenv("GEMINI_API_KEY")

        if replit_base_url and replit_api_key:
            # Running on Replit — use the managed proxy
            _client = genai.Client(
                api_key=replit_api_key,
                http_options={
                    "base_url": replit_base_url,
                    "api_version": "",
                },
            )
            logger.info("Gemini client: Replit AI proxy")
        elif gemini_api_key:
            # Running elsewhere — use the direct Google API
            _client = genai.Client(api_key=gemini_api_key)
            logger.info("Gemini client: Google AI direct (GEMINI_API_KEY)")
        else:
            raise RuntimeError(
                "No Gemini credentials found. "
                "Set GEMINI_API_KEY (Google AI Studio) or the Replit AI proxy vars."
            )
    return _client


def detect_language(text: str) -> str:
    """Detect Arabic/Darija or French from text."""
    arabic_chars = len(re.findall(r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]', text))
    total_chars = len(text.replace(" ", "").replace(",", ""))
    if total_chars == 0:
        return "fr"
    if arabic_chars / total_chars > 0.3:
        return "ar"
    french_kw = [
        "blé","ble","tomate","orge","mais","maïs","olive","agrume","courgette",
        "poivron","oignon","ail","pomme","terre","lentille","pois","chiche",
        "fève","feve","tournesol","betterave","melon","pastèque","pasteque",
        "vigne","amandier","figuier","grenadier","région","region","ville",
        "cette","semaine","agriculteur","conseil","culture","champ",
        "irrigation","maladie","traitement",
    ]
    darija_kw = [
        "قمح","شعير","طماطم","زيتون","برتقال","خضرة","فلاح",
        "مزرعة","ماء","تقاوي","بذور","سماد","حشرات","مرض",
        "حصاد","سقي","تربة","مناخ","حرارة","مطر",
    ]
    text_lower = text.lower()
    if any(kw in text_lower for kw in french_kw):
        return "fr"
    if any(kw in text for kw in darija_kw):
        return "ar"
    return "fr"


SYSTEM_PROMPTS = {
    "ar": (
        "أنت FellahAlert، مستشار زراعي للمزارعين الصغار في المغرب. "
        "بناءً على بيانات الطقس ونوع المحصول وقاعدة المعرفة الزراعية المقدمة، "
        "قدم نصيحة واحدة عملية لهذا الأسبوع في جملتين إلى ثلاث جمل قصيرة بالضبط. "
        "اكتب بالدارجة المغربية (الخط العربي). "
        "إذا كانت هناك معلومات من قاعدة المعرفة ذات صلة، استخدمها لتقديم نصيحة دقيقة وموثوقة. "
        "كن عملياً ومباشراً."
    ),
    "fr": (
        "Tu es FellahAlert, un conseiller agricole pour les petits agriculteurs marocains. "
        "En te basant sur les données météo, le type de culture et la base de connaissances agronomiques fournis, "
        "donne UN conseil concret pour cette semaine en exactement 2-3 courtes phrases. "
        "Si des informations de la base de connaissances sont pertinentes, utilise-les pour un conseil précis et fiable. "
        "Écris en français simple et accessible. Sois pratique et direct."
    ),
}


def get_advice(
    crop: str,
    city: str,
    weather: dict | None,
    lang: str = "fr",
    problem_type: str = "général",
    rag_context: str = "",
) -> str:
    """Generate AI advice, optionally grounded with RAG context."""
    if weather:
        if lang == "ar":
            weather_text = (
                f"بيانات الطقس لـ {city} (آخر 7 أيام):\n"
                f"- متوسط الحرارة: {weather['avg_temp_c']}°C\n"
                f"- مجموع الأمطار: {weather['total_rain_mm']} ملم\n"
                f"- متوسط الرطوبة: {weather['avg_humidity_pct']}%\n"
            )
        else:
            weather_text = (
                f"Données météo pour {city} (7 derniers jours) :\n"
                f"- Température moyenne : {weather['avg_temp_c']}°C\n"
                f"- Précipitations totales : {weather['total_rain_mm']} mm\n"
                f"- Humidité moyenne : {weather['avg_humidity_pct']}%\n"
            )
    else:
        weather_text = (
            f"المنطقة: {city} (لا تتوفر بيانات طقس تفصيلية).\n"
            if lang == "ar"
            else f"Région : {city} (données météo non disponibles).\n"
        )

    if lang == "ar":
        user_message = f"المحصول: {crop}\nنوع المشكلة: {problem_type}\n{weather_text}"
        if rag_context:
            user_message += f"\n\n=== قاعدة المعرفة الزراعية ===\n{rag_context}"
    else:
        user_message = f"Culture : {crop}\nType de problème : {problem_type}\n{weather_text}"
        if rag_context:
            user_message += f"\n\n=== Base de connaissances agronomiques ===\n{rag_context}"

    system_prompt = SYSTEM_PROMPTS.get(lang, SYSTEM_PROMPTS["fr"])
    client = get_client()
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=user_message,
        config={
            "system_instruction": system_prompt,
            "max_output_tokens": 400,
        },
    )
    return response.text.strip()


def diagnose_plant(
    image_url: str | None = None,
    lang: str = "fr",
    *,
    image_bytes: bytes | None = None,
    mime_type: str = "image/jpeg",
) -> dict:
    """
    Analyse a plant photo using Gemini Vision (inline bytes).

    Accepts either:
      - image_url: publicly reachable URL (bytes fetched here)
      - image_bytes + mime_type: raw bytes already downloaded (e.g. from WhatsApp)

    Returns structured diagnosis: disease, confidence, symptoms, treatment, urgency.
    """
    import json as _json
    import base64
    import requests as _req

    if image_bytes is None:
        if not image_url:
            raise ValueError("Either image_url or image_bytes must be provided")
        resp = _req.get(image_url, timeout=10, headers={"User-Agent": "FellahAlert/1.0"})
        resp.raise_for_status()
        image_bytes = resp.content
        mime_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()

    content_type = mime_type if mime_type in ("image/jpeg", "image/png", "image/webp", "image/gif") else "image/jpeg"
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    if lang == "ar":
        prompt = (
            'أنت خبير زراعي في المغرب. حلل الصورة وأجب بسطر JSON واحد فقط بدون markdown:\n'
            '{"disease":"المرض","confidence":"عالية","symptoms":"الأعراض","treatment":"العلاج","urgency":"عاجل","healthy":false}'
        )
        system = "خبير زراعي. أجب بـ JSON فقط على سطر واحد."
    else:
        prompt = (
            'Tu es un expert phytopathologiste au Maroc. Analyse cette image de plante.\n'
            'Réponds avec EXACTEMENT une ligne JSON compact, sans markdown:\n'
            '{"disease":"nom_maladie","confidence":"haute","symptoms":"symptomes","treatment":"traitement","urgency":"urgent","healthy":false}'
        )
        system = "Expert phytopathologiste. Réponds uniquement avec du JSON compact sur une ligne."

    from google.genai import types as _types
    user_content = _types.Content(
        role="user",
        parts=[
            _types.Part(text=prompt),
            _types.Part(inline_data=_types.Blob(mime_type=content_type, data=image_bytes)),
        ],
    )

    client = get_client()
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=user_content,
        config={
            "system_instruction": system,
            "max_output_tokens": 800,
        },
    )
    raw = response.text.strip() if response.text else ""

    # Strip markdown code fences if present
    import re as _re
    if raw.startswith("```"):
        raw = _re.sub(r"^```[a-z]*\n?", "", raw)
        raw = _re.sub(r"\n?```$", "", raw).strip()

    # Try to parse complete JSON first
    match = _re.search(r'\{.*\}', raw, _re.DOTALL)
    if match:
        try:
            return _json.loads(match.group(0))
        except _json.JSONDecodeError:
            pass

    # Model truncated the response — extract field by field via regex
    def _grab(field: str) -> str:
        m = _re.search(rf'"{field}"\s*:\s*"([^"]*)"', raw)
        return m.group(1) if m else ""

    def _grab_bool(field: str) -> bool:
        m = _re.search(rf'"{field}"\s*:\s*(true|false)', raw)
        return m.group(1) == "true" if m else False

    disease = _grab("disease") or "Non identifiable"
    confidence = _grab("confidence") or "faible"
    symptoms = _grab("symptoms") or "Impossible d'analyser"
    treatment = _grab("treatment") or "Consultez un agronome"
    urgency = _grab("urgency") or "normal"
    healthy = _grab_bool("healthy")

    if not disease and not symptoms:
        raise ValueError(f"Could not extract any fields from vision response: {raw[:120]}")

    return {
        "disease": disease,
        "confidence": confidence,
        "symptoms": symptoms,
        "treatment": treatment,
        "urgency": urgency,
        "healthy": healthy,
    }


def check_gemini_status() -> bool:
    """
    Cached health check — calls Gemini at most once per minute.
    Uses 200 tokens to accommodate gemini-2.5-flash's thinking overhead.
    """
    now = time.time()
    if now - _status_cache["ts"] < _STATUS_TTL and _status_cache["ok"] is not None:
        return _status_cache["ok"]
    try:
        client = get_client()
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents="Reply with the single word: OK",
            config={"max_output_tokens": 200},
        )
        result = resp.text is not None and len(resp.text.strip()) > 0
    except Exception:
        result = False
    _status_cache["ok"] = result
    _status_cache["ts"] = now
    return result
