"""
Structured message parser using Gemini.

Extracts: crop, city, problem_type, urgency, language from free-form farmer messages.
Robust against truncation: tries full JSON, then regex field-by-field, then comma-split.
"""
import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("fellahalert.parser")

SYSTEM_INSTRUCTION = (
    "Agricultural data extractor. Reply ONLY with one compact JSON line, no markdown, no explanation. "
    "Schema: "
    '{"crop":"PLANT","city":"CITY","problem_type":"TYPE","urgency":"LEVEL","language":"CODE","original_crop":"ORIG"} '
    "Rules: "
    "crop=common plant name in French (1-2 words, e.g. tomate, ble, oignon); "
    "city=Moroccan city lowercase (default meknes); "
    "problem_type=one of: maladie irrigation ravageur fertilisation recolte semis general; "
    "urgency=one of: haute normale faible; "
    "language=ar if message is mostly Arabic else fr; "
    "original_crop=exact crop word from original message."
)


@dataclass
class ParsedQuery:
    crop: str
    city: str
    problem_type: str = "général"
    urgency: str = "normale"
    language: str = "fr"
    original_crop: str = ""

    def __post_init__(self):
        if not self.original_crop:
            self.original_crop = self.crop


# Map English problem_type values back to French
_PROBLEM_MAP = {
    "maladie": "maladie",
    "irrigation": "irrigation",
    "ravageur": "ravageur",
    "fertilisation": "fertilisation",
    "recolte": "récolte",
    "récolte": "récolte",
    "semis": "semis",
    "general": "général",
    "général": "général",
}


def _normalise_problem(val: str) -> str:
    return _PROBLEM_MAP.get(val.lower().strip(), "général")


def _extract_fields_regex(text: str) -> dict:
    """Extract key fields using regex — handles partial/truncated JSON."""
    result = {}
    for key in ("crop", "city", "problem_type", "urgency", "language", "original_crop"):
        m = re.search(rf'"{key}"\s*:\s*"([^"]*)"', text)
        if m:
            result[key] = m.group(1).strip()
    return result


def _detect_language(message: str) -> str:
    arabic_chars = len(re.findall(r'[\u0600-\u06FF]', message))
    return "ar" if arabic_chars / max(len(message), 1) > 0.3 else "fr"


def _fallback_parse(message: str) -> ParsedQuery:
    """Original comma-split parser as last resort fallback."""
    normalized = message.replace("،", ",")
    parts = [p.strip() for p in normalized.split(",")]
    crop = parts[0] if parts else message.strip()
    city = parts[1] if len(parts) > 1 else "meknès"
    return ParsedQuery(
        crop=crop.strip()[:50],
        city=city.strip().lower(),
        language=_detect_language(message),
        original_crop=crop.strip()[:50],
    )


def parse_message(message: str) -> ParsedQuery:
    """
    Parse a farmer message using Gemini.
    Falls back gracefully: full JSON → regex field extraction → comma-split.
    """
    from ai import get_client
    try:
        client = get_client()
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"Message: {message[:200]}",
            config={
                "system_instruction": SYSTEM_INSTRUCTION,
                "max_output_tokens": 300,
            },
        )
        raw = response.text.strip() if response.text else ""

        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw).strip()

        # 1. Try complete JSON parse
        json_match = re.search(r'\{[^{}]+\}', raw, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(0))
                crop = str(data.get("crop", "")).strip()[:50]
                if crop:
                    return ParsedQuery(
                        crop=crop,
                        city=str(data.get("city", "meknès")).strip().lower()[:50],
                        problem_type=_normalise_problem(str(data.get("problem_type", "général"))),
                        urgency=str(data.get("urgency", "normale")),
                        language=str(data.get("language", _detect_language(message))),
                        original_crop=str(data.get("original_crop", crop)).strip()[:50],
                    )
            except (json.JSONDecodeError, ValueError):
                pass

        # 2. Regex field extraction (handles truncated responses)
        fields = _extract_fields_regex(raw)
        if fields.get("crop"):
            logger.info("Parser used regex fallback for truncated response")
            return ParsedQuery(
                crop=fields["crop"][:50],
                city=fields.get("city", "meknès").lower()[:50],
                problem_type=_normalise_problem(fields.get("problem_type", "général")),
                urgency=fields.get("urgency", "normale"),
                language=fields.get("language", _detect_language(message)),
                original_crop=fields.get("original_crop", fields["crop"])[:50],
            )

        logger.warning("Structured parse yielded no usable data for: %.40s", message)
    except Exception as e:
        logger.warning("Structured parse error (%s), using fallback", e)

    return _fallback_parse(message)
