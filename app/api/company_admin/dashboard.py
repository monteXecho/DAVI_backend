"""
Company admin dashboard — stats and detail lists (scoped to users managed by this admin).
"""

import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from bson import ObjectId
from bson.errors import InvalidId

from app.deps.db import get_db
from app.api.company_admin.shared import get_admin_or_user_company_id
from app.services.document_usage import USAGE_COLLECTION, ensure_usage_indexes
from app.services.document_usage.ids import company_id_field_match
from app.services.document_chat_questions import (
    DOCUMENT_CHAT_QUESTIONS_COLLECTION,
    ensure_document_chat_question_indexes,
)
from app.services.document_chat_unanswered import (
    DOCUMENT_CHAT_UNANSWERED_COLLECTION,
    REASON_NO_CITATIONS_IN_ANSWER,
    REASON_NO_DOCUMENTS_IN_INDEX,
    ensure_document_chat_unanswered_indexes,
)
from app.services.user_activity_log import (
    USER_ACTIVITY_LOG_COLLECTION,
    ensure_user_activity_log_indexes,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["Company Admin Dashboard"])


def _require_company_admin_primary_workspace(admin_context: dict) -> None:
    """Dashboard is only for real company admins on their own workspace (not guest/teamlid mode)."""
    if admin_context.get("user_type") != "company_admin":
        raise HTTPException(
            status_code=403,
            detail="Alleen beschikbaar voor bedrijfsbeheerders",
        )
    if admin_context.get("real_admin_id") != admin_context.get("admin_id"):
        raise HTTPException(
            status_code=403,
            detail="Dashboard is niet beschikbaar in gastmodus",
        )


def _active_window_filter(company_id: str, admin_id: str, cutoff: datetime) -> Dict[str, Any]:
    return {
        "company_id": company_id,
        "added_by_admin_id": admin_id,
        "last_activity": {"$gte": cutoff},
    }


def _format_dt(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


@router.get("/active-users-count")
async def active_users_count(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db),
):
    """Count company users + company admins (created by this admin) active in the last 30 days."""
    _require_company_admin_primary_workspace(admin_context)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    cutoff = datetime.utcnow() - timedelta(days=30)

    flt = _active_window_filter(company_id, admin_id, cutoff)
    n_users = await db.company_users.count_documents(flt)
    n_admins = await db.company_admins.count_documents(flt)

    return {"count": n_users + n_admins, "days": 30}


@router.get("/active-users")
async def list_active_users(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db),
):
    """List users/admins managed by this admin with last_activity in the last 30 days."""
    _require_company_admin_primary_workspace(admin_context)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    cutoff = datetime.utcnow() - timedelta(days=30)
    flt = _active_window_filter(company_id, admin_id, cutoff)

    results: List[dict] = []

    async for doc in db.company_users.find(flt):
        roles = list(doc.get("assigned_roles") or [])
        if doc.get("is_teamlid") and "Teamlid" not in roles:
            roles.append("Teamlid")
        role_label = ", ".join(roles) if roles else "Geen rol"

        results.append(
            {
                "id": doc.get("user_id"),
                "name": doc.get("name") or "",
                "email": doc.get("email") or "",
                "last_activity": _format_dt(doc.get("last_activity")),
                "role_label": role_label,
                "kind": "company_user",
            }
        )

    async for doc in db.company_admins.find(flt):
        results.append(
            {
                "id": doc.get("user_id"),
                "name": doc.get("name") or "",
                "email": doc.get("email") or "",
                "last_activity": _format_dt(doc.get("last_activity")),
                "role_label": "Beheerder",
                "kind": "company_admin",
            }
        )

    results.sort(key=lambda x: (x.get("name") or "").lower())

    return {"users": results, "days": 30}


async def _folder_names_for_company(db, company_id: Any, admin_id: str) -> List[str]:
    """
    Folder labels (`documents.upload_type`) for *this admin's* workspace only.

    Must filter `folders` by `admin_id`. Otherwise the dashboard lists every
    company's folder name and shows documents from deleted/other-admin folders.

    Fallback distinct is scoped to `user_id` == admin_id (folder docs are owned
    by the uploader admin) so we do not resurrect stale upload_type values from
    other admins or orphaned rows.
    """
    cid_match = company_id_field_match(company_id)
    names: List[str] = []
    async for f in db.folders.find({**cid_match, "admin_id": admin_id}, {"name": 1}):
        n = f.get("name")
        if n:
            names.append(n)
    if names:
        return names

    try:
        raw = await db.documents.distinct(
            "upload_type",
            {
                **cid_match,
                "user_id": admin_id,
                "upload_type": {"$ne": "document"},
            },
        )
    except Exception:
        raw = []
    out: List[str] = []
    for x in raw or []:
        if isinstance(x, str) and x.strip() and x != "document":
            out.append(x.strip())
    return out


async def _documents_in_use_filter(
    db,
    company_id: Any,
    admin_id: str,
    *,
    updated_by_me: bool,
    role_name: Optional[str],
    folder_name: Optional[str],
    q: Optional[str],
) -> Dict[str, Any]:
    """
    Role/folder documents (upload_type = folder name), excluding private uploads (upload_type=document).
    When updated_by_me is True, only rows uploaded/last replaced by this admin (user_id on the document).
    """
    folder_names = await _folder_names_for_company(db, company_id, admin_id)
    if not folder_names:
        return {"_id": {"$exists": False}}

    flt: Dict[str, Any] = {
        **company_id_field_match(company_id),
        "upload_type": {"$in": folder_names},
    }
    if updated_by_me:
        flt["user_id"] = admin_id

    if folder_name and folder_name.strip():
        fn = folder_name.strip()
        if fn in folder_names:
            flt["upload_type"] = fn
        else:
            return {"_id": {"$exists": False}}

    if role_name and role_name.strip():
        role_doc = await db.roles.find_one(
            {
                **company_id_field_match(company_id),
                "added_by_admin_id": admin_id,
                "name": role_name,
            }
        )
        if not role_doc:
            return {"_id": {"$exists": False}}
        role_folders = list(role_doc.get("folders") or [])
        if role_folders:
            allowed = [fn for fn in role_folders if fn in folder_names]
            if allowed:
                if isinstance(flt["upload_type"], str):
                    if flt["upload_type"] not in allowed:
                        return {"_id": {"$exists": False}}
                else:
                    flt["upload_type"] = {"$in": allowed}
            else:
                return {"_id": {"$exists": False}}
        else:
            return {"_id": {"$exists": False}}

    if q and q.strip():
        flt["file_name"] = {"$regex": re.escape(q.strip()), "$options": "i"}

    return flt


