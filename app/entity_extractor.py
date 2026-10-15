"""
BOMA v2.3 — Layer 1: Entity Extractor (Semantic Enhancement)
=============================================================
v2.3 Improvements:
  - Smarter stopword list — normal English words NEVER treated as materials
  - Better comma/and-list detection for "F-10A, F-10B and F-20A"
  - Plant code validation — rejects non-plant 4-digit numbers (e.g. "1000 USD")
  - Follow-up phrases fully resolved from session
  - Numeric-only materials handled carefully (2258 vs 2000 USD)
  - BOM numbers: 5–10 digit zero-padded patterns
  - Budget: handles "2.5 CR USD", "₹5 lakh", "1M USD", "200 crore INR"
  - Current query ALWAYS wins over session (no bleed-through)
"""

from __future__ import annotations

import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# SAP Material ID — starts with letter OR purely numeric (2–18 digits)
_MAT_PATTERN = re.compile(r'\b([A-Z][A-Z0-9\-_\.]{1,39}|[0-9]{2,18})\b')

# BOM number: 5–10 digit purely numeric, often zero-padded
_BOM_PATTERN = re.compile(r'\b(0{3,}\d{3,8})\b')

# Plant: exactly 4 digits starting with 1 or 2
_PLANT_PATTERN = re.compile(r'\b([12]\d{3})\b')

# Plant codes must be ANCHORED to the word "plant(s)" to be extracted.
# Without this, a bare 4-digit number that is actually a numeric MATERIAL
# id (e.g. "2258") gets misread as a plant purely because it happens to
# match the 4-digit/starts-with-1-or-2 shape — causing nonsense like
# "material 2258 at plant 2258" for a query that never mentioned a plant.
_PLANT_ANCHOR_PATTERN = re.compile(
    r'\bplants?\b\s*(?:number\s*)?[:\-]?\s*'
    r'([12]\d{3}(?:\s*(?:,|;|and|&|or|to)\s*[12]\d{3})*)',
    re.IGNORECASE,
)

# Budget patterns — handles all common formats
# NOTE: longer alternatives MUST come before shorter ones in each alternation
# (crore before cr, million before mn before m, billion before bn before b)
# to prevent "million" being greedily matched as just "m".
_BUDGET_PATTERN = re.compile(
    r'(?:₹|rs\.?\s*)?(\d+(?:\.\d+)?)\s*'
    r'(crores?|cr|lakhs?|lacs?|billions?|bn|millions?|mn|k|m|b)?\b\s*'
    r'(usd|inr|eur|gbp|chf)?',
    re.IGNORECASE,
)

# Explicit comma/and-separated list separator
_COMMA_LIST_SEP = re.compile(r'\s*(?:,|;|\band\b|\bor\b|&)\s*', re.IGNORECASE)

