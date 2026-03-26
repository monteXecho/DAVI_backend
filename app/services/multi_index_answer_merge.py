"""
Merge strategy when /ask queries multiple OpenSearch indices (e.g. private user
index + company-admin role index). Each index returns an independent LLM answer;
concatenating them blindly can produce contradictory "no information" text next
to a correct cited answer.

Design (in order of trust):
1. Citation markers [n] in the answer text — primary signal that the model
   grounded the reply in retrieved chunks for that index.
2. Retrieval scores on returned document chunks — when the model omits [n],
   strong scores still indicate relevant evidence for that index.
3. Short "no information" boilerplate heuristics (nl/en) — fallback when scores
   are absent (older RAG payloads).

Segments with empty ``answer_text`` are always kept (same offset behaviour as
before: docs still occupy slots in the combined list). Dropping a segment with
text removes its answer *and* its ``raw_docs`` so citation offsets stay aligned.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("uvicorn")

# Answer has substantive uncited content (models sometimes omit [n]).
LONG_UNCITED_ANSWER_CHARS = 2500
# Below this length, a citation-less segment is dropped if another segment cites.
SHORT_UNCITED_WHEN_OTHERS_CITE_CHARS = 900
# Keep a citation-less segment if its best chunk score is within this ratio of
# the global best score across segments (same pipeline, different indices).
RETRIEVAL_SCORE_FRACTION_OF_BEST = 0.55

_CITATION_IN_ANSWER = re.compile(r"\[\d+\]")
_DISCLAIMER_PATTERNS = re.compile(
    r"(?:"
    r"de\s+opgegeven\s+documenten\s+bevat(?:ten)?\s+geen\s+informatie"
    r"|de\s+documenten\s+bevatten\s+geen\s+informatie"
    r"|bevat(?:ten)?\s+geen\s+informatie\s+over"
    r"|kan\s+niet\s+beantwoord\s+worden\s+op\s+basis\s+van\s+de\s+aangeleverde\s+documenten"
    r"|de\s+vraag\s+kan\s+niet\s+beantwoord\s+worden"
    r"|geen\s+relevante\s+informatie\s+(?:beschikbaar|om\s+deze)"
    r"|er\s+is\s+(?:dus\s+)?geen\s+relevante\s+informatie"
    r"|the\s+provided\s+documents\s+do\s+not\s+contain"
    r"|cannot\s+be\s+answered\s+based\s+on\s+the\s+(?:provided|given)\s+documents"
    r"|no\s+information\s+(?:in|about|found\s+in)\s+the\s+(?:provided|given)\s+documents"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RagIndexSegment:
    index_id: str
    answer_text: str
    raw_docs: list


def _max_chunk_score(documents: list[Any]) -> Optional[float]:
    best: Optional[float] = None
    for doc in documents or []:
        if not isinstance(doc, dict):
            continue
        meta = doc.get("meta") or {}
        s = meta.get("score")
        if s is None:
            continue
        try:
            v = float(s)
        except (TypeError, ValueError):
            continue
        if best is None or v > best:
            best = v
    return best


def _answer_has_citations(answer_text: str) -> bool:
    return bool(_CITATION_IN_ANSWER.search(answer_text or ""))


def _looks_like_disclaimer_only(text: str) -> bool:
    """Stock 'nothing in these documents' reply; never used when [n] is present."""
    t = (text or "").strip()
    if not t:
        return True
    if _CITATION_IN_ANSWER.search(t):
        return False
    if len(t) > 1200:
        return False
    head = t[:500]
    if len(t) <= 700:
        return bool(_DISCLAIMER_PATTERNS.search(t))
    return bool(_DISCLAIMER_PATTERNS.search(head))


def select_segments_for_merge(segments: list[RagIndexSegment]) -> list[RagIndexSegment]:
    """
    Return which per-index RAG responses to include in the merged answer.

    Segments with no answer text are always retained (preserve document offsets).

    Guarantees:
    - Never returns an empty list when ``segments`` is non-empty (falls back to all).
    - Dropping a text segment removes its ``raw_docs`` from the combined list
      (caller concatenates only returned segments in order).
    """
    if not segments:
        return []
    if len(segments) == 1:
        return segments

    text_segments = [s for s in segments if (s.answer_text or "").strip()]
    if len(text_segments) <= 1:
        return segments

    meta = []
    for seg in text_segments:
        text = (seg.answer_text or "").strip()
        meta.append(
            {
                "seg": seg,
                "text": text,
                "has_citations": _answer_has_citations(text),
                "max_score": _max_chunk_score(seg.raw_docs),
                "char_len": len(text),
            }
        )

    any_citations = any(m["has_citations"] for m in meta)
    score_values = [m["max_score"] for m in meta if m["max_score"] is not None]
    global_max = max(score_values) if score_values else None

    if any_citations:
        kept_text = _select_when_some_cite(meta, global_max)
    else:
        kept_text = _select_when_none_cite(meta)

    if not kept_text:
        logger.warning(
            "multi_index_answer_merge: selection empty; falling back to all %s segments",
            len(segments),
        )
        return segments

    kept_ids = {id(s) for s in kept_text}
    out: list[RagIndexSegment] = []
    for seg in segments:
        if not (seg.answer_text or "").strip():
            out.append(seg)
            continue
        if id(seg) in kept_ids:
            out.append(seg)

    if len(out) != len(segments):
        kept_ids_log = {s.index_id for s in kept_text}
        dropped_ids = [
            s.index_id for s in text_segments if s.index_id not in kept_ids_log
        ]
        logger.info(
            "Multi-index merge: omitted %s index segment(s) (ids=%s); "
            "citation-grounded or retrieval-based filter",
            len(text_segments) - len(kept_text),
            dropped_ids,
        )
    return out


def _select_when_some_cite(
    meta: list[dict],
    global_max: Optional[float],
) -> list[RagIndexSegment]:
    """At least one segment uses [n] in the answer."""
    kept: list[RagIndexSegment] = []
    for m in meta:
        seg = m["seg"]
        text = m["text"]
        if m["has_citations"]:
            kept.append(seg)
            continue
        if m["char_len"] >= LONG_UNCITED_ANSWER_CHARS:
            kept.append(seg)
            continue
        if global_max is not None and m["max_score"] is not None:
            if m["max_score"] >= global_max * RETRIEVAL_SCORE_FRACTION_OF_BEST:
                kept.append(seg)
                continue
        if _looks_like_disclaimer_only(text):
            continue
        if m["char_len"] < SHORT_UNCITED_WHEN_OTHERS_CITE_CHARS:
            continue
        kept.append(seg)
    return kept


def _select_when_none_cite(meta: list[dict]) -> list[RagIndexSegment]:
    """No [n] in any answer — use disclaimer vs substantive contrast only."""
    if len(meta) < 2:
        return [m["seg"] for m in meta]

    flags = [_looks_like_disclaimer_only(m["text"]) for m in meta]
    if not any(flags) or all(flags):
        return [m["seg"] for m in meta]
    return [m["seg"] for m, bad in zip(meta, flags) if not bad]