def _doc_to_row(doc: dict, usage: Optional[Dict[str, Any]] = None) -> dict:
    ts = doc.get("updated_at") or doc.get("created_at")
    usage = usage or {}
    count = int(usage.get("count", 0) or 0)
    last_at = usage.get("last_at")
    return {
        "id": str(doc.get("_id", "")),
        "folder_name": doc.get("upload_type") or "",
        "file_name": doc.get("file_name") or "",
        "updated_at": _format_dt(ts),
        "answer_usage_count": count,
        "last_answer_at": _format_dt(last_at) if last_at else None,
        "path": doc.get("path") or "",
    }


async def _usage_stats_for_document_ids(
    db, company_id: Any, doc_ids: List[str]
) -> Dict[str, Dict[str, Any]]:
    if not doc_ids:
        return {}
    try:
        await ensure_usage_indexes(db)
    except Exception:
        pass
    coll = db[USAGE_COLLECTION]
    match: Dict[str, Any] = {
        **company_id_field_match(company_id),
        "document_id": {"$in": doc_ids},
    }
    pipeline = [
        {"$match": match},
        {
            "$group": {
                "_id": "$document_id",
                "count": {"$sum": 1},
                "last_at": {"$max": "$at"},
            }
        },
    ]
    out: Dict[str, Dict[str, Any]] = {}
    async for row in coll.aggregate(pipeline):
        did = row.get("_id")
        if did is None:
            continue
        key = str(did)
        out[key] = {
            "count": int(row.get("count", 0) or 0),
            "last_at": row.get("last_at"),
        }
    return out


def _merge_usage_stats_for_doc_ids(
    stats_map: Dict[str, Dict[str, Any]], oid_list: List[Any]
) -> Dict[str, Any]:
    """Sum citation counts and take latest `last_at` across duplicate document rows."""
    total_c = 0
    last_at = None
    for oid in oid_list:
        s = stats_map.get(str(oid))
        if not s:
            continue
        total_c += int(s.get("count", 0) or 0)
        la = s.get("last_at")
        if la is not None and (last_at is None or la > last_at):
            last_at = la
    return {"count": total_c, "last_at": last_at}


async def _document_id_strings_same_logical_file(
    db, company_id: Any, doc: dict
) -> List[str]:
    """
    All MongoDB `documents._id` rows for the same folder file: case-insensitive file name
    and same `upload_type` (map), across all users. Folder documents are shared across
    company users; usage history is merged for all matching rows.
    """
    fn = (doc.get("file_name") or "").lower()
    ut = doc.get("upload_type")
    flt: Dict[str, Any] = {
        **company_id_field_match(company_id),
        "upload_type": ut,
        "$expr": {
            "$eq": [
                {"$toLower": {"$ifNull": ["$file_name", ""]}},
                fn,
            ]
        },
    }
    out: List[str] = []
    async for row in db.documents.find(flt, {"_id": 1}):
        out.append(str(row["_id"]))
    return out


async def _count_documents_in_use_deduped(db, flt: Dict[str, Any]) -> int:
    """One row per folder + file name (case-insensitive), merged across company users."""
    pipeline: List[Dict[str, Any]] = [
        {"$match": flt},
        {
            "$addFields": {
                "_fn": {"$toLower": {"$ifNull": ["$file_name", ""]}},
            }
        },
        {
            "$group": {
                "_id": {
                    "fn": "$_fn",
                    "ut": "$upload_type",
                }
            }
        },
        {"$count": "total"},
    ]
    rows = await db.documents.aggregate(pipeline).to_list(1)
    if not rows:
        return 0
    return int(rows[0].get("total", 0) or 0)


async def _list_documents_in_use_deduped_page(
    db, flt: Dict[str, Any], skip: int, limit: int
) -> List[dict]:
    """
    One row per folder + file name; `_all_ids` lists every MongoDB documents._id merged
    (same file assigned to multiple users / duplicate rows). Representative doc = most recently updated.
    """
    pipeline: List[Dict[str, Any]] = [
        {"$match": flt},
        {
            "$addFields": {
                "_fn": {"$toLower": {"$ifNull": ["$file_name", ""]}},
                "_sort_ts": {"$ifNull": ["$updated_at", "$created_at"]},
            }
        },
        {"$sort": {"_sort_ts": -1, "_id": -1}},
        {
            "$group": {
                "_id": {
                    "fn": "$_fn",
                    "ut": "$upload_type",
                },
                "doc": {"$first": "$$ROOT"},
                "all_ids": {"$push": "$_id"},
            }
        },
        {
            "$replaceRoot": {
                "newRoot": {
                    "$mergeObjects": [
                        "$doc",
                        {"_all_ids": "$all_ids"},
                    ]
                }
            }
        },
        {"$sort": {"created_at": -1, "_id": -1}},
        {"$skip": skip},
        {"$limit": limit},
    ]
    out: List[dict] = []
    async for doc in db.documents.aggregate(pipeline):
        out.append(doc)
    return out


def _cutoff_documents_older_than_2_years() -> datetime:
    """UTC threshold: document last touched at or before this instant counts as >2 years old."""
    return datetime.utcnow() - timedelta(days=730)


