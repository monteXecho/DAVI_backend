"""
Constants and utility functions for repositories.
"""

import os

# Base path for all uploads
UPLOAD_ROOT = "/app/uploads/documents"

# Ensure root folder exists at startup
os.makedirs(UPLOAD_ROOT, exist_ok=True)

BASE_DOC_URL = "https://your-backend.com/documents/download"

DEFAULT_MODULES = {
    "Documenten chat": {"desc": "AI-zoek & Q&A over geuploade documenten.", "enabled": False},
    "GGD Checks": {"desc": "Automatische GGD-controles, inclusief BKR-bewaking, afwijkingslogica en rapportage.", "enabled": False},
    "Admin Dashboard": {"desc": "Admin dashboard voor beheer en overzicht.", "enabled": False},
    "Webcrawler": {"desc": "Web crawler functionaliteit.", "enabled": False},
    "Nexcloud": {"desc": "Nextcloud integratie.", "enabled": False}
}


def serialize_modules(modules: dict) -> list:
    """Serialize module dictionary to list format."""
    return [{"name": k, "desc": v["desc"], "enabled": v["enabled"]} for k, v in modules.items()]


def serialize_documents(docs_cursor, user_id: str):
    """Serialize documents cursor to list format."""
    return [
        {
            "file_name": d["file_name"],
            "file_url": f"{BASE_DOC_URL}/{user_id}/{d['file_name']}",
        }
        for d in docs_cursor
    ]