# Component spec patterns for hypothetical material planning
_COMP_PAT1 = re.compile(
    r'(\d+(?:\.\d+)?)\s*(?:x|units?\s+of|pcs?\s+of|nos?\s+of|qty\s*)?\s+([A-Z][A-Z0-9\-_\.]{1,39})',
    re.IGNORECASE,
)
_COMP_PAT2 = re.compile(
    r'([A-Z][A-Z0-9\-_\.]{1,39})\s*[:=]\s*(\d+(?:\.\d+)?)',
    re.IGNORECASE,
)
_COMP_PAT3 = re.compile(
    r'component\s+([A-Z][A-Z0-9\-_\.]{1,39})\s+(?:qty|quantity|no|count)?\s*(\d+(?:\.\d+)?)',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Stopwords — NEVER extracted as materials
# Includes: query keywords, common English, SAP-domain words, follow-up words
# ---------------------------------------------------------------------------
_STOPWORDS = {
    # SAP and system terms
    "BOM", "SAP", "INR", "USD", "EUR", "GBP", "CHF", "JPY",
    "PLANT", "CIN", "ECC", "S4", "HANA", "MM", "PP", "FI",
    # Query words
    "FOR", "AND", "THE", "FROM", "WITH", "WHAT", "HOW", "WHY",
    "CAN", "ALL", "ANY", "ARE", "IS", "IN", "AT", "OF", "TO",
    "BY", "DO", "BE", "IF", "OR", "TELL", "ME", "GIVE",
    "SHOW", "FIND", "GET", "FETCH", "LIST", "COMPARE", "VS",
    "ACROSS", "ACROSS", "BETWEEN",
    # Material/cost domain words
    "MAIN", "COST", "PRICE", "DIFF", "BUY", "MAKE", "HAVE",
    "WILL", "SAVE", "MANY", "MUCH", "NUMBER", "NUMBERS",
    "MATERIALS", "COMPONENT", "COMPONENTS", "RATE", "VALUE",
    "MATERIAL", "PLANTS", "PLANT", "PROCUREMENT", "STRATEGY",
    "PURCHASE", "PURCHASING", "PURCHASES", "SPEND", "SPENDING",
    "DIFFERENCE", "DIFFERENCES",
    # Assembly / construction verbs — never SAP material codes
    "BUILD", "BUILDS", "BUILT", "BUILDING", "BUILDOUT",
    "ASSEMBLE", "ASSEMBLES", "ASSEMBLED", "ASSEMBLING", "ASSEMBLY",
    "MANUFACTURE", "MANUFACTURES", "MANUFACTURED", "MANUFACTURING",
    "CREATE", "CREATES", "CREATED", "CREATING",
    "DESIGN", "DESIGNS", "DESIGNED", "DESIGNING",
    "PRODUCE", "PRODUCES", "PRODUCED", "PRODUCING",
    "CONSTRUCT", "CONSTRUCTS", "CONSTRUCTED", "CONSTRUCTING",
    "INSTALL", "INSTALLS", "INSTALLED", "INSTALLING",
    "EXPLODE", "EXPLODES", "EXPLODED", "EXPLODING",
    # Structural / descriptive words
    "FULL", "FULLY", "COMPLETE", "COMPLETELY", "ENTIRE", "ENTIRELY",
    "BOMS", "HIERARCHY", "STRUCTURE", "TREE", "LEVEL", "LEVELS",
    "RECORDS", "RECORD", "INFO", "INFOS",
    "SUPPLIER", "SUPPLIERS", "SUPPLY", "SUPPLIES",
    "VENDOR", "VENDORS", "SOURCE", "SOURCES",
    "SAVING", "SAVINGS", "SWITCH", "SWITCHING",
    "RESPONSIBLE", "LEFT", "REMAINING",
    # Adjectives that can look like SAP codes
    "CHEAP", "CHEAPER", "CHEAPEST", "EXPENSIVE",
    # Follow-up / common English — NEVER material codes
    "OTHERS", "REMAINING", "CONTINUE", "REST", "ABOVE", "SAME",
    "THOSE", "THEM", "EACH", "ELSE", "ABOUT", "PROCEED",
    "THESE", "THEN", "NEXT", "ALSO", "PLEASE", "JUST",
    "MORE", "LESS", "ONLY", "BOTH", "THEIR", "THAT", "THIS",
    # Question words
    "WHEN", "WHERE", "WHO", "WHICH", "WHOSE", "DOES", "DID",
    "HAS", "HAD", "WOULD", "COULD", "SHOULD", "MIGHT",
    "NEED", "WANT", "LIKE", "USING", "USED", "PLAN",
    # Adjectives/adverbs
    "NEW", "OLD", "CURRENT", "TOTAL", "UNITS", "UNIT",
    "BUDGET", "AMOUNT", "SPEND", "SPENDING", "AVAILABLE",
    "INTO", "ONTO", "OVER", "UNDER", "AFTER", "BEFORE",
    "SINCE", "UNTIL", "WHILE", "DURING", "AROUND", "THROUGH",
    "AGAINST", "ALONG", "UPON", "BEST", "WORST", "MOST",
    # Analysis terms
    "ANALYSIS", "ANALYZE", "ANALYSE", "REPORT", "RESULT",
    "DATA", "INFO", "INFORMATION", "DETAIL", "DETAILS",
    "SUMMARY", "OVERVIEW", "BREAKDOWN",
    # Currencies as standalone tokens
    "CRORE", "CRORES", "LAKH", "LAKHS", "LAC", "LACS",
    "MILLION", "MILLIONS", "BILLION", "BILLIONS", "THOUSAND",
    # Comparison / superlative words — NEVER material codes
    # (these describe HOW to rank/filter results, they are not entities)
    "CHEAP", "CHEAPER", "CHEAPEST", "COSTLY", "COSTLIER", "COSTLIEST",
    "EXPENSIVE", "DEAR", "DEARER", "DEAREST", "PRICEY", "PRICIER", "PRICIEST",
    "HIGH", "HIGHER", "HIGHEST", "LOW", "LOWER", "LOWEST",
    "PRICED", "PRICING", "PRICES", "COSTS", "COSTING",
    "GREATER", "GREATEST", "SMALLER", "SMALLEST", "MINIMUM", "MAXIMUM",
    "MIN", "MAX", "TOP", "BOTTOM", "OPTIMAL", "OPTIMUM", "OPTIMIZED",
    "GLOBALLY", "OVERALL", "AMONG", "WITHIN", "AMONGST",
    # Buy/sell/procure verbs — never material codes
    "SELL", "SELLS", "SELLING", "SOLD", "BUYS", "BUYING", "BOUGHT",
    "PROCURE", "PROCURING", "PROCURED", "SOURCE", "SOURCING", "SOURCED",
    "SWITCH", "SWITCHING", "CONSIDER", "RECOMMEND", "RECOMMENDED",
    "STRATEGIC", "STRATEGIES", "EXTERNAL", "INTERNAL", "INHOUSE",
    "EXIST", "EXISTS", "EXISTING", "VERIFY", "CHECK", "CHECKING",
    "DOUBT", "SURE", "CONFIRM", "CONFIRMED",
    # Pronouns / conversational fillers / misspellings that otherwise leak
    # through the broad-scan fallback (Strategy B)
    "YOU", "YOUR", "YOURS", "WE", "OUR", "OURS", "THEY", "THEIR", "IT",
    "SO", "NOW", "YES", "YEAH", "NO", "OK", "OKAY", "SAY", "SAYS", "SAID",
    "SAYING", "SEE", "SEEING", "SEEN", "LOOK", "LOOKING", "TABLE", "TABLES",
    "PROVIDE", "PROVIDED", "PROVIDING", "CONSIDERING", "CONSIDER",
    "COMPARISON", "COMPARISION", "COMPARING", "COMPARED",
    "AGAIN", "STILL", "EVEN", "TOO", "VERY", "REALLY", "ACTUALLY",
    "HERE", "THERE", "SOME", "ANOTHER", "OWN", "SUCH", "WAY",
    # More conversational/meta noise words seen leaking through
    "DOWN", "UP", "WAS", "WERE", "QUESTION", "QUESTIONS", "CONTAIN",
    "CONTAINS", "FIRST", "SECOND", "THIRD", "TIME", "TIMES", "GAVE",
    "GIVEN", "WRONG", "RIGHT", "CORRECT", "COOL", "GOOD", "GREAT",
    "NICE", "BAD", "ANSWER", "ANSWERS", "FREE", "ANYTHING", "SOMETHING",
    "NOTHING", "EVERYTHING", "SINGLE", "MULTIPLE", "TAKING", "TAKEN",
    "TAKE", "PICKED", "PICKING", "CHOOSING", "CHOSEN", "CHOSE",
    "FOLLOW", "FOLLOWING", "FOLLOWED", "SCENARIO", "CASE", "OPTIMIZE",
    "OPTIMAL", "OPTIMIZING", "MAXIMIZE", "MAXIMIZING", "MINIMIZE",
    "ALLOCATE", "ALLOCATION",
}

# 4-digit numbers that look like plants but are actually amounts/years
_NON_PLANT_4DIGIT = {"2000", "2001", "2002", "2003", "2004", "2005",
                      "2006", "2007", "2008", "2009", "2010",
                      "1000", "1001", "1002", "1003", "1004", "1005"}


# ---------------------------------------------------------------------------
# Follow-up phrase detection
# ---------------------------------------------------------------------------
_FOLLOWUP_PHRASES: dict[str, str] = {
    # others
    "what about others":        "others",
    "what about the others":    "others",
    "and the others":           "others",
    "other materials":          "others",
    "other ones":               "others",
    "the others":               "others",
    "how about others":         "others",
    "for the other materials":  "others",
    "for the other ones":       "others",
    "what about the other":     "others",
    "for other materials":      "others",
    # remaining
    "remaining materials":      "remaining",
    "remaining ones":           "remaining",
    "the remaining":            "remaining",
    "what about remaining":     "remaining",
    "for the remaining":        "remaining",
    "and remaining":            "remaining",
    "remaining items":          "remaining",
    # rest
    "what about rest":          "rest",
    "the rest":                 "rest",
    "rest of them":             "rest",
    "for the rest":             "rest",
    "how about the rest":       "rest",
    "rest of the materials":    "rest",
    "rest of material":         "rest",
    # continue
    "continue":                 "continue",
    "proceed":                  "continue",
    "go on":                    "continue",
    "carry on":                 "continue",
    "next one":                 "continue",
    "move on":                  "continue",
    "what's next":              "continue",
    "whats next":               "continue",
    # all of them
    "all of them":              "all_of_them",
    "each of them":             "all_of_them",
    "for all of them":          "all_of_them",
    "all materials":            "all_of_them",
    "all the materials":        "all_of_them",
    "every material":           "all_of_them",
    # same for all
    "same for all":             "same_for_all",
    "same for all plants":      "same_for_all",
    "do the same":              "same_for_all",
    "same analysis":            "same_for_all",
    "same for each":            "same_for_all",
    "apply to all":             "same_for_all",
    "do same for":              "same_for_all",
    # same as above
    "same as above":            "same_as_above",
    "as above":                 "same_as_above",
    "like above":               "same_as_above",
    "mentioned above":          "same_as_above",
    "as mentioned":             "same_as_above",
    "as discussed":             "same_as_above",
    "the above material":       "same_as_above",
    "the above":                "same_as_above",
    "for the above":            "same_as_above",
    # what else
    "what else":                "what_else",
    "anything else":            "what_else",
    "is there more":            "what_else",
}

# ---------------------------------------------------------------------------
# Meta-commentary detection
# ---------------------------------------------------------------------------
# Phrases that mean "you're commenting on / correcting my PREVIOUS answer",
# not "here is a NEW material name". Without this, the broad-scan fallback
# (Strategy B) will happily grab whatever capitalised word comes first in
# these sentences ("THREE", "FIRST", "EARLIER", "KNOW"...) and treat it as
# a material — this is exactly the "agent doesn't understand intent" failure
# mode. When one of these phrases is present and no explicit material/BOM
# pattern matched earlier, we skip the broad scan and reuse the material(s)
# already on record from the session instead of guessing.
_META_COMMENTARY_PHRASES = [
    "you said", "you only", "you didn't", "you did not", "you didnt",
    "i am saying", "i'm saying", "im saying",
    "you told me", "you mentioned", "above you said", "earlier you said",
    "check again", "recheck", "double check", "you provided",
    "i doubt", "are you sure", "is that correct", "that's wrong",
    "thats wrong", "not correct", "confusing",
    "but you said",
]


def _is_meta_commentary(query_lower: str) -> bool:
    """True if the user is commenting on/correcting the PREVIOUS answer,
    rather than introducing a new material."""
    return any(p in query_lower for p in _META_COMMENTARY_PHRASES)


def _extract_anchored_plants(query: str) -> list[str]:
    """Extract plant codes ONLY when anchored to the word 'plant(s)'.

    A bare 4-digit number elsewhere in the sentence (e.g. a numeric material
    ID like "2258", or a budget figure) must NEVER be silently reinterpreted
    as a plant just because it has the right shape. Handles lists too:
    "plant 1710 and 1720" → ['1710', '1720'].
    """
    plants: list[str] = []
    for m in _PLANT_ANCHOR_PATTERN.finditer(query):
        group = m.group(1)
        for p in _PLANT_PATTERN.findall(group):
            if p not in _NON_PLANT_4DIGIT and p not in plants:
                plants.append(p)
    return plants


def _detect_followup(query_lower: str) -> tuple[bool, str]:
    """Detect conversational follow-up references."""
    for phrase, ftype in _FOLLOWUP_PHRASES.items():
        if phrase in query_lower:
            return True, ftype
    return False, ""


def _extract_comma_and_list(query_upper: str, stopwords: set) -> list[str]:
    """
    Detect explicit comma/and-separated sequences of SAP material IDs.
    Returns the list ONLY if ≥2 consecutive valid-looking material tokens found.

    Example: "F-10A, F-10B and F-20A" → ["F-10A", "F-10B", "F-20A"]
    Example: "compare price and cost" → [] (common English, no SAP IDs)
    """
    raw_tokens = _COMMA_LIST_SEP.split(query_upper)
    candidates: list[str] = []
    for tok in raw_tokens:
        tok = tok.strip()
        # Scan EVERY token in the segment (not just the first match) — a
        # segment like "WHAT ARE THE BOM NUMBERS FOR F-10A" has several
        # leading stopwords before the real material code, and re.search()
        # only ever looks at the first hit, silently dropping F-10A.
        found_in_segment: Optional[str] = None
        for m in re.finditer(r'\b([A-Z][A-Z0-9\-_\.]{1,39})\b', tok):
            cand = m.group(1)
            # Must look like an actual SAP ID — not a common word
            if (cand not in stopwords
                    and len(cand) >= 2
                    # Must have at least one digit OR be >= 4 chars (SEAT is valid, BOM is not)
                    and (any(c.isdigit() for c in cand) or len(cand) >= 4)
                    # Avoid all-letter common words <= 5 chars that are likely English
                    and not (cand.isalpha() and len(cand) <= 3)):
                found_in_segment = cand
                break  # take the FIRST valid (non-stopword) token in this segment
        if found_in_segment:
            candidates.append(found_in_segment)
    return candidates if len(candidates) >= 2 else []


def _extract_hypothetical_components(query: str) -> list[dict]:
    """Extract component specifications from hypothetical/planning queries."""
    components: list[dict] = []
    seen: set[str] = set()
    q_upper = query.upper()

    for match in _COMP_PAT1.finditer(q_upper):
        qty = float(match.group(1))
        mat = match.group(2).strip()
        if mat not in _STOPWORDS and mat not in seen and len(mat) >= 2:
            components.append({"material": mat, "quantity": qty})
            seen.add(mat)

    for match in _COMP_PAT2.finditer(q_upper):
        mat = match.group(1).strip()
        qty = float(match.group(2))
        if mat not in _STOPWORDS and mat not in seen and len(mat) >= 2:
            components.append({"material": mat, "quantity": qty})
            seen.add(mat)

    for match in _COMP_PAT3.finditer(q_upper):
        mat = match.group(1).strip()
        qty = float(match.group(2))
        if mat not in _STOPWORDS and mat not in seen and len(mat) >= 2:
            components.append({"material": mat, "quantity": qty})
            seen.add(mat)

    return components


def _is_valid_material(candidate: str, plant_set: set, bom_number: Optional[str]) -> bool:
    """
    Returns True if a token looks like a genuine SAP material code.
    Filters out plants, BOM numbers, stopwords, and common English words.
    """
    if not candidate or len(candidate) < 2:
        return False
    if candidate in _STOPWORDS:
        return False
    if candidate in plant_set:
        return False
    if bom_number and candidate == bom_number:
        return False
    # Pure numeric: only valid if 2-7 digits (not a plant, not a BOM, not a year)
    if candidate.isdigit():
        if candidate in _NON_PLANT_4DIGIT:
            return False
        if len(candidate) > 7:  # too long to be a material number
            return False
        return True
    # Alphabetic-only tokens: short common words (<=3 letters, e.g. "YOU",
    # "SEE", "ARE") are virtually never real SAP material codes — real
    # alpha-only materials in this system (DEMOCAR, SEAT, GEARBOX...) are
    # all 4+ letters.
    if candidate.isalpha() and len(candidate) <= 3:
        return False
    return True


def extract_entities(query: str, session: dict) -> dict:
    """
    Extract entities from query. Session is READ-ONLY fallback for MISSING fields.
    Current query entities ALWAYS override session.

    Returns:
        material, materials, plants, bom_number,
        budget, budget_currency,
        is_followup, followup_type,
        hypothetical_components,
        entities_found_in_query, raw_query
    """
    q_upper = query.upper()
    q_lower = query.lower()
    entities_found_in_query: list[str] = []

    # ── 0. Follow-up detection (FIRST — before anything else) ───────────────
    is_followup, followup_type = _detect_followup(q_lower)
    # Meta-commentary ("you said", "I doubt", "check again"...) behaves like
    # a "same as above" follow-up if no more-specific follow-up type matched —
    # it should inherit the FULL material context from session, not just a
    # single leftover field.
    if not is_followup and _is_meta_commentary(q_lower):
        is_followup = True
        followup_type = "same_as_above"

    # ── 1. BOM number (8-digit zero-padded) ──────────────────────────────────
    bom_match = _BOM_PATTERN.search(query)
    bom_number: Optional[str] = bom_match.group(1) if bom_match else None
    if bom_number:
        entities_found_in_query.append("bom_number")

    # ── 2. Plants (4-digit SAP plant codes, ANCHORED to the word "plant") ───
    plants_found = _extract_anchored_plants(query)
    if plants_found:
        entities_found_in_query.append("plants")
    plant_set = set(plants_found)

    # ── 3. Material extraction ────────────────────────────────────────────────
    primary_material: Optional[str] = None
    all_materials: list[str] = []

    # Strategy A0: Explicit comma/and-list ("F-10A, F-10B and F-20A")
    list_materials = _extract_comma_and_list(q_upper, _STOPWORDS)
    list_materials = [m for m in list_materials
                      if _is_valid_material(m, plant_set, bom_number)]
    if list_materials:
        all_materials = list_materials
        primary_material = list_materials[0]
        entities_found_in_query.append("material")
        entities_found_in_query.append("multiple_materials")
        logger.debug("[extractor] A0 comma-list: %s", list_materials)

    # Strategy A1: Explicit anchor patterns (single primary material)
    if not primary_material:
        explicit_patterns = [
            r'(?:material|mat)\s+([A-Z][A-Z0-9\-_\.]{1,39}|[0-9]{2,18})',
            r'(?:bom\s+(?:number|numbers|no|nos?)\s+(?:for|of))\s+([A-Z][A-Z0-9\-_\.]{1,39})',
            r'(?:for|of)\s+material\s+([A-Z][A-Z0-9\-_\.]{1,39})',
            r'(?:purchase|buy|buying|sell|selling|of)\s+([A-Z][A-Z0-9\-_\.]{2,39})\s+(?:from|in|at|across)',
            r'(?:how\s+many)\s+([A-Z][A-Z0-9\-_\.]{2,39})',
            r'(?:for|of)\s+([A-Z][A-Z0-9\-_\.]{2,39})\s+(?:plant|in\s+plant|across)',
            r'(?:price\s+of|cost\s+of|save\s+on|valuation\s+of)\s+([A-Z][A-Z0-9\-_\.]{2,39})',
            r'(?:explode|components\s+of|structure\s+of|bom\s+of)\s+([A-Z][A-Z0-9\-_\.]{2,39})',
            r'(?:analys[ei]s?\s+(?:for|of)|analyz[ei]\s*)\s*([A-Z][A-Z0-9\-_\.]{2,39})',
            r'([A-Z][A-Z0-9\-_\.]{2,39})\s+(?:at|in)\s+plant',
            r'(?:bom\s+numbers\s+for)\s+([A-Z][A-Z0-9\-_\.]{2,39})',
            r'(?:what\s+are\s+bom\s+numbers\s+for)\s+([A-Z][A-Z0-9\-_\.]{2,39})',
        ]
        for pat in explicit_patterns:
            m = re.search(pat, q_upper, re.IGNORECASE)
            if m:
                candidate = m.group(1).strip().upper()
                if _is_valid_material(candidate, plant_set, bom_number):
                    primary_material = candidate
                    entities_found_in_query.append("material")
                    break

    # Strategy B: Broad scan — all non-stopword candidates
    # SKIP this for meta-commentary ("you said", "I doubt", "check again"...)
    # — these sentences are about the PREVIOUS answer, not a new material,
    # and blind regex scanning would otherwise grab ordinary words like
    # "THREE", "FIRST", "EARLIER" and misfire as materials.
    is_meta = _is_meta_commentary(q_lower)
    if not all_materials and not is_meta:
        broad_candidates = _MAT_PATTERN.findall(q_upper)
        seen: set[str] = set()
        broad_materials: list[str] = []
        for c in broad_candidates:
            if _is_valid_material(c, plant_set, bom_number) and c not in seen:
                broad_materials.append(c)
                seen.add(c)
        all_materials = broad_materials
        if not primary_material and all_materials:
            primary_material = all_materials[0]
            entities_found_in_query.append("material")

    # Strategy C: Numeric-only material (e.g. "2258", "10001")
    if not primary_material:
        num_match = re.search(r'\b(\d{2,7})\b', query)
        if num_match:
            candidate = num_match.group(1)
            if (candidate not in plant_set
                    and candidate != bom_number
                    and candidate not in _NON_PLANT_4DIGIT):
                primary_material = candidate
                all_materials = [candidate]
                entities_found_in_query.append("material")

    # ── 3a. Multi-material flag ───────────────────────────────────────────────
    if len(all_materials) > 1 and "multiple_materials" not in entities_found_in_query:
        entities_found_in_query.append("multiple_materials")

    # ── 3b. Budget-quantity guard — remove numeric "materials" that are actually
    # part of a budget/quantity expression (e.g. "500 lakh INR" → "500" must not
    # become the material. "how many units of SEAT" → SEAT is correct, 500 is not.)
    _BUDGET_MULTIPLIER_WORDS = (
        "crores?", "cr", "lakhs?", "lacs?", "millions?", "mn", "billions?", "bn", "k",
        "usd", "inr", "eur", "gbp", "units?",
    )
    if all_materials:
        _clean_mats = []
        for _m in all_materials:
            if _m.isdigit():
                # Check if this number immediately precedes a multiplier word in query
                _qty_re = re.compile(
                    r'\b' + re.escape(_m) + r'\s*(?:' +
                    '|'.join(_BUDGET_MULTIPLIER_WORDS) + r')\b',
                    re.IGNORECASE,
                )
                if _qty_re.search(q_lower):
                    logger.debug("[extractor] dropping budget-quantity number: %s", _m)
                    continue  # skip — it's part of a budget expression
                # Also skip if this number is part of an Indian comma-formatted number
                # e.g. "₹50,00,000" → fragments "50", "00", "000" should NOT become materials
                # Check if followed by ",digits" (e.g. "50,00") OR preceded by "," (e.g. ",000")
                _is_comma_num = bool(
                    re.search(r'\b' + re.escape(_m) + r',\d{2}', q_lower)
                    or re.search(r',\s*' + re.escape(_m) + r'\b', q_lower)
                )
                if _is_comma_num:
                    logger.debug("[extractor] dropping comma-formatted number fragment: %s", _m)
                    continue
            _clean_mats.append(_m)
        all_materials = _clean_mats
        if all_materials:
            primary_material = all_materials[0]
        elif primary_material and primary_material.isdigit():
            primary_material = None

    # ── 4. Budget ────────────────────────────────────────────────────────────
    budget: Optional[float] = None
    budget_currency = "USD"

    # Detect ₹ / Rs. prefix — means all amounts in this query are INR
    _has_rupee_prefix = "₹" in query or "rs." in q_lower or bool(re.search(r'\brs\s', q_lower))

    # Only parse budget when there's a recognisable budget-context signal near a number.
    # Including currency codes (usd/eur/inr/gbp) so "65000 USD" triggers budget parsing.
    budget_context_words = [
        "budget", "afford", "buy", "purchase", "spend", "cr", "crore", "crores",
        "lakh", "lakhs", "lac", "lacs", "million", "millions", "billion", "billions",
        "how many", "units",
        "usd", "eur", "gbp", "chf", "inr", "₹",  # ₹
    ]
    has_budget_context = any(w in q_lower for w in budget_context_words) or _has_rupee_prefix

    # Pre-process: strip commas inside numbers so "₹50,00,000" → "₹5000000"
    _q_budget_scan = re.sub(r'(\d),(\d)', r'\1\2', q_lower)

    if has_budget_context:
        for bm in _BUDGET_PATTERN.finditer(_q_budget_scan):
            raw_val_str = bm.group(1)
            unit = (bm.group(2) or "").lower()
            # Determine currency: explicit suffix > ₹/Rs. prefix > Indian multiplier > USD
            _explicit_curr = (bm.group(3) or "").upper()
            _is_indian_unit = unit in ("cr", "crore", "crores", "lakh", "lakhs", "lac", "lacs")
            if _explicit_curr:
                curr = _explicit_curr
            elif _has_rupee_prefix or _is_indian_unit:
                curr = "INR"
            else:
                curr = "USD"
            try:
                raw_val = float(raw_val_str)
                multiplier = {
                    "cr": 1e7, "crore": 1e7, "crores": 1e7,
                    "lakh": 1e5, "lac": 1e5, "lakhs": 1e5, "lacs": 1e5,
                    "k": 1e3,
                    "m": 1e6, "mn": 1e6, "million": 1e6, "millions": 1e6,
                    "b": 1e9, "bn": 1e9, "billion": 1e9, "billions": 1e9,
                }.get(unit, 1.0)
                candidate_budget = raw_val * multiplier

                # Guard: a bare 4-digit integer (no unit, no currency)
                # after a plant-context preposition is a PLANT CODE, not a budget.
                # e.g. "buy from 1710 only" — 1710 is plant, not USD 1710.
                _is_bare_4digit = (
                    unit == ""
                    and bm.group(3) is None  # no explicit currency suffix
                    and raw_val == int(raw_val)
                    and 1000 <= raw_val <= 9999
                )
                _plant_prep_re = re.compile(
                    r'\b(?:from|at|in|plant)\s+' + str(int(raw_val)) + r'\b',
                    re.IGNORECASE,
                )
                _looks_like_plant = _is_bare_4digit and bool(_plant_prep_re.search(q_lower))

                # Sanity check: budget should be > 100 (avoid "2 plants" being budget)
                if candidate_budget >= 100 and not _looks_like_plant:
                    budget = candidate_budget
                    budget_currency = curr
                    entities_found_in_query.append("budget")
                    break  # use first valid budget match
            except (ValueError, TypeError):
                pass

    # ── 5. Hypothetical component extraction ─────────────────────────────────
    hypothetical_components: list[dict] = []
    hypothetical_keywords = [
        "plan", "create", "design", "hypothetical", "estimate",
        "new material", "not yet exist", "i want to build",
        "components are", "consists of", "made of",
        "planning to create", "planning to make", "i will create",
        "not exist", "doesn't exist", "does not exist",
        "i am planning", "i'm planning",
    ]
    if any(kw in q_lower for kw in hypothetical_keywords):
        hypothetical_components = _extract_hypothetical_components(query)
        if hypothetical_components:
            entities_found_in_query.append("hypothetical_components")

    # ── 6. Build result — current query wins, session fills MISSING only ──────
    result: dict = {
        "material":                primary_material,
        "materials":               all_materials,
        "plants":                  plants_found,
        "bom_number":              bom_number,
        "budget":                  budget,
        "budget_currency":         budget_currency,
        "is_followup":             is_followup,
        "followup_type":           followup_type,
        "hypothetical_components": hypothetical_components,
        "entities_found_in_query": entities_found_in_query,
        "raw_query":               query,
    }

    # ── Detect "cheapest / lowest / minimum / any plant" intent ─────────────────
    # When user asks to find the cheapest plant automatically, DO NOT inherit a
    # specific plant from session — that would lock them to a prior plant and
    # skip the cross-plant search.
    _CHEAPEST_SIGNALS = (
        "cheapest plant", "cheapest available", "cheapest overall",
        "lowest price plant", "lowest cost plant", "minimum price plant",
        "minimum cost plant", "from any plant", "any available plant",
        "cheapest among all", "cheapest source", "from cheapest",
        "at cheapest", "most affordable", "least expensive",
        "best price plant", "best deal plant", "globally cheapest",
        "from the cheapest", "at the cheapest", "the cheapest plant",
        "lowest priced plant", "minimum priced plant",
        "cheapest possible", "lowest possible price",
        "minimum available", "find the cheapest", "find cheapest",
        "which plant is cheapest", "which is the cheapest",
        "cheapest option", "best sourcing plant",
    )
    # "across all plants" / "all plants" signals — no single plant should be injected
    _ALL_PLANTS_SIGNALS = (
        "across all plants", "across all plant", "all plants", "all plant",
        "all available plants", "across plants", "for all plants",
        "at all plants", "in all plants", "from all plants",
        "every plant", "any plant", "any of the plants",
        "all existing plants", "any available plant",
        "which plants", "across the plants",
    )
    _wants_cheapest_plant = (
        any(sig in q_lower for sig in _CHEAPEST_SIGNALS)
        # Broad fallback: any query containing "cheapest" without a specific plant
        # — "which plant has cheapest X?" is clearly a cheapest-plant query
        or ("cheapest" in q_lower and not plants_found)
        or ("lowest price" in q_lower and not plants_found)
        or ("minimum price" in q_lower and not plants_found)
        or ("best price" in q_lower and not plants_found)
    )
    _wants_all_plants = any(sig in q_lower for sig in _ALL_PLANTS_SIGNALS)

    # ── Pure price query detection — session budget must NOT bleed into a simple price lookup ──
    # "What is the price of F-10B at plant 1710?" should NEVER trigger budget analysis
    # even if the session has a budget from a prior turn.
    _PRICE_QUERY_SIGNALS = (
        "what is the price", "what's the price", "whats the price",
        "what is the cost", "what's the cost", "whats the cost",
        "price of", "cost of", "how much does", "how much is",
        "what does it cost", "what is the value",
        "show me the price", "show price", "show the price",
        "tell me the price", "tell the price",
        "price at plant", "price at", "price in plant",
        "price for", "get price", "current price",
        "cost at plant", "cost at",
    )
    _is_pure_price_query = (
        any(sig in q_lower for sig in _PRICE_QUERY_SIGNALS)
        and "budget" not in q_lower
        and "afford" not in q_lower
        and "how many" not in q_lower
        and "how much can i" not in q_lower
    )

    # ── BOM query without explicit plant — session plant must NOT bleed in ─────
    # "What are the BOM numbers for F-10A, F-20A, F-10B?" should search ALL plants,
    # not lock to plant 1720 just because the previous query was about plant 1720.
    _BOM_QUERY_SIGNALS = (
        "bom number", "bom numbers", "bill of material", "bill of materials",
        "which bom", "what bom", "get bom", "list bom", "list all bom",
        "bom for", "what are bom", "what are the bom", "bom no", "bom nos",
        "what boms", "list boms", "all boms", "show bom",
    )
    _is_bom_query_no_plant = (
        any(sig in q_lower for sig in _BOM_QUERY_SIGNALS)
        and not plants_found  # no plant explicitly mentioned in this query
    )

    # ── Fill ONLY missing fields from session ─────────────────────────────────
    if not result["material"] and session.get("material"):
        result["material"] = session["material"]
        logger.debug("[extractor] material from session: %s", result["material"])

    if not result["materials"] and session.get("material"):
        result["materials"] = [session["material"]]

    if (not result["plants"] and session.get("plants")
            and not _wants_cheapest_plant
            and not _wants_all_plants
            and not _is_bom_query_no_plant):
        result["plants"] = session["plants"]
        logger.debug("[extractor] plants from session: %s", result["plants"])
    elif _wants_cheapest_plant and not result["plants"]:
        logger.debug("[extractor] cheapest-plant intent detected — NOT inheriting session plants")
    elif _wants_all_plants and not result["plants"]:
        logger.debug("[extractor] all-plants intent detected — NOT inheriting session plants")
    elif _is_bom_query_no_plant:
        logger.debug("[extractor] BOM query without explicit plant — NOT inheriting session plants")

    if (result["budget"] is None
            and session.get("budget") is not None
            and not _is_pure_price_query):
        result["budget"] = session["budget"]
        result["budget_currency"] = session.get("budget_currency", "USD")
        logger.debug("[extractor] budget from session: %s %s",
                     result["budget"], result["budget_currency"])
    elif _is_pure_price_query and result["budget"] is None:
        logger.debug("[extractor] pure price query — NOT inheriting session budget")

    # ── Follow-up resolution: pull pending/remaining materials from session ───
    if is_followup:
        all_mentioned = session.get("materials_mentioned", [])
        processed     = set(session.get("materials_processed", []))
        pending       = [m for m in all_mentioned if m not in processed]

        if followup_type in ("others", "remaining", "rest"):
            if pending:
                result["materials"] = pending
                result["material"]  = pending[0]
                logger.debug("[extractor] follow-up '%s': pending=%s", followup_type, pending)
        elif followup_type in ("all_of_them", "same_for_all"):
            if all_mentioned:
                result["materials"] = all_mentioned
                result["material"]  = all_mentioned[0]
                logger.debug("[extractor] follow-up '%s': all=%s", followup_type, all_mentioned)
        elif followup_type == "continue":
            if pending:
                result["materials"] = pending[:1]
                result["material"]  = pending[0]
        elif followup_type == "same_as_above":
            if session.get("material") and not result["material"]:
                result["material"]  = session["material"]
                result["materials"] = [session["material"]]

        # If after follow-up resolution we still have no material → session fallback
        if not result["material"] and session.get("material"):
            result["material"]  = session["material"]
        if not result["materials"] and session.get("materials_mentioned"):
            result["materials"] = session["materials_mentioned"]

    logger.info(
        "[extractor] '%.60s' → mat=%s mats=%s plants=%s bom=%s "
        "budget=%s followup=%s(%s) found=%s",
        query,
        result["material"], result["materials"],
        result["plants"], result["bom_number"],
        result["budget"],
        result["is_followup"], result["followup_type"],
        result["entities_found_in_query"],
    )
    return result