async def _count_documents_older_than_2y_deduped(
    db, flt: Dict[str, Any], cutoff: datetime
) -> int:
    """
    Deduped folder files (same as documents-in-use) where the group's latest
    updated_at/created_at is at or before `cutoff`.
    """
    pipeline: List[Dict[str, Any]] = [
        {"$match": flt},
        {
            "$addFields": {
                "_fn": {"$toLower": {"$ifNull": ["$file_name", ""]}},
                "_ts": {"$ifNull": ["$updated_at", "$created_at"]},
            }
        },
        {"$match": {"_ts": {"$ne": None}}},
        {"$sort": {"_ts": -1}},
        {
            "$group": {
                "_id": {"fn": "$_fn", "ut": "$upload_type"},
                "max_ts": {"$first": "$_ts"},
            }
        },
        {"$match": {"max_ts": {"$lte": cutoff}}},
        {"$count": "total"},
    ]
    rows = await db.documents.aggregate(pipeline).to_list(1)
    if not rows:
        return 0
    return int(rows[0].get("total", 0) or 0)


async def _list_documents_older_than_2y_deduped_page(
    db, flt: Dict[str, Any], cutoff: datetime, skip: int, limit: int
) -> List[dict]:
    pipeline: List[Dict[str, Any]] = [
        {"$match": flt},
        {
            "$addFields": {
                "_fn": {"$toLower": {"$ifNull": ["$file_name", ""]}},
                "_ts": {"$ifNull": ["$updated_at", "$created_at"]},
            }
        },
        {"$match": {"_ts": {"$ne": None}}},
        {"$sort": {"_ts": -1}},
        {
            "$group": {
                "_id": {"fn": "$_fn", "ut": "$upload_type"},
                "max_ts": {"$first": "$_ts"},
                "doc": {"$first": "$$ROOT"},
                "all_ids": {"$push": "$_id"},
            }
        },
        {"$match": {"max_ts": {"$lte": cutoff}}},
        {"$sort": {"max_ts": 1}},
        {"$skip": skip},
        {"$limit": limit},
    ]
    out: List[dict] = []
    async for row in db.documents.aggregate(pipeline):
        d = row.get("doc") or {}
        all_ids = row.get("all_ids") or []
        merged = {**d, "_all_ids": all_ids}
        out.append(merged)
    return out


@router.get("/documents-in-use-count")
async def documents_in_use_count(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db),
    updated_by_me: bool = Query(
        True,
        description="When true (default), only documents owned by this admin (user_id), matching Documenten / get_admin_documents. Set false to include all folder-document rows for the company (e.g. extra rows per assigned user).",
    ),
):
    """Count folder-linked documents; default scope matches the Documenten page (this admin's uploads)."""
    _require_company_admin_primary_workspace(admin_context)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    flt = await _documents_in_use_filter(
        db,
        company_id,
        admin_id,
        updated_by_me=updated_by_me,
        role_name=None,
        folder_name=None,
        q=None,
    )
    n = await _count_documents_in_use_deduped(db, flt)
    return {"count": n}


@router.get("/documents-in-use")
async def documents_in_use(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    role: Optional[str] = Query(None),
    folder: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    updated_by_me: bool = Query(
        True,
        description="Same as documents-in-use-count: default only this admin's folder documents.",
    ),
):
    """Paginated list of documents in use; default scope matches Documenten (this admin's uploads)."""
    _require_company_admin_primary_workspace(admin_context)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    flt = await _documents_in_use_filter(
        db,
        company_id,
        admin_id,
        updated_by_me=updated_by_me,
        role_name=role,
        folder_name=folder,
        q=q,
    )

    total = await _count_documents_in_use_deduped(db, flt)
    skip = (page - 1) * page_size

    docs_raw = await _list_documents_in_use_deduped_page(db, flt, skip, page_size)

    flat_ids: List[str] = []
    for d in docs_raw:
        for oid in d.get("_all_ids") or []:
            flat_ids.append(str(oid))
    stats_map = await _usage_stats_for_document_ids(db, company_id, flat_ids)

    documents: List[dict] = []
    for d in docs_raw:
        all_ids = d.pop("_all_ids", None) or []
        for _k in ("_fn", "_sort_ts"):
            d.pop(_k, None)
        merged = _merge_usage_stats_for_doc_ids(stats_map, all_ids)
        documents.append(_doc_to_row(d, merged))

    role_options: List[str] = []
    cid_match = company_id_field_match(company_id)
    async for r in db.roles.find(
        {**cid_match, "added_by_admin_id": admin_id}, {"name": 1}
    ).sort("name", 1):
        rn = r.get("name")
        if rn:
            role_options.append(rn)

    folder_options = sorted(await _folder_names_for_company(db, company_id, admin_id))

    return {
        "documents": documents,
        "total": total,
        "role_options": role_options,
        "folder_options": folder_options,
    }


@router.get("/documents-older-than-2y-count")
async def documents_older_than_2y_count(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db),
    updated_by_me: bool = Query(
        True,
        description="Same scope as documents-in-use-count (this admin's folder uploads by default).",
    ),
):
    """Count deduped folder documents whose last update (or creation) is older than ~2 years."""
    _require_company_admin_primary_workspace(admin_context)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    cutoff = _cutoff_documents_older_than_2_years()

    flt = await _documents_in_use_filter(
        db,
        company_id,
        admin_id,
        updated_by_me=updated_by_me,
        role_name=None,
        folder_name=None,
        q=None,
    )
    n = await _count_documents_older_than_2y_deduped(db, flt, cutoff)
    return {"count": n}


@router.get("/documents-older-than-2y")
async def documents_older_than_2y_list(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    q: Optional[str] = Query(None),
    updated_by_me: bool = Query(
        True,
        description="Same scope as documents-in-use list.",
    ),
):
    """Paginated list of folder documents not updated in ~2 years (deduped per map + file name)."""
    _require_company_admin_primary_workspace(admin_context)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    cutoff = _cutoff_documents_older_than_2_years()

    flt = await _documents_in_use_filter(
        db,
        company_id,
        admin_id,
        updated_by_me=updated_by_me,
        role_name=None,
        folder_name=None,
        q=q,
    )
    total = await _count_documents_older_than_2y_deduped(db, flt, cutoff)
    skip = (page - 1) * page_size

    docs_raw = await _list_documents_older_than_2y_deduped_page(
        db, flt, cutoff, skip, page_size
    )

    flat_ids: List[str] = []
    for d in docs_raw:
        for oid in d.get("_all_ids") or []:
            flat_ids.append(str(oid))
    stats_map = await _usage_stats_for_document_ids(db, company_id, flat_ids)

    documents: List[dict] = []
    for d in docs_raw:
        all_ids = d.pop("_all_ids", None) or []
        for _k in ("_fn", "_ts", "_sort_ts"):
            d.pop(_k, None)
        merged = _merge_usage_stats_for_doc_ids(stats_map, all_ids)
        documents.append(_doc_to_row(d, merged))

    return {
        "documents": documents,
        "total": total,
    }


