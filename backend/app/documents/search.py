import re
from typing import Any

from app.db import connect
from app.documents.errors import DocumentNotFound

SEARCH_CONTEXT_CHARS = 250
MAX_SEARCH_RESULTS = 5
STOP_WORDS = {
    "the", "and", "for", "are", "was", "with", "that", "this", "from", "what",
    "about", "into", "how", "who", "why", "when", "where", "which", "does",
}


def _search_terms(query: str) -> list[str]:
    words = re.findall(r"\w+", query.lower())
    terms = [word for word in words if len(word) > 2 and word not in STOP_WORDS]
    return list(dict.fromkeys(terms)) or [query.lower().strip()]


async def search_document_text(
    document_id: str, query: str
) -> tuple[list[dict[str, Any]], int]:
    """Rank passages by how many distinct query terms (and the exact phrase) they hold."""
    async with await connect() as conn:
        cursor = await conn.execute(
            "SELECT parsed_text FROM app.documents "
            "WHERE id = %s AND parse_status = 'parsed' AND expires_at > now()",
            (document_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        raise DocumentNotFound("The parsed document is not available.")
    text = row["parsed_text"]
    lower_text = text.lower()
    phrase = query.lower().strip()
    terms = _search_terms(query)
    hits: list[int] = []
    for term in dict.fromkeys([phrase, *terms]):
        position = lower_text.find(term)
        while position >= 0:
            hits.append(position)
            position = lower_text.find(term, position + max(1, len(term)))

    candidates = []
    for hit in sorted(set(hits)):
        start = max(0, hit - SEARCH_CONTEXT_CHARS)
        end = min(len(text), hit + SEARCH_CONTEXT_CHARS)
        window = lower_text[start:end]
        score = sum(term in window for term in terms) + (2 if phrase in window else 0)
        candidates.append((score, start, end))
    candidates.sort(key=lambda item: (-item[0], item[1]))

    results: list[dict[str, Any]] = []
    for score, start, end in candidates:
        if any(start < other["end"] and end > other["offset"] for other in results):
            continue
        results.append({"offset": start, "end": end, "score": score})
        if len(results) == MAX_SEARCH_RESULTS:
            break
    results.sort(key=lambda item: item["offset"])
    return [
        {"offset": item["offset"], "matched_terms": item["score"],
         "snippet": text[item["offset"]:item["end"]]}
        for item in results
    ], len(text)
