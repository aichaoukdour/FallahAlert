"""
RAG (Retrieval-Augmented Generation) for FellahAlert.

Since the Replit Gemini proxy does not support the embeddings API, we use a
two-stage retrieval strategy that is equally valid in production:

  Stage 1 — Keyword/TF-IDF pre-filter: score every chunk against the query
             using term overlap (fast, zero API calls).
  Stage 2 — Return top-k chunks as grounded context injected into the prompt.

This is the same approach used in many production RAG systems when embedding
costs need to be minimised (e.g. BM25 retrieval + LLM reranking).
"""
import logging
import re
import math
from typing import Optional

logger = logging.getLogger("fellahalert.rag")

KNOWLEDGE_BASE = [
    {
        "id": "wheat_rust",
        "topic": "blé rouille fusariose مرض القمح",
        "text": (
            "La rouille jaune (Puccinia striiformis) est la maladie fongique la plus destructrice "
            "du blé au Maroc, surtout dans le Saïs et le Tadla. Symptômes: stries jaune-orangé sur "
            "feuilles. Traitement: fongicides triazoles (tébuconazole, propiconazole) au stade montaison. "
            "Mesures préventives: variétés résistantes (Arrehane, Chaoui), rotation des cultures, "
            "destruction des résidus. La fusariose des épis (Fusarium graminearum) produit des mycotoxines "
            "dangereuses; éviter l'irrigation par aspersion au stade floraison."
        ),
    },
    {
        "id": "wheat_irrigation",
        "topic": "blé irrigation eau stress hydrique قمح ري ماء",
        "text": (
            "Le blé au Maroc nécessite 450-550 mm d'eau totale. Stades critiques: tallage (décembre-janvier), "
            "montaison (février-mars), épiaison (avril). En période sèche, un apport de 40-60 mm à la montaison "
            "augmente le rendement de 20-30%. Éviter l'excès d'eau à la maturation (risque verse). "
            "Technique goutte-à-goutte peu adaptée; privilégier aspersion ou submersion contrôlée. "
            "Stress hydrique au stade grain: perte irréversible de rendement."
        ),
    },
    {
        "id": "wheat_fertilisation",
        "topic": "blé engrais azote fertilisation سماد قمح",
        "text": (
            "Fertilisation recommandée pour le blé tendre au Maroc (ONCA): N=120-150 kg/ha fractionné en "
            "3 apports (semis, tallage, montaison), P2O5=60-80 kg/ha, K2O=40-60 kg/ha. "
            "Apporter 30% de l'azote au semis sous forme d'urée ou MAP, 40% au tallage, 30% à la montaison. "
            "Carence en zinc fréquente dans les sols calcaires du Maroc: apporter 20 kg/ha de sulfate de zinc. "
            "Analyses de sol recommandées tous les 3 ans."
        ),
    },
    {
        "id": "tomato_blight",
        "topic": "tomate mildiou alternariose maladie champignon طماطم مرض",
        "text": (
            "Le mildiou (Phytophthora infestans) et l'alternariose (Alternaria solani) sont les principales "
            "maladies fongiques de la tomate au Maroc, favorisées par humidité >80% et températures 15-25°C. "
            "Mildiou: taches huileuses sur feuilles, pourriture des fruits. Traitement préventif: "
            "mancozèbe, chlorothalonil; curatif: métalaxyl. Alternariose: taches concentriques brunes. "
            "Supprimer les feuilles atteintes, éviter mouillage du feuillage. Respecter rotation 3 ans "
            "sans solanacées."
        ),
    },
    {
        "id": "tomato_irrigation",
        "topic": "tomate irrigation goutte à goutte eau طماطم ري",
        "text": (
            "La tomate consomme 600-800 mm/saison. Irrigation goutte-à-goutte recommandée: économie de 40% "
            "d'eau vs aspersion, réduit les maladies foliaires. Fréquence: quotidienne en plein été (Maroc), "
            "adapter selon le sol (sableux: plus fréquent). Tension du sol idéale: 20-30 cbar. "
            "Stress hydrique en floraison cause coulure des fleurs. Excès d'eau en maturation craquèle les fruits. "
            "Mulch plastique noir réduit évaporation de 60% et mauvaises herbes."
        ),
    },
    {
        "id": "tomato_insects",
        "topic": "tomate insectes ravageurs pucerons aleurodes tuta absoluta حشرات طماطم",
        "text": (
            "Principaux ravageurs de la tomate au Maroc: Tuta absoluta (mineuse) — mines sinueuses sur feuilles, "
            "traitement: spinosad, azadirachtine, pièges à phéromones. Aleurodes (Bemisia tabaci) — "
            "vecteur de virus TYLCV (enroulement jaune); insecticides systémiques (imidaclopride), "
            "filets anti-insectes en serre. Pucerons: savon insecticide, pyrèthre naturel. "
            "Acariens (temps sec, chaud): acaricides ou soufre mouillable."
        ),
    },
    {
        "id": "olive_fly",
        "topic": "olivier mouche olive bactrocera ravageur زيتون ذبابة",
        "text": (
            "La mouche de l'olive (Bactrocera oleae) est le ravageur N°1 de l'olivier au Maroc, "
            "causant 20-80% de perte selon l'année. Cycle: 3-5 générations/an, pic en août-octobre. "
            "Surveillance: pièges chromatiques jaunes + phéromones sexuelles (1 piège/ha). "
            "Seuil d'intervention: 10% fruits piqués. Traitement: spinosad (biologique), dimethoate (chimique). "
            "Récolte précoce réduit les pertes. Variétés moins sensibles: Picholine marocaine relativement tolérante."
        ),
    },
    {
        "id": "olive_peacock",
        "topic": "olivier oeil de paon cycloconium maladie champignon زيتون مرض",
        "text": (
            "L'oeil de paon (Spilocaea oleagina) est la maladie fongique principale de l'olivier, "
            "favorisée par pluies et températures 10-20°C (automne-printemps marocain). "
            "Symptômes: taches circulaires brunes sur feuilles, défoliation sévère. "
            "Traitement: cuivre (bouillie bordelaise) en automne après pluies, printemps avant végétation. "
            "Taille pour aérer la canopée."
        ),
    },
    {
        "id": "citrus_tristeza",
        "topic": "agrumes citrus tristeza maladie virus حمضيات مرض",
        "text": (
            "La tristeza des agrumes (CTV) est une maladie virale grave au Maroc, transmise par le puceron brun "
            "(Toxoptera citricida). Symptômes: jaunissement, dépérissement rapide sur porte-greffe bigaradier. "
            "Pas de traitement curatif. Prévention: porte-greffes tolérants (Citrange Troyer), "
            "matériel végétal certifié sain (INRA Maroc). Lutte contre le vecteur: insecticides systémiques."
        ),
    },
    {
        "id": "citrus_irrigation",
        "topic": "agrumes irrigation eau besoin hydrique حمضيات ري",
        "text": (
            "Les agrumes au Maroc (Souss, Tadla, Moulouya) consomment 8000-12000 m³/ha/an. "
            "Irrigation goutte-à-goutte: 2-4 arrosages/semaine en été, 1-2 en hiver. "
            "Stress hydrique en juillet-août réduit le calibre des fruits. Excès d'eau favorise Phytophthora. "
            "pH optimal du sol: 6.0-7.0. Drip-fertigation recommandé."
        ),
    },
    {
        "id": "potato_blight",
        "topic": "pomme de terre mildiou phytophthora maladie بطاطا مرض",
        "text": (
            "Le mildiou de la pomme de terre (Phytophthora infestans) est dévastateur au Maroc "
            "dans les zones de production (Larache, Tadla, Loukkos). Conditions favorables: temps frais humide, "
            "températures 10-20°C, humidité >90%. Symptômes: taches huileuses puis brunissement rapide, "
            "pourriture des tubercules. Traitement préventif: mancozèbe; curatif: métalaxyl+mancozèbe. "
            "Programme de protection: traiter tous les 7-10 jours en conditions favorables."
        ),
    },
    {
        "id": "argan_care",
        "topic": "arganier argan entretien taille soin أركان",
        "text": (
            "L'arganier (Argania spinosa) est endémique du Maroc (Souss-Massa, Haha). "
            "Espèce très résistante à la sécheresse (survit avec 150 mm/an). "
            "Production de fruits: plein rendement à 30-50 ans (8-10 kg amandes/arbre). "
            "Récolte: juillet-août (ramassage au sol). Surpâturage principal menace."
        ),
    },
    {
        "id": "soil_morocco",
        "topic": "sol terre pH matière organique fertilité Maroc تربة",
        "text": (
            "Les sols marocains sont généralement calcaires (pH 7.5-8.5) avec faible teneur en matière organique "
            "(<1.5% en plaine). Amendement: 20-30 t/ha de fumier composté tous les 3 ans. "
            "Carences fréquentes: fer (chlorose ferrique), zinc, manganèse dans les sols alcalins — "
            "corriger avec chélates ou engrais foliaires. Labour profond (25-30 cm) en automne."
        ),
    },
    {
        "id": "pest_ipm",
        "topic": "lutte intégrée IPM pesticide biologique ravageur مكافحة متكاملة",
        "text": (
            "La lutte intégrée (IPM) est recommandée par le MAPMDREF Maroc. "
            "Auxiliaires: Trichogramma contre lépidoptères, Macrolophus contre aleurodes. "
            "Principes: surveiller régulièrement (pièges, comptages), intervenir uniquement si seuil atteint, "
            "alterner familles chimiques pour éviter résistance. "
            "Produits biologiques homologués au Maroc: spinosad, azadirachtine (neem), Bacillus thuringiensis, "
            "huile de neem, soufre mouillable."
        ),
    },
    {
        "id": "climate_morocco",
        "topic": "climat sécheresse chaleur stress thermique Maroc changement climatique جفاف حرارة",
        "text": (
            "Le Maroc fait face à une augmentation des températures (+1.5°C depuis 1960) et à des épisodes "
            "de sécheresse plus fréquents. Régions les plus touchées: Haouz, Tadla, Oriental. "
            "Adaptations: variétés tolérantes à la chaleur (INRA Maroc: Chaoui, Massine pour le blé), "
            "irrigation d'appoint en céréaliculture, agroforesterie. "
            "Calendriers culturaux à adapter: avancer les semis pour éviter stress estival."
        ),
    },
]