@router.get("/documents-in-use/{document_id}/answer-usage-history")
async def document_answer_usage_history(
    document_id: str,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db),
    limit: int = Query(100, ge=1, le=500),
):
    """Events for documents cited in generated answers (newest first)."""
    _require_company_admin_primary_workspace(admin_context)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
        oid = ObjectId(document_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Ongeldig document")

    doc = await db.documents.find_one({"_id": oid, **company_id_field_match(company_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Document niet gevonden")

    folder_names = await _folder_names_for_company(db, company_id, admin_id)
    ut = doc.get("upload_type")
    if not ut or ut not in folder_names:
        raise HTTPException(status_code=403, detail="Geen toegang tot dit document")

    try:
        await ensure_usage_indexes(db)
    except Exception:
        pass

    coll = db[USAGE_COLLECTION]
    events: List[dict] = []
    doc_id_strings = await _document_id_strings_same_logical_file(db, company_id, doc)
    if not doc_id_strings:
        doc_id_strings = [document_id]
    hist_match: Dict[str, Any] = {
        **company_id_field_match(company_id),
        "document_id": {"$in": doc_id_strings},
    }
    cursor = coll.find(hist_match).sort("at", -1).limit(limit)
    async for e in cursor:
        events.append(
            {
                "action": e.get("action") or "Antwoord gegenereerd",
                "at": _format_dt(e.get("at")),
                "folder_name": e.get("folder_name") or "",
                "file_name": e.get("file_name") or "",
                "path": e.get("path") or "",
                "asker_email": e.get("asker_email") or "",
                "asker_user_id": e.get("asker_user_id") or "",
                "event_type": e.get("event_type") or "answer_citation",
            }
        )

    return {
        "events": events,
        "file_name": doc.get("file_name") or "",
    }


def _utc_month_bounds(now: datetime) -> tuple[datetime, datetime]:
    """Start of current UTC month (inclusive) and start of next month (exclusive)."""
    start = datetime(now.year, now.month, 1)
    if now.month == 12:
        end = datetime(now.year + 1, 1, 1)
    else:
        end = datetime(now.year, now.month + 1, 1)
    return start, end


def _document_chat_questions_base_filter(
    company_id: Any, admin_id: str
) -> Dict[str, Any]:
    return {**company_id_field_match(company_id), "added_by_admin_id": admin_id}


@router.get("/document-chat-questions-count")
async def document_chat_questions_count(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db),
):
    """Questions asked via Document Chat this calendar month (UTC), scoped to this admin's users."""
    _require_company_admin_primary_workspace(admin_context)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    start, end = _utc_month_bounds(datetime.utcnow())

    try:
        await ensure_document_chat_question_indexes(db)
    except Exception:
        pass

    flt: Dict[str, Any] = {
        **_document_chat_questions_base_filter(company_id, admin_id),
        "at": {"$gte": start, "$lt": end},
    }
    n = await db[DOCUMENT_CHAT_QUESTIONS_COLLECTION].count_documents(flt)
    return {"count": n, "month_start": _format_dt(start)}


@router.get("/document-chat-questions")
async def document_chat_questions_list(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    role: Optional[str] = Query(None),
    folder: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
):
    """Paginated Document Chat questions for users managed by this admin."""
    _require_company_admin_primary_workspace(admin_context)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
        await ensure_document_chat_question_indexes(db)
    except Exception:
        pass

    flt: Dict[str, Any] = _document_chat_questions_base_filter(company_id, admin_id)

    if role and role.strip():
        flt["assigned_roles_snapshot"] = role.strip()

    if folder and folder.strip():
        flt["folder_context"] = folder.strip()

    if q and q.strip():
        flt["question_text"] = {"$regex": re.escape(q.strip()), "$options": "i"}

    coll = db[DOCUMENT_CHAT_QUESTIONS_COLLECTION]
    total = await coll.count_documents(flt)
    skip = (page - 1) * page_size

    cursor = (
        coll.find(flt).sort([("at", -1), ("_id", -1)]).skip(skip).limit(page_size)
    )
    items: List[dict] = []
    async for doc in cursor:
        items.append(
            {
                "id": str(doc.get("_id", "")),
                "question_text": doc.get("question_text") or "",
                "asker_email": doc.get("asker_email") or "",
                "asker_name": doc.get("asker_name") or "",
                "asker_user_id": doc.get("asker_user_id") or "",
                "asker_is_company_admin": bool(doc.get("asker_is_company_admin")),
                "assigned_roles_snapshot": list(doc.get("assigned_roles_snapshot") or []),
                "folder_context": list(doc.get("folder_context") or []),
                "answer_preview": doc.get("answer_preview"),
                "at": _format_dt(doc.get("at")),
            }
        )

    cid_match = company_id_field_match(company_id)
    role_options: List[str] = []
    async for r in db.roles.find(
        {**cid_match, "added_by_admin_id": admin_id}, {"name": 1}
    ).sort("name", 1):
        rn = r.get("name")
        if rn:
            role_options.append(rn)

    folder_options = sorted(await _folder_names_for_company(db, company_id, admin_id))

    return {
        "questions": items,
        "total": total,
        "role_options": role_options,
        "folder_options": folder_options,
    }


def _document_chat_unanswered_base_filter(company_id: Any, admin_id: str) -> Dict[str, Any]:
    return {
        **_document_chat_questions_base_filter(company_id, admin_id),
        "reason": {
            "$in": [REASON_NO_DOCUMENTS_IN_INDEX, REASON_NO_CITATIONS_IN_ANSWER],
        },
    }


@router.get("/document-chat-unanswered-count")
async def document_chat_unanswered_count(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db),
):
    """Count questions with no document-backed answer (empty index or no citations in answer)."""
    _require_company_admin_primary_workspace(admin_context)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
        await ensure_document_chat_unanswered_indexes(db)
    except Exception:
        pass

    flt = _document_chat_unanswered_base_filter(company_id, admin_id)
    n = await db[DOCUMENT_CHAT_UNANSWERED_COLLECTION].count_documents(flt)
    return {"count": n}


@router.get("/document-chat-unanswered")
async def document_chat_unanswered_list(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    role: Optional[str] = Query(None),
    folder: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
):
    """Paginated list: no grounded sources (no index hits, or model answered without citations)."""
    _require_company_admin_primary_workspace(admin_context)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
        await ensure_document_chat_unanswered_indexes(db)
    except Exception:
        pass

    flt: Dict[str, Any] = _document_chat_unanswered_base_filter(company_id, admin_id)

    if role and role.strip():
        flt["assigned_roles_snapshot"] = role.strip()

    if folder and folder.strip():
        flt["folder_context"] = folder.strip()

    if q and q.strip():
        flt["question_text"] = {"$regex": re.escape(q.strip()), "$options": "i"}

    coll = db[DOCUMENT_CHAT_UNANSWERED_COLLECTION]
    total = await coll.count_documents(flt)
    skip = (page - 1) * page_size

    cursor = coll.find(flt).sort([("at", -1), ("_id", -1)]).skip(skip).limit(page_size)
    items: List[dict] = []
    async for doc in cursor:
        items.append(
            {
                "id": str(doc.get("_id", "")),
                "question_text": doc.get("question_text") or "",
                "asker_email": doc.get("asker_email") or "",
                "asker_name": doc.get("asker_name") or "",
                "asker_user_id": doc.get("asker_user_id") or "",
                "asker_is_company_admin": bool(doc.get("asker_is_company_admin")),
                "assigned_roles_snapshot": list(doc.get("assigned_roles_snapshot") or []),
                "folder_context": list(doc.get("folder_context") or []),
                "at": _format_dt(doc.get("at")),
            }
        )

    cid_match = company_id_field_match(company_id)
    role_options: List[str] = []
    async for r in db.roles.find(
        {**cid_match, "added_by_admin_id": admin_id}, {"name": 1}
    ).sort("name", 1):
        rn = r.get("name")
        if rn:
            role_options.append(rn)

    folder_options = sorted(await _folder_names_for_company(db, company_id, admin_id))

    return {
        "questions": items,
        "total": total,
        "role_options": role_options,
        "folder_options": folder_options,
    }


def _document_chat_questions_month_filter(
    company_id: Any, admin_id: str
) -> Dict[str, Any]:
    start, end = _utc_month_bounds(datetime.utcnow())
    return {
        **_document_chat_questions_base_filter(company_id, admin_id),
        "at": {"$gte": start, "$lt": end},
    }


async def _aggregate_top_faq_this_month(
    db, company_id: Any, admin_id: str, *, limit: int
) -> List[dict]:
    try:
        await ensure_document_chat_question_indexes(db)
    except Exception:
        pass
    flt = _document_chat_questions_month_filter(company_id, admin_id)
    pipeline: List[Dict[str, Any]] = [
        {"$match": flt},
        {
            "$addFields": {
                "q_key": {
                    "$toLower": {
                        "$trim": {"input": {"$ifNull": ["$question_text", ""]}}
                    }
                }
            }
        },
        {"$match": {"q_key": {"$ne": ""}}},
        {"$sort": {"at": -1}},
        {
            "$group": {
                "_id": "$q_key",
                "count": {"$sum": 1},
                "has_source_num": {
                    "$max": {
                        "$cond": [{"$eq": ["$has_cited_sources", True]}, 1, 0]
                    }
                },
                "display_text": {"$first": "$question_text"},
            }
        },
        {"$sort": {"count": -1, "_id": 1}},
        {"$limit": limit},
    ]
    out: List[dict] = []
    async for row in db[DOCUMENT_CHAT_QUESTIONS_COLLECTION].aggregate(pipeline):
        hs = int(row.get("has_source_num", 0) or 0) >= 1
        out.append(
            {
                "question_text": row.get("display_text") or "",
                "count": int(row.get("count", 0) or 0),
                "has_cited_sources": hs,
                "bron_status": "metBron" if hs else "zonder",
            }
        )
    return out


@router.get("/top-faq-preview")
async def top_faq_preview(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db),
):
    """#1 most frequent question this calendar month (UTC) for the dashboard tile."""
    _require_company_admin_primary_workspace(admin_context)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    start, end = _utc_month_bounds(datetime.utcnow())
    rows = await _aggregate_top_faq_this_month(db, company_id, admin_id, limit=1)
    if not rows:
        return {
            "top_question": "",
            "count": 0,
            "has_cited_sources": False,
            "bron_status": "zonder",
            "month_start": _format_dt(start),
        }
    r = rows[0]
    return {
        "top_question": r["question_text"],
        "count": r["count"],
        "has_cited_sources": r["has_cited_sources"],
        "bron_status": r["bron_status"],
        "month_start": _format_dt(start),
    }


@router.get("/top-faq")
async def top_faq_list(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db),
):
    """Top 10 most frequent questions this month with source (met bron / zonder)."""
    _require_company_admin_primary_workspace(admin_context)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    start, end = _utc_month_bounds(datetime.utcnow())
    rows = await _aggregate_top_faq_this_month(db, company_id, admin_id, limit=10)
    items: List[dict] = []
    for i, r in enumerate(rows, start=1):
        items.append(
            {
                "rank": i,
                "question_text": r["question_text"],
                "count": r["count"],
                "has_cited_sources": r["has_cited_sources"],
                "bron_status": r["bron_status"],
            }
        )
    return {"items": items, "month_start": _format_dt(start)}


