"""Normalize filesystem paths across host and container mount points."""

from __future__ import annotations

import os
from typing import List, Optional


def folder_name_from_storage_path(path: str) -> Optional[str]:
    """Parent directory name of the file (maps to Mongo upload_type for role-based docs)."""
    if not path or not path.strip():
        return None
    d = os.path.dirname(path.strip())
    if not d or d in ("/", "."):
        return None
    base = os.path.basename(d)
    return base if base else None


def path_lookup_variants(path: str) -> List[str]:
    """All path strings worth trying for Mongo `documents.path` equality."""
    if not path or not path.strip():
        return []
    p = path.strip()
    out: List[str] = [p, os.path.normpath(p)]

    replacements = (
        ("/var/opt/DAVI_backend/uploads", "/app/uploads"),
        ("/app/uploads", "/var/opt/DAVI_backend/uploads"),
    )
    for a, b in replacements:
        if a in p:
            out.append(p.replace(a, b))
        alt = p.replace(b, a) if b in p else None
        if alt and alt != p:
            out.append(alt)

    # Deduplicate, preserve order
    seen = set()
    uniq: List[str] = []
    for x in out:
        if x and x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq
