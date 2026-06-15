"""
Keep OpenSearch indexes in sync when DAVI removes uploaded content.

Call these helpers before deleting MongoDB records / disk files so RAG chunks
are removed and re-upload does not duplicate retrieval results.
"""

import logging
import os
import shutil
from collections import defaultdict
from typing import Iterable, List, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.rag import build_rag_file_id, rag_delete_entire_index, rag_remove_indexed_files

logger = logging.getLogger(__name__)

UPLOAD_ROOT = "/app/uploads"
SOURCES_UPLOAD_ROOT = os.path.join(UPLOAD_ROOT, "sources")


def documentchat_index_id(company_id: str, scope_id: str) -> str:
    """OpenSearch index for document chat (private user or role-based admin scope)."""
    return f"documentchat-{company_id}-{scope_id}"


def webchat_index_id(company_id: str, admin_id: str) -> str:
    return f"webchat-{company_id}-{admin_id}"


def publicchat_index_id(company_id: str, admin_id: str, chat_name: str) -> str:
    """Same rules as ``get_public_chat_index_id`` in public_chat router (lazy import avoids cycles)."""
    from app.api.company_admin.public_chat import get_public_chat_index_id

    return get_public_chat_index_id(company_id, admin_id, chat_name)


async def _delete_entire_index_safe(index_id: str) -> None:
    if not index_id:
        return
    try:
        await rag_delete_entire_index(index_id)
    except Exception as e:
        logger.warning("RAG delete entire index failed for %s: %s", index_id, e)


async def remove_documentchat_index(
    company_id: str,
    scope_id: str,
    file_names: Iterable[str],
) -> None:
    names = [str(name).strip() for name in file_names if name and str(name).strip()]
    if not names:
        return

    index_id = documentchat_index_id(company_id, scope_id)
    file_ids = [build_rag_file_id(index_id, name) for name in names]
    try:
        await rag_remove_indexed_files(index_id, file_ids)
    except Exception as e:
        logger.warning(
            "RAG remove failed for documentchat index=%s files=%s: %s",
            index_id,
            len(file_ids),
            e,
        )


async def remove_documentchat_for_mongo_docs(
    company_id: str,
    scope_id: str,
    documents: List[dict],
) -> None:
    file_names = [doc.get("file_name") for doc in documents if doc.get("file_name")]
    await remove_documentchat_index(company_id, scope_id, file_names)


async def remove_documentchat_for_mongo_docs_by_owner(
    company_id: str,
    documents: List[dict],
) -> None:
    by_owner: dict[str, list[str]] = defaultdict(list)
    for doc in documents:
        owner_id = doc.get("user_id")
        file_name = doc.get("file_name")
        if owner_id and file_name:
            by_owner[str(owner_id)].append(file_name)

    for owner_id, file_names in by_owner.items():
        await remove_documentchat_index(company_id, owner_id, file_names)


async def purge_publicchat_index(company_id: str, admin_id: str, chat_name: str) -> None:
    if not chat_name:
        return
    await _delete_entire_index_safe(publicchat_index_id(company_id, admin_id, chat_name))


async def purge_webchat_index(company_id: str, admin_id: str) -> None:
    await _delete_entire_index_safe(webchat_index_id(company_id, admin_id))


async def purge_documentchat_scope_index(company_id: str, scope_id: str) -> None:
    await _delete_entire_index_safe(documentchat_index_id(company_id, scope_id))


async def purge_user_rag_indexes(company_id: str, user_id: str) -> None:
    await purge_documentchat_scope_index(company_id, user_id)


async def purge_admin_rag_indexes(db: AsyncIOMotorDatabase, company_id: str, admin_id: str) -> None:
    await purge_webchat_index(company_id, admin_id)
    await purge_documentchat_scope_index(company_id, admin_id)

    chats = await db.public_chats.find(
        {"company_id": company_id, "admin_id": admin_id}
    ).to_list(length=None)
    for chat in chats:
        chat_name = chat.get("chat_name") or ""
        if chat_name:
            await purge_publicchat_index(company_id, admin_id, chat_name)

    # Legacy index name (pre documentchat-* indexes)
    await _delete_entire_index_safe(company_id)


async def purge_company_rag_indexes(db: AsyncIOMotorDatabase, company_id: str) -> None:
    admins = await db.company_admins.find({"company_id": company_id}).to_list(length=None)
    for admin in admins:
        admin_id = admin.get("user_id")
        if admin_id:
            await purge_admin_rag_indexes(db, company_id, admin_id)

    users = await db.company_users.find({"company_id": company_id}).to_list(length=None)
    for user in users:
        user_id = user.get("user_id")
        if user_id:
            await purge_user_rag_indexes(company_id, user_id)

    await _delete_entire_index_safe(company_id)


def _rm_tree(path: str) -> None:
    if path and os.path.isdir(path):
        try:
            shutil.rmtree(path)
            logger.info("Removed directory: %s", path)
        except Exception as e:
            logger.warning("Failed to remove directory %s: %s", path, e)


async def purge_admin_chat_db_and_disk(
    db: AsyncIOMotorDatabase,
    company_id: str,
    admin_id: str,
) -> None:
    """Remove webchat + public chat Mongo rows and on-disk source files for one admin."""
    await db.public_chat_query_history.delete_many(
        {"company_id": company_id, "admin_id": admin_id}
    )
    await db.public_chat_sources.delete_many({"company_id": company_id, "admin_id": admin_id})
    await db.public_chats.delete_many({"company_id": company_id, "admin_id": admin_id})
    await db.webchat_sources.delete_many({"company_id": company_id, "admin_id": admin_id})

    _rm_tree(os.path.join(SOURCES_UPLOAD_ROOT, company_id, admin_id))


async def purge_company_chat_db_and_disk(db: AsyncIOMotorDatabase, company_id: str) -> None:
    """Remove all chat/source Mongo rows and source upload tree for a company."""
    await db.public_chat_query_history.delete_many({"company_id": company_id})
    await db.public_chat_sources.delete_many({"company_id": company_id})
    await db.public_chats.delete_many({"company_id": company_id})
    await db.webchat_sources.delete_many({"company_id": company_id})

    _rm_tree(os.path.join(SOURCES_UPLOAD_ROOT, company_id))


async def migrate_publicchat_index_on_rename(
    db: AsyncIOMotorDatabase,
    *,
    company_id: str,
    admin_id: str,
    chat_id: str,
    old_chat_name: str,
    new_chat_name: str,
) -> None:
    """
    Public chat rename: drop the old OpenSearch index, then re-index all sources under the new index id.
    """
    if old_chat_name and old_chat_name != new_chat_name:
        await purge_publicchat_index(company_id, admin_id, old_chat_name)

    from app.api.company_admin.public_chat import _reindex_public_chat_sources_after_chat_rename

    await _reindex_public_chat_sources_after_chat_rename(
        db,
        company_id=company_id,
        admin_id=admin_id,
        chat_id=chat_id,
        new_chat_name=new_chat_name,
    )