def _user_activity_period_cutoff(period: str) -> Optional[datetime]:
    """None = no lower bound (caller may still cap query range)."""
    now = datetime.utcnow()
    if period == "7d":
        return now - timedelta(days=7)
    if period == "30d":
        return now - timedelta(days=30)
    if period == "90d":
        return now - timedelta(days=90)
    if period == "all":
        return None
    return now - timedelta(days=30)


async def _managed_users_name_map(
    db,
    company_id: Any,
    admin_id: str,
    *,
    include_self: bool = True,
) -> Dict[str, str]:
    """user_id -> display name for company users/admins created by this admin."""
    out: Dict[str, str] = {}
    cid = company_id_field_match(company_id)
    async for u in db.company_users.find(
        {**cid, "added_by_admin_id": admin_id}, {"user_id": 1, "name": 1}
    ):
        uid = u.get("user_id")
        if uid:
            out[str(uid)] = (u.get("name") or "").strip() or str(uid)
    async for a in db.company_admins.find(
        {**cid, "added_by_admin_id": admin_id}, {"user_id": 1, "name": 1}
    ):
        uid = a.get("user_id")
        if uid:
            out[str(uid)] = (a.get("name") or "").strip() or str(uid)
    # Include this admin so their own uploads appear (e.g. other dashboards)
    if include_self and admin_id and str(admin_id) not in out:
        adm = await db.company_admins.find_one(
            {**cid, "user_id": admin_id}, {"name": 1}
        )
        if adm:
            out[str(admin_id)] = (adm.get("name") or "").strip() or "Beheerder"
    return out


