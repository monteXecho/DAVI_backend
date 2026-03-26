"""Normalize company_id for usage events and build Mongo matches across BSON variants."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def normalize_company_id_str(value: Any) -> Optional[str]:
    """
    Canonical string form for persisting company_id on usage events.

    Admin and company user records sometimes store the same logical id as str,
    uuid.UUID, or BSON Binary subtype 4; normalizing on write avoids aggregate
    mismatches when the dashboard reads from a different collection.
    """
    if value is None:
        return None
    try:
        from uuid import UUID

        if isinstance(value, UUID):
            return str(value)
    except Exception:
        pass
    try:
        from bson.binary import Binary

        if isinstance(value, Binary) and getattr(value, "subtype", None) == 4:
            return str(value.as_uuid())
    except Exception:
        pass
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            from uuid import UUID

            return str(UUID(s))
        except Exception:
            return s
    try:
        from uuid import UUID

        return str(UUID(str(value).strip()))
    except Exception:
        s = str(value).strip()
        return s or None


def company_id_field_match(company_id: Any) -> Dict[str, Any]:
    """
    Match usage rows for this company even when older events used non-string BSON.

    New events store canonical strings from normalize_company_id_str; legacy rows
    may still use the raw value from the asker/admin record.
    """
    variants: List[Any] = []
    seen: set = set()

    def add(v: Any) -> None:
        if v is None:
            return
        key = repr(type(v)), str(v)
        if key in seen:
            return
        seen.add(key)
        variants.append(v)

    add(company_id)
    canon = normalize_company_id_str(company_id)
    if canon:
        add(canon)
    if isinstance(company_id, str):
        s = company_id.strip()
        if s and s != canon:
            add(s)

    try:
        from uuid import UUID

        u = UUID(canon) if canon else UUID(str(company_id).strip())
        try:
            from bson.binary import Binary

            if hasattr(Binary, "from_uuid"):
                add(Binary.from_uuid(u))
        except Exception:
            pass
    except Exception:
        pass

    if not variants:
        return {"company_id": company_id}
    if len(variants) == 1:
        return {"company_id": variants[0]}
    return {"company_id": {"$in": variants}}
