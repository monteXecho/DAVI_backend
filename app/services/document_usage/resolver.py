"""
Resolve a `documents` collection row from RAG citation metadata.

Primary key: `file_id` format from indexing:
  documentchat-{company_uuid}-{admin_or_user_uuid}--{filename}

Path-based lookup is a fallback (host vs container path differences).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Optional

from app.services.document_usage.ids import company_id_field_match, normalize_company_id_str
from app.services.document_usage.path_normalizer import (
    folder_name_from_storage_path,
    path_lookup_variants,
)

logger = logging.getLogger(__name__)

# documentchat-<company_uuid>-<owner_uuid>--<filename.pdf>
_FILE_ID_RE = re.compile(
    r"^documentchat-"
    r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})-"
    r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})"
    r"--(.+)$",
    re.IGNORECASE,
)


async def resolve_document_for_citation(
    db,
    company_id: Any,
    meta: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    Find the MongoDB documents row for this citation.

    Works when the asker is a company user (not the uploader): resolution uses
    file_id (owner uuid + file name), not only path equality.
    """
    file_id = (meta.get("file_id") or "").strip()
    file_name_meta = (meta.get("file_name") or "").strip()
    original_path = (meta.get("original_file_path") or "").strip()

    fn_from_meta = os.path.basename(file_name_meta) if file_name_meta else ""

    # --- 1) file_id (most reliable for role-based / admin-uploaded docs) ---
    m = _FILE_ID_RE.match(file_id)
    if m:
        cid_in_id, owner_id, fn_raw = m.group(1), m.group(2), m.group(3)
        fn = os.path.basename(fn_raw.strip())
        cid_canon = normalize_company_id_str(company_id) or str(company_id)
        if cid_in_id.lower() != cid_canon.lower():
            logger.warning(
                "file_id company_id mismatch: token=%s db=%s file_id=%s",
                cid_in_id,
                company_id,
                file_id[:80],
            )

        q = {**company_id_field_match(company_id), "user_id": owner_id, "file_name": fn}
        rec = await db.documents.find_one(q)
        if rec:
            return rec

        folder_hint = folder_name_from_storage_path(original_path)
        if folder_hint:
            rec = await db.documents.find_one(
                {
                    **company_id_field_match(company_id),
                    "user_id": owner_id,
                    "file_name": fn,
                    "upload_type": folder_hint,
                }
            )
            if rec:
                return rec

        logger.warning(
            "file_id parse ok but no Mongo match: owner=%s file=%s",
            owner_id,
            fn,
        )

    # --- 2) path variants (host vs container) ---
    for p in path_lookup_variants(original_path):
        rec = await db.documents.find_one({**company_id_field_match(company_id), "path": p})
        if rec:
            return rec

    # --- 3) file_name only if unambiguous ---
    fn = fn_from_meta or (os.path.basename(file_id.split("--")[-1]) if "--" in file_id else "")
    if not fn:
        return None

    cid_f = company_id_field_match(company_id)
    count = await db.documents.count_documents({**cid_f, "file_name": fn})
    if count == 1:
        return await db.documents.find_one({**cid_f, "file_name": fn})
    if count > 1:
        folder_hint = folder_name_from_storage_path(original_path)
        if folder_hint:
            rec = await db.documents.find_one(
                {
                    **cid_f,
                    "file_name": fn,
                    "upload_type": folder_hint,
                }
            )
            if rec:
                return rec
        logger.warning(
            "Ambiguous file_name=%s count=%s in company %s — skipping usage event",
            fn,
            count,
            company_id,
        )
    return None