def _nearest_chat_question_text(
    asker_id: str,
    at: datetime,
    by_user: Dict[str, List[Tuple[datetime, str]]],
    *,
    max_delta_sec: float = 900.0,
) -> Optional[str]:
    """Match legacy usage rows to document_chat_questions by time + asker."""
    best_txt: Optional[str] = None
    best_d: Optional[float] = None
    for q_at, q_txt in by_user.get(str(asker_id)) or []:
        d = abs((q_at - at).total_seconds())
        if d <= max_delta_sec and (best_d is None or d < best_d):
            best_d = d
            best_txt = q_txt
    return best_txt


async def _collect_user_activity_events(
    db,
    company_id: Any,
    admin_id: str,
    *,
    period: str,
    user_sub: Optional[str],
    activity_kind: Optional[str],
) -> List[dict]:
    """
    Merge document upload/update, Document Chat usage, folder creation, and
    explicit activity-log rows (e.g. private deletes). Scoped to users managed
    by this admin (not the viewing admin). Returns one row per user (most recent activity only).
    """
    name_map = await _managed_users_name_map(
        db, company_id, admin_id, include_self=False
    )
    if not name_map:
        return []

    allowed_ids = list(name_map.keys())
    cutoff = _user_activity_period_cutoff(period)
    # Cap "all" queries to last 365 days for performance
    query_floor = cutoff if cutoff is not None else (datetime.utcnow() - timedelta(days=365))

    cid = company_id_field_match(company_id)
    doc_flt: Dict[str, Any] = {
        **cid,
        "user_id": {"$in": allowed_ids},
        "$or": [
            {"created_at": {"$gte": query_floor}},
            {"updated_at": {"$gte": query_floor}},
        ],
    }

    events: List[dict] = []
    try:
        cursor = db.documents.find(doc_flt).sort([("updated_at", -1), ("created_at", -1)]).limit(
            1500
        )
        async for doc in cursor:
            uid = str(doc.get("user_id") or "")
            who = name_map.get(uid, uid)
            fn = (doc.get("file_name") or "").strip()
            ut = doc.get("upload_type") or ""
            map_bit = f" ({ut})" if ut and ut != "document" else ""
            ca = doc.get("created_at")
            ua = doc.get("updated_at")
            if isinstance(ca, datetime) and ca >= query_floor:
                events.append(
                    {
                        "at": ca,
                        "who_id": uid,
                        "who_name": who,
                        "what": f'Uploaden "{fn}"{map_bit}'.strip(),
                        "kind": "upload",
                        "sort_ts": ca.timestamp(),
                    }
                )
            if (
                isinstance(ua, datetime)
                and isinstance(ca, datetime)
                and ua >= query_floor
                and ua > ca + timedelta(seconds=2)
            ):
                events.append(
                    {
                        "at": ua,
                        "who_id": uid,
                        "who_name": who,
                        "what": f'Aanpassen "{fn}"{map_bit}'.strip(),
                        "kind": "update",
                        "sort_ts": ua.timestamp(),
                    }
                )
    except Exception as e:
        logger.warning("user activity documents scan: %s", e)

    try:
        await ensure_usage_indexes(db)
    except Exception:
        pass
    usage_flt: Dict[str, Any] = {
        **cid,
        "asker_user_id": {"$in": allowed_ids},
        "at": {"$gte": query_floor},
    }
    usage_rows: List[dict] = []
    try:
        async for e in (
            db[USAGE_COLLECTION].find(usage_flt).sort("at", -1).limit(1500)
        ):
            uid = str(e.get("asker_user_id") or "")
            at = e.get("at")
            if not isinstance(at, datetime):
                continue
            usage_rows.append(
                {
                    "at": at,
                    "asker_user_id": uid,
                    "question_text": (e.get("question_text") or "").strip(),
                }
            )
    except Exception as ex:
        logger.warning("user activity usage scan: %s", ex)

    by_user_q: Dict[str, List[Tuple[datetime, str]]] = defaultdict(list)
    need_backfill = any(not r.get("question_text") for r in usage_rows)
    if need_backfill:
        try:
            await ensure_document_chat_question_indexes(db)
        except Exception:
            pass
        q_flt: Dict[str, Any] = {
            **cid,
            "added_by_admin_id": admin_id,
            "asker_user_id": {"$in": allowed_ids},
            "at": {"$gte": query_floor},
        }
        try:
            async for qdoc in (
                db[DOCUMENT_CHAT_QUESTIONS_COLLECTION]
                .find(q_flt)
                .sort("at", -1)
                .limit(8000)
            ):
                q_uid = str(qdoc.get("asker_user_id") or "")
                qat = qdoc.get("at")
                qt = (qdoc.get("question_text") or "").strip()
                if not isinstance(qat, datetime) or not qt:
                    continue
                by_user_q[q_uid].append((qat, qt))
        except Exception as ex:
            logger.warning("user activity chat question backfill: %s", ex)
        for uid in by_user_q:
            by_user_q[uid].sort(key=lambda x: x[0])

    for r in usage_rows:
        uid = r["asker_user_id"]
        who = name_map.get(uid, uid)
        at = r["at"]
        qt = (r.get("question_text") or "").strip()
        if not qt:
            qt = _nearest_chat_question_text(uid, at, by_user_q) or ""
        if qt:
            what = f'Stelde de vraag\n"{qt}"'
        else:
            what = "Stelde een vraag"
        events.append(
            {
                "at": at,
                "who_id": uid,
                "who_name": who,
                "what": what,
                "kind": "answer_usage",
                "sort_ts": at.timestamp(),
            }
        )

    try:
        folder_flt: Dict[str, Any] = {
            **cid,
            "admin_id": {"$in": allowed_ids},
            "created_at": {"$gte": query_floor},
        }
        async for f in (
            db.company_folders.find(folder_flt).sort("created_at", -1).limit(1500)
        ):
            uid = str(f.get("admin_id") or "")
            if uid not in name_map:
                continue
            who = name_map.get(uid, uid)
            cat = f.get("created_at")
            if not isinstance(cat, datetime):
                continue
            fname = (f.get("name") or "").strip() or "map"
            events.append(
                {
                    "at": cat,
                    "who_id": uid,
                    "who_name": who,
                    "what": f'Map aangemaakt "{fname}"',
                    "kind": "folder_create",
                    "sort_ts": cat.timestamp(),
                }
            )
    except Exception as ex:
        logger.warning("user activity folders scan: %s", ex)

    try:
        await ensure_user_activity_log_indexes(db)
    except Exception:
        pass
    log_flt: Dict[str, Any] = {
        **cid,
        "added_by_admin_id": admin_id,
        "user_id": {"$in": allowed_ids},
        "at": {"$gte": query_floor},
    }
    try:
        async for row in (
            db[USER_ACTIVITY_LOG_COLLECTION].find(log_flt).sort("at", -1).limit(1500)
        ):
            uid = str(row.get("user_id") or "")
            who = name_map.get(uid, uid)
            at = row.get("at")
            if not isinstance(at, datetime):
                continue
            kind = (row.get("kind") or "activity").strip() or "activity"
            what = (row.get("what") or "").strip() or kind
            events.append(
                {
                    "at": at,
                    "who_id": uid,
                    "who_name": who,
                    "what": what,
                    "kind": kind,
                    "sort_ts": at.timestamp(),
                }
            )
    except Exception as ex:
        logger.warning("user activity log scan: %s", ex)

    events.sort(key=lambda x: -x["sort_ts"])

    deduped: List[dict] = []
    seen_who: set = set()
    for e in events:
        wid = str(e.get("who_id") or "")
        if not wid or wid in seen_who:
            continue
        seen_who.add(wid)
        deduped.append(e)

    if user_sub and user_sub.strip():
        qn = user_sub.strip().lower()
        deduped = [
            e
            for e in deduped
            if qn in (e.get("who_name") or "").lower()
            or qn in (e.get("who_id") or "").lower()
        ]

    if activity_kind and activity_kind != "all":
        if activity_kind == "upload":
            deduped = [e for e in deduped if e.get("kind") == "upload"]
        elif activity_kind == "update":
            deduped = [e for e in deduped if e.get("kind") == "update"]
        elif activity_kind == "answer_usage":
            deduped = [e for e in deduped if e.get("kind") == "answer_usage"]
        elif activity_kind == "folder_create":
            deduped = [e for e in deduped if e.get("kind") == "folder_create"]
        elif activity_kind == "delete_private":
            deduped = [e for e in deduped if e.get("kind") == "delete_private"]
        elif activity_kind == "delete_folder":
            deduped = [e for e in deduped if e.get("kind") == "delete_folder"]

    return deduped


