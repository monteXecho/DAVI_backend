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
    "CreatieChat": {"desc": "Creatieve schrijfassistent met AI-ondersteuning voor het schrijven, herschrijven en brainstormen.", "enabled": False},
    "WebChat": {"desc": "AI-zoek & Q&A over gecureerde websites en HTML bronnen.", "enabled": False},
    "PublicChat": {"desc": "Publieke chat module voor niet-geregistreerde gebruikers met toegang tot URL, HTML en document bronnen.", "enabled": False},
    "Admin Dashboard": {"desc": "Admin dashboard voor beheer en overzicht.", "enabled": False},
    "Webcrawler": {"desc": "Web crawler functionaliteit.", "enabled": False},
    "Nextcloud": {"desc": "Nextcloud integratie.", "enabled": False}
}


def serialize_modules(modules: dict) -> list:
    """Serialize module dictionary to list format.
    
    Handles multiple data formats for backward compatibility:
    - New format: {"module_name": {"desc": "...", "enabled": True}}
    - Old format: {"module_name": {"enabled": True}} (no desc)
    - Simple format: {"module_name": True} (just boolean)
    """
    result = []
    for k, v in modules.items():
        # Handle different data formats
        if isinstance(v, bool):
            # Simple format: {"module_name": True}
            enabled = v
            desc = DEFAULT_MODULES.get(k, {}).get("desc", "")
        elif isinstance(v, dict):
            # Dict format: {"module_name": {"desc": "...", "enabled" : True}}
            enabled = v.get("enabled", False)
            # Try to get desc from the value, fallback to DEFAULT_MODULES, then empty string
            desc = v.get("desc") or DEFAULT_MODULES.get(k, {}).get("desc", "")
        else:
            # Fallback for unexpected formats
            enabled = bool(v)
            desc = DEFAULT_MODULES.get(k, {}).get("desc", "")
        
        result.append({
            "name": k,
            "desc": desc,
            "enabled": enabled
        })
    return result


def serialize_documents(docs_cursor, user_id: str):
    """Serialize documents cursor to list format."""
    return [
        {
            "file_name": d["file_name"],
            "file_url": f"{BASE_DOC_URL}/{user_id}/{d['file_name']}",
        }
        for d in docs_cursor
    ]

