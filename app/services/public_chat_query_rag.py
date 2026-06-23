"""
RAG execution and source metadata for QR-Chat query history.

Supports storing which file_ids were queried and admin per-question source correction
(re-run RAG with a narrowed file_id filter).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from bson import ObjectId
from fastapi import HTTPException

from app.api.rag import build_rag_file_id, rag_query
from app.services.multi_index_answer_merge import answer_matches_documents_no_information_disclaimer

logger = logging.getLogger(__name__)


def _public_chat_index_id(company_id: str, admin_id: str, chat_name: str) -> str:
    from app.api.company_admin.public_chat import get_public_chat_index_id

    return get_public_chat_index_id(company_id, admin_id, chat_name)


def answer_is_meaningful(answer_text: Optional[str]) -> bool:
    if answer_text is None:
        return False
    s = str(answer_text).strip()
    if not s:
        return False
    if s.lower() == "no answer generated":
        return False
    return True


def source_file_id(source: dict, index_id: str) -> Optional[str]:
    logical = (source.get("rag_logical_file_name") or "").strip() or (source.get("file_name") or "").strip()
    if not logical:
        return None
    return build_rag_file_id(index_id, logical)


def source_snapshot(source: dict, index_id: str) -> Dict[str, Any]:
    return {
        "source_id": str(source["_id"]),
        "type": source.get("type", ""),
        "file_name": source.get("file_name", ""),
        "url": source.get("url"),
        "file_id": source_file_id(source, index_id),
    }


def _file_id_logical_name(file_id: str) -> Optional[str]:
    if file_id and "--" in file_id:
        return file_id.split("--", 1)[1]
    return file_id or None


def file_names_from_pass_ids(pass_ids: List[str]) -> List[str]:
    names: List[str] = []
    for fid in pass_ids:
        logical = _file_id_logical_name(fid)
        if logical:
            names.append(logical)
    return names


def normalize_public_chat_question(question: str) -> str:
    """Canonical form for exact-match admin correction lookup (trim + collapse whitespace)."""
    return " ".join((question or "").split())


async def find_admin_corrected_pass_ids_for_question(
    db,
    *,
    company_id: str,
    admin_id: str,
    chat_id: str,
    question: str,
) -> Optional[Dict[str, Any]]:
    """
    If an admin corrected sources for this exact question, return the narrowed pass_ids
    to use on the public chat page (most recent correction wins).
    """
    q = normalize_public_chat_question(question)
    if not q:
        return None

    cursor = db.public_chat_query_history.find(
        {
            "company_id": company_id,
            "admin_id": admin_id,
            "chat_id": chat_id,
            "corrected_pass_ids": {"$exists": True, "$ne": []},
        },
        sort=[("sources_corrected_at", -1)],
    ).limit(200)

    docs = await cursor.to_list(length=200)
    for doc in docs:
        stored_q = doc.get("question") or ""
        if normalize_public_chat_question(stored_q) != q and stored_q.strip() != q:
            continue
        pass_ids = [p for p in (doc.get("corrected_pass_ids") or []) if p]
        if not pass_ids:
            continue
        return {
            "corrected_pass_ids": pass_ids,
            "corrected_sources": doc.get("corrected_sources") or [],
            "history_id": str(doc["_id"]),
        }
    return None


def source_matches_file_id(source: dict, file_id: str) -> bool:
    source_file_name = source.get("file_name", "")
    source_rag_logical = (source.get("rag_logical_file_name") or "").strip()
    logical = _file_id_logical_name(file_id)
    if not logical:
        return source_file_name == file_id
    return source_file_name == logical or source_rag_logical == logical or source_file_name == file_id


def build_linked_sources_from_rag(
    sources: List[dict],
    used_file_ids: Set[str],
    normalized_docs: List[dict],
    index_id: str,
) -> List[Dict[str, Any]]:
    """Map RAG-retrieved file_ids back to chat source snapshots (with source_id)."""
    linked: List[Dict[str, Any]] = []
    seen_source_ids: Set[str] = set()

    for s in sources:
        source_file_name = s.get("file_name", "")
        matches = False
        for uid in used_file_ids:
            if source_matches_file_id(s, uid):
                matches = True
                break
        if not matches:
            for doc in normalized_docs:
                if doc.get("meta", {}).get("file_name") == source_file_name:
                    matches = True
                    break
        if not matches:
            continue
        sid = str(s["_id"])
        if sid in seen_source_ids:
            continue
        seen_source_ids.add(sid)
        linked.append(source_snapshot(s, index_id))

    return linked


def classify_answer(answer_text: str, linked_source_count: int) -> Tuple[bool, Optional[str]]:
    shown_sources = linked_source_count > 0
    has_meaningful = answer_is_meaningful(answer_text)
    no_info_reply = answer_matches_documents_no_information_disclaimer(answer_text)
    has_ans = has_meaningful and shown_sources and not no_info_reply
    if has_ans:
        return True, None
    if no_info_reply:
        return False, "Geen relevante informatie in de aangeleverde documenten"
    if not shown_sources:
        return False, "Geen gekoppelde bron voor dit antwoord"
    return False, "Geen inhoudelijk antwoord gegenereerd"


def parse_rag_payload(rag_result: dict) -> Tuple[str, List[dict]]:
    answer_data = (
        rag_result.get("result", [{}])[1]
        if isinstance(rag_result.get("result"), list)
        else rag_result.get("result", {})
    )
    answer_text = answer_data.get("data", "No answer generated")
    raw_docs = answer_data.get("documents", []) or rag_result.get("documents", []) or []
    return answer_text, raw_docs


def normalize_rag_documents(raw_docs: List[dict], sources: List[dict]) -> Tuple[List[dict], Set[str]]:
    normalized_docs: List[dict] = []
    used_file_ids: Set[str] = set()

    for doc in raw_docs:
        meta = doc.get("meta", {})
        file_id = meta.get("file_id", "")
        file_path = meta.get("file_path", "")
        file_name = meta.get("file_name", "")
        source_info: dict = {}

        for s in sources:
            if source_matches_file_id(s, file_id) or s.get("file_name") == file_name:
                source_info = s
                break

        if file_id:
            used_file_ids.add(file_id)

        source_url_val = meta.get("source_url") or source_info.get("url", "")
        source_title_val = meta.get("source_title") or source_info.get("title", "")
        if source_url_val and not source_title_val:
            source_title_val = source_url_val

        original_path = meta.get("original_file_path") or source_info.get("file_path", "")

        normalized_docs.append(
            {
                "content": doc.get("content", ""),
                "meta": {
                    "file_id": file_id,
                    "file_path": source_info.get("file_path", "") if source_info else file_path,
                    "original_file_path": original_path,
                    "page_number": meta.get("page_number", 1),
                    "file_name": file_name,
                    "type": source_info.get("type", ""),
                    "url": source_url_val,
                    "score": meta.get("score"),
                    "source_url": source_url_val,
                    "source_title": source_title_val or source_info.get("file_name", ""),
                },
            }
        )

    return normalized_docs, used_file_ids


async def run_public_chat_rag(
    *,
    question: str,
    company_id: str,
    admin_id: str,
    chat_name: str,
    pass_file_ids: List[str],
    sources: List[dict],
) -> Dict[str, Any]:
    """Execute RAG for one QR-Chat question with an explicit file_id filter."""
    if not pass_file_ids:
        raise HTTPException(status_code=400, detail="Geen bronnen geselecteerd voor deze vraag")

    index_id = _public_chat_index_id(company_id, admin_id, chat_name)
    file_names = []
    for fid in pass_file_ids:
        logical = _file_id_logical_name(fid)
        if logical:
            file_names.append(logical)

    rag_result = await rag_query(
        pass_ids=",".join(pass_file_ids),
        question=question,
        file_names=file_names,
        company_id=company_id,
        index_id=index_id,
    )

    answer_text, raw_docs = parse_rag_payload(rag_result)
    normalized_docs, used_file_ids = normalize_rag_documents(raw_docs, sources)
    rag_sources = build_linked_sources_from_rag(sources, used_file_ids, normalized_docs, index_id)
    linked_source_count = len(rag_sources)
    has_answer, error_detail = classify_answer(answer_text, linked_source_count)

    return {
        "answer": answer_text,
        "has_answer": has_answer,
        "error_detail": error_detail,
        "linked_source_count": linked_source_count,
        "rag_pass_ids": list(pass_file_ids),
        "rag_sources": rag_sources,
        "documents": normalized_docs,
    }


async def resolve_active_chat_sources_by_ids(
    db,
    *,
    company_id: str,
    admin_id: str,
    chat_id: str,
    source_ids: List[str],
) -> Tuple[List[dict], List[str], List[Dict[str, Any]]]:
    """
    Validate source_ids belong to this chat (active only).
    Returns (source docs, pass file_ids, source snapshots).
    """
    if not source_ids:
        raise HTTPException(status_code=400, detail="Selecteer minimaal één bron")

    unique_ids = list(dict.fromkeys(sid.strip() for sid in source_ids if sid and str(sid).strip()))
    if not unique_ids:
        raise HTTPException(status_code=400, detail="Selecteer minimaal één bron")

    object_ids = []
    for sid in unique_ids:
        try:
            object_ids.append(ObjectId(sid))
        except Exception:
            raise HTTPException(status_code=400, detail=f"Ongeldige bron-id: {sid}")

    chat = await db.public_chats.find_one(
        {"_id": ObjectId(chat_id), "company_id": company_id, "admin_id": admin_id}
    )
    if not chat:
        raise HTTPException(status_code=404, detail="Public chat not found")

    chat_name = chat.get("chat_name", "")
    index_id = _public_chat_index_id(company_id, admin_id, chat_name)

    cursor = db.public_chat_sources.find(
        {
            "_id": {"$in": object_ids},
            "company_id": company_id,
            "admin_id": admin_id,
            "chat_id": chat_id,
            "status": "active",
        }
    )
    found = await cursor.to_list(length=len(object_ids))
    if len(found) != len(object_ids):
        raise HTTPException(
            status_code=400,
            detail="Een of meer bronnen horen niet bij deze QR-Chat of zijn niet actief",
        )

    by_id = {str(doc["_id"]): doc for doc in found}
    ordered_sources = [by_id[sid] for sid in unique_ids if sid in by_id]

    pass_ids: List[str] = []
    snapshots: List[Dict[str, Any]] = []
    for source in ordered_sources:
        fid = source_file_id(source, index_id)
        if not fid:
            raise HTTPException(
                status_code=400,
                detail=f"Bron kan niet worden gekoppeld aan RAG: {source.get('file_name') or source.get('url')}",
            )
        if fid not in pass_ids:
            pass_ids.append(fid)
        snapshots.append(source_snapshot(source, index_id))

    return ordered_sources, pass_ids, snapshots


def serialize_history_row(doc: dict) -> Dict[str, Any]:
    dt = doc.get("created_at")
    corrected_at = doc.get("sources_corrected_at")
    rag_sources = doc.get("rag_sources") or []
    corrected_sources = doc.get("corrected_sources")
    display_sources = corrected_sources if corrected_sources is not None else rag_sources

    return {
        "id": str(doc["_id"]),
        "question": doc.get("question", ""),
        "answer": doc.get("answer"),
        "has_answer": doc.get("has_answer", False),
        "error_detail": doc.get("error_detail"),
        "linked_source_count": doc.get("linked_source_count"),
        "created_at": dt.isoformat() if hasattr(dt, "isoformat") else None,
        "rag_pass_ids": doc.get("rag_pass_ids") or [],
        "rag_sources": rag_sources,
        "corrected_pass_ids": doc.get("corrected_pass_ids"),
        "corrected_sources": corrected_sources,
        "display_sources": display_sources,
        "sources_corrected_at": corrected_at.isoformat() if hasattr(corrected_at, "isoformat") else None,
        "sources_corrected_by": doc.get("sources_corrected_by"),
        "answer_from_correction": bool(doc.get("answer_from_correction")),
    }


def history_bucket_item(item: dict, doc: dict) -> None:
    """Apply with_answer / without_answer downgrade rules to a serialized history item."""
    lc = doc.get("linked_source_count")
    downgrade_disclaimer = (
        bool(doc.get("has_answer"))
        and answer_matches_documents_no_information_disclaimer(doc.get("answer"))
    )
    downgrade_no_linked = bool(doc.get("has_answer")) and lc is not None and int(lc) == 0
    if downgrade_disclaimer or downgrade_no_linked:
        item["has_answer"] = False
        if not item.get("error_detail"):
            item["error_detail"] = (
                "Geen relevante informatie in de aangeleverde documenten"
                if downgrade_disclaimer
                else "Geen gekoppelde bron voor dit antwoord"
            )