def _nl_document_action_label(kind: str) -> str:
    return {
        "upload": "Geupload",
        "update": "Aangepast",
        "delete_private": "Verwijderd",
    }.get(kind or "", kind or "")


def _parse_private_delete_file_name(what: str) -> str:
    if not what:
        return ""
    m = re.search(r'Privédocument verwijderd "([^"]*)"', what)
    return (m.group(1) if m else "").strip()


async def _collect_document_changes_events(
    db,
    company_id: Any,
    admin_id: str,
    *,
    period: str,
    user_sub: Optional[str],
    activity_kind: Optional[str],
) -> List[dict]:
    """
    All document upload/update events plus private-document deletes (activity log).
    No per-user dedupe — chronological list for the document-changes page.
    """
    name_map = await _managed_users_name_map(
        db, company_id, admin_id, include_self=False
    )
    if not name_map:
        return []

    allowed_ids = list(name_map.keys())
    cutoff = _user_activity_period_cutoff(period)
    query_floor = cutoff if cutoff is not None else (datetime.utcnow() - timedelta(days=365))

    cid = company_id_field_match(company_id)
    doc_flt: Dict[str, Any] = {
        **cid,
        "user_id": {"$in": allowed_ids},
        "$or": [
            {"created_at": {"$gte": query_floor}},
            {"updated_at": {"$gte": query_floor}},
        ],
    }

    events: List[dict] = []
    try:
        cursor = (
            db.documents.find(doc_flt)
            .sort([("updated_at", -1), ("created_at", -1)])
            .limit(4000)
        )
        async for doc in cursor:
            uid = str(doc.get("user_id") or "")
            who = name_map.get(uid, uid)
            fn = (doc.get("file_name") or "").strip()
            if not fn:
                continue
            ut = doc.get("upload_type") or ""
            map_bit = f" ({ut})" if ut and ut != "document" else ""
            ca = doc.get("created_at")
            ua = doc.get("updated_at")
            if isinstance(ca, datetime) and ca >= query_floor:
                what = f'Uploaden "{fn}"{map_bit}'.strip()
                events.append(
                    {
                        "at": ca,
                        "who_id": uid,
                        "who_name": who,
                        "what": what,
                        "kind": "upload",
                        "file_name": fn,
                        "action_label": _nl_document_action_label("upload"),
                        "sort_ts": ca.timestamp(),
                    }
                )
            if (
                isinstance(ua, datetime)
                and isinstance(ca, datetime)
                and ua >= query_floor
                and ua > ca + timedelta(seconds=2)
            ):
                what = f'Aanpassen "{fn}"{map_bit}'.strip()
                events.append(
                    {
                        "at": ua,
                        "who_id": uid,
                        "who_name": who,
                        "what": what,
                        "kind": "update",
                        "file_name": fn,
                        "action_label": _nl_document_action_label("update"),
                        "sort_ts": ua.timestamp(),
                    }
                )
    except Exception as e:
        logger.warning("document changes documents scan: %s", e)

    try:
        await ensure_user_activity_log_indexes(db)
    except Exception:
        pass
    log_flt: Dict[str, Any] = {
        **cid,
        "added_by_admin_id": admin_id,
        "user_id": {"$in": allowed_ids},
        "kind": "delete_private",
        "at": {"$gte": query_floor},
    }
    try:
        async for row in (
            db[USER_ACTIVITY_LOG_COLLECTION].find(log_flt).sort("at", -1).limit(4000)
        ):
            uid = str(row.get("user_id") or "")
            who = name_map.get(uid, uid)
            at = row.get("at")
            if not isinstance(at, datetime):
                continue
            what = (row.get("what") or "").strip()
            fn = _parse_private_delete_file_name(what)
            events.append(
                {
                    "at": at,
                    "who_id": uid,
                    "who_name": who,
                    "what": what or "Verwijderd",
                    "kind": "delete_private",
                    "file_name": fn or "—",
                    "action_label": _nl_document_action_label("delete_private"),
                    "sort_ts": at.timestamp(),
                }
            )
    except Exception as ex:
        logger.warning("document changes log scan: %s", ex)

    events.sort(key=lambda x: -x["sort_ts"])

    if user_sub and user_sub.strip():
        qn = user_sub.strip().lower()
        events = [
            e
            for e in events
            if qn in (e.get("who_name") or "").lower()
            or qn in (e.get("who_id") or "").lower()
        ]

    if activity_kind and activity_kind != "all":
        if activity_kind == "upload":
            events = [e for e in events if e.get("kind") == "upload"]
        elif activity_kind == "update":
            events = [e for e in events if e.get("kind") == "update"]
        elif activity_kind == "delete_private":
            events = [e for e in events if e.get("kind") == "delete_private"]

    return events