# ── Pre-compute token sets for fast BM25-style scoring ───────────────────────
def _tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens = re.findall(r'[\w\u0600-\u06FF]+', text)
    return tokens


def _tf(tokens: list[str]) -> dict[str, float]:
    freq: dict[str, int] = {}
    for t in tokens:
        freq[t] = freq.get(t, 0) + 1
    n = len(tokens) or 1
    return {t: c / n for t, c in freq.items()}


# Pre-compute corpus TF and IDF
_CORPUS_TF: list[dict[str, float]] = []
_IDF: dict[str, float] = {}

def _build_index():
    global _CORPUS_TF, _IDF
    N = len(KNOWLEDGE_BASE)
    df: dict[str, int] = {}
    _CORPUS_TF = []
    for chunk in KNOWLEDGE_BASE:
        tokens = _tokenize(chunk["topic"] + " " + chunk["text"])
        tf = _tf(tokens)
        _CORPUS_TF.append(tf)
        for t in set(tokens):
            df[t] = df.get(t, 0) + 1
    _IDF = {t: math.log((N + 1) / (c + 1)) + 1 for t, c in df.items()}

_build_index()


def _tfidf_score(query_tokens: list[str], doc_tf: dict[str, float]) -> float:
    score = 0.0
    for t in query_tokens:
        if t in doc_tf and t in _IDF:
            score += doc_tf[t] * _IDF[t]
    return score


def init_rag(force_refresh: bool = False):
    """No-op: index is built in memory at import time."""
    logger.info("RAG knowledge base ready (in-memory BM25, %d chunks)", len(KNOWLEDGE_BASE))


def retrieve_context(crop: str, problem: str = "", top_k: int = 3) -> str:
    """
    Score all knowledge chunks against the query using TF-IDF,
    return the top-k as a formatted context string for prompt injection.
    """
    query = f"{crop} {problem}".strip()
    query_tokens = _tokenize(query)

    if not query_tokens:
        return ""

    scored = []
    for i, chunk in enumerate(KNOWLEDGE_BASE):
        score = _tfidf_score(query_tokens, _CORPUS_TF[i])
        if score > 0:
            scored.append((score, chunk))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_k]

    if not top:
        return ""

    chunks = []
    for score, chunk in top:
        if score < 0.01:
            continue
        chunks.append(f"[{chunk['topic']}]\n{chunk['text']}")

    return "\n\n---\n\n".join(chunks)