@router.get("/user-activity-count")
async def user_activity_count(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db),
    period: str = Query("30d", description="7d|30d|90d|all"),
):
    """Approximate count of activity rows (for dashboard tile / badges)."""
    _require_company_admin_primary_workspace(admin_context)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    rows = await _collect_user_activity_events(
        db,
        company_id,
        admin_id,
        period=period,
        user_sub=None,
        activity_kind="all",
    )
    return {"count": len(rows), "period": period}


@router.get("/user-activity")
async def user_activity_list(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    period: str = Query("30d", description="7d|30d|90d|all"),
    user: Optional[str] = Query(None, description="Filter by user name fragment"),
    activity: Optional[str] = Query(
        None,
        description="all|upload|update|answer_usage|folder_create|delete_private|delete_folder",
    ),
):
    """Paginated merged activity: last action per user (upload, update, chat usage, map, delete)."""
    _require_company_admin_primary_workspace(admin_context)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    kind = activity or "all"
    if kind not in (
        "all",
        "upload",
        "update",
        "answer_usage",
        "folder_create",
        "delete_private",
        "delete_folder",
    ):
        kind = "all"

    all_rows = await _collect_user_activity_events(
        db,
        company_id,
        admin_id,
        period=period,
        user_sub=user,
        activity_kind=kind,
    )
    total = len(all_rows)
    skip = (page - 1) * page_size
    page_rows = all_rows[skip : skip + page_size]

    name_map = await _managed_users_name_map(
        db, company_id, admin_id, include_self=False
    )
    user_options = sorted(set(name_map.values()), key=lambda x: x.lower())

    items: List[dict] = []
    for e in page_rows:
        items.append(
            {
                "who_name": e.get("who_name") or "",
                "who_id": e.get("who_id") or "",
                "what": e.get("what") or "",
                "when": _format_dt(e.get("at")),
                "kind": e.get("kind") or "",
            }
        )

    return {
        "items": items,
        "total": total,
        "user_options": user_options,
        "period": period,
    }


@router.get("/document-changes-count")
async def document_changes_count(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db),
    period: str = Query("30d", description="7d|30d|90d|all"),
):
    """Count of document upload/update/delete-private events in period (not deduped by user)."""
    _require_company_admin_primary_workspace(admin_context)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    rows = await _collect_document_changes_events(
        db,
        company_id,
        admin_id,
        period=period,
        user_sub=None,
        activity_kind="all",
    )
    return {"count": len(rows), "period": period}


@router.get("/document-changes")
async def document_changes_list(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    period: str = Query("30d", description="7d|30d|90d|all"),
    user: Optional[str] = Query(None, description="Filter by user name fragment"),
    activity: Optional[str] = Query(
        None,
        description="all|upload|update|delete_private",
    ),
):
    """Paginated document-only activity: uploads, updates, private deletes."""
    _require_company_admin_primary_workspace(admin_context)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    kind = activity or "all"
    if kind not in ("all", "upload", "update", "delete_private"):
        kind = "all"

    all_rows = await _collect_document_changes_events(
        db,
        company_id,
        admin_id,
        period=period,
        user_sub=user,
        activity_kind=kind,
    )
    total = len(all_rows)
    skip = (page - 1) * page_size
    page_rows = all_rows[skip : skip + page_size]

    name_map = await _managed_users_name_map(
        db, company_id, admin_id, include_self=False
    )
    user_options = sorted(set(name_map.values()), key=lambda x: x.lower())

    items: List[dict] = []
    for e in page_rows:
        items.append(
            {
                "who_name": e.get("who_name") or "",
                "who_id": e.get("who_id") or "",
                "what": e.get("what") or "",
                "when": _format_dt(e.get("at")),
                "kind": e.get("kind") or "",
                "file_name": e.get("file_name") or "",
                "action_label": e.get("action_label") or "",
            }
        )

    return {
        "items": items,
        "total": total,
        "user_options": user_options,
        "period": period,
    }
