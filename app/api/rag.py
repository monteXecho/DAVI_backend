import os
import httpx
import asyncio
import logging
import json
from typing import List, Optional

# RAG_INDEX_URL = "http://host.docker.internal:1416/davi_indexing/run"
# RAG_REMOVE_URL = "http://host.docker.internal:1416/davi_indexing_remove/run"
# RAG_QUERY_URL = "http://host.docker.internal:1416/davi_query/run"

RAG_INDEX_URL = "https://demo.daviapp.nl/rag/davi_indexing/run"
RAG_REMOVE_URL = "https://demo.daviapp.nl/rag/davi_indexing_remove/run"
RAG_QUERY_URL = "https://demo.daviapp.nl/rag/davi_query/run"

logger = logging.getLogger("uvicorn")


def _is_custom_chat_index(actual_index_id: str) -> bool:
    return bool(
        actual_index_id
        and (
            actual_index_id.startswith("publicchat-")
            or actual_index_id.startswith("documentchat-")
            or actual_index_id.startswith("webchat-")
        )
    )


def build_rag_file_id(index_id: str, logical_name: str) -> str:
    """Single file_id suffix used in OpenSearch ``meta.file_id``."""
    return f"{index_id}--{str(logical_name).strip()}"


def build_rag_file_ids(
    *,
    actual_index_id: str,
    file_paths: List[str],
    user_id: str,
    company_id: str,
    is_role_based: bool,
    rag_file_logical_names: Optional[List[str]] = None,
) -> List[str]:
    """
    Build per-file ``file_ids`` passed to the RAG indexer.

    For ``publicchat-`` / ``documentchat-`` / ``webchat-`` indexes, optional
    ``rag_file_logical_names`` overrides the basename of each ``file_path`` in the RAG ``file_id``
    suffix (defaults to ``os.path.basename(path)`` when omitted).
    """
    if rag_file_logical_names is not None:
        if len(rag_file_logical_names) != len(file_paths):
            raise ValueError(
                f"rag_file_logical_names ({len(rag_file_logical_names)}) must match "
                f"file_paths ({len(file_paths)})"
            )
        for ln in rag_file_logical_names:
            if not ln or not str(ln).strip():
                raise ValueError("rag_file_logical_names entries must be non-empty strings")
            ln_s = str(ln).strip()
            if os.path.sep in ln_s:
                raise ValueError("rag_file_logical_names must not contain path separators")
            if os.altsep and os.altsep in ln_s:
                raise ValueError("rag_file_logical_names must not contain alt path separators")
    use_custom = _is_custom_chat_index(actual_index_id)
    if use_custom:
        if rag_file_logical_names is not None:
            return [f"{actual_index_id}--{str(ln).strip()}" for ln in rag_file_logical_names]
        return [f"{actual_index_id}--{os.path.basename(fp)}" for fp in file_paths]

    if rag_file_logical_names is not None:
        raise ValueError(
            "rag_file_logical_names is only supported when index_id is "
            "publicchat-*, documentchat-*, or webchat-*"
        )
    if is_role_based:
        return [f"{company_id}-{user_id}--{os.path.basename(fp)}" for fp in file_paths]
    return [f"{user_id}--{os.path.basename(fp)}" for fp in file_paths]


def _check_rag_remove_response_payload(result) -> None:
    """RAG remove may return HTTP 200 with an error string inside ``result.message``."""
    if not isinstance(result, dict):
        return
    result_data = result.get("result", result)
    if not isinstance(result_data, dict):
        return
    message = result_data.get("message", "")
    if message and (
        message.startswith("Error")
        or "error" in message.lower()
        or "failed" in message.lower()
    ):
        raise RuntimeError(f"RAG remove failed: {message}")


def _check_rag_index_response_payload(result) -> None:
    """RAG sometimes returns HTTP 200 with an error string inside ``result.message``."""
    if not isinstance(result, dict):
        return
    result_data = result.get("result", {})
    if not isinstance(result_data, dict):
        return
    message = result_data.get("message", "")
    if message and (
        "Error" in message
        or "error" in message.lower()
        or "failed" in message.lower()
        or "Failed" in message
    ):
        error_detail = result_data.get("error", message)
        logger.error(f"RAG indexing error detected: {error_detail}")
        raise RuntimeError(f"RAG indexing failed: {error_detail}")


# ------------------------------------------------------------
#  INTERNAL ASYNC HELPER
# ------------------------------------------------------------
async def _async_rag_index_files(
    user_id: str,
    file_paths: List[str],
    company_id: str,
    is_role_based: bool = False,
    index_id: str = None,
    file_metadata: List[dict] = None,
    rag_file_logical_names: Optional[List[str]] = None,
):
    """
    Internal async implementation for RAG indexing.
    Uses httpx.AsyncClient for non-blocking uploads.
    
    Args:
        user_id: User ID (for private docs) or admin_id (for role-based docs)
        file_paths: List of file paths to index
        company_id: Company identifier
        is_role_based: If True, file_id format will be {company_id}-{user_id}--{filename}
                      If False, file_id format will be {user_id}--{filename}
        index_id: Optional custom index_id (e.g., for webchat). If None, uses company_id
        file_metadata: Optional list of metadata dicts for each file (e.g., [{"url": "...", "title": "..."}])
        rag_file_logical_names: Optional per-file logical names for publicchat/documentchat/webchat
                                (suffix after ``--`` in file_id), aligned with ``file_paths``.
    """
    files = []
    
    # Use custom index_id if provided, otherwise use company_id
    actual_index_id = index_id if index_id else company_id
    
    file_ids = build_rag_file_ids(
        actual_index_id=actual_index_id,
        file_paths=file_paths,
        user_id=user_id,
        company_id=company_id,
        is_role_based=is_role_based,
        rag_file_logical_names=rag_file_logical_names,
    )
    
    data = {
        "index_id": actual_index_id,
        "file_ids": file_ids,
        "original_file_paths": file_paths, 
    }
    
    # Add file metadata if provided
    # RAG API expects 'files_meta_data' as a JSON-encoded string
    # Format: JSON array of objects, e.g., [{"source_url": "...", "source_title": "..."}, ...]
    if file_metadata and len(file_metadata) == len(file_paths):
        data["files_meta_data"] = json.dumps(file_metadata)

    try:
        # Validate file_paths is a list
        if not isinstance(file_paths, list):
            raise ValueError(f"file_paths must be a list, got {type(file_paths)}: {file_paths}")
        
        logger.info(f"   _____ File paths (list) _____: {file_paths}")
        for file_path in file_paths:
            if not isinstance(file_path, str):
                raise ValueError(f"Each file_path must be a string, got {type(file_path)}: {file_path}")
            logger.info(f"   _____ Processing file path _____: {file_path}")
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"File does not exist: {file_path}")
            file_name = os.path.basename(file_path)
            files.append(("files", (file_name, open(file_path, "rb"), "application/octet-stream")))

        logger.info(f"📤 Sending RAG index request to {RAG_INDEX_URL}")
        logger.info(f"   Files: {[f[1][0] for f in files]}")
        logger.info(f"   Data: {data}")

        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(RAG_INDEX_URL, files=files, data=data)
            response_text = response.text
            logger.info(f"✅ RAG response: {response.status_code}, body={response_text[:500]}")

            response.raise_for_status()
            result = response.json()
            _check_rag_index_response_payload(result)

            return result

    finally:
        for _, (fn, f, ct) in files:
            try:
                f.close()
            except Exception:
                pass


# ------------------------------------------------------------
#  INTERNAL SYNC HELPER (NO EVENT LOOP)
# ------------------------------------------------------------
def _sync_rag_index_files(
    user_id: str,
    file_paths: List[str],
    company_id: str,
    is_role_based: bool = False,
    index_id: str = None,
    file_metadata: List[dict] = None,
    rag_file_logical_names: Optional[List[str]] = None,
):
    """
    Sync-safe RAG call.
    Runs in its own threadpool or standalone sync context.
    
    Args:
        user_id: User ID (for private docs) or admin_id (for role-based docs)
        file_paths: List of file paths to index
        company_id: Company identifier
        is_role_based: If True, file_id format will be {company_id}-{user_id}--{filename}
                      If False, file_id format will be {user_id}--{filename}
        index_id: Optional custom index_id (e.g., for webchat). If None, uses company_id
        file_metadata: Optional list of metadata dicts for each file (e.g., [{"url": "...", "title": "..."}])
        rag_file_logical_names: See async helper.
    """
    files = []
    
    actual_index_id = index_id if index_id else company_id

    file_ids = build_rag_file_ids(
        actual_index_id=actual_index_id,
        file_paths=file_paths,
        user_id=user_id,
        company_id=company_id,
        is_role_based=is_role_based,
        rag_file_logical_names=rag_file_logical_names,
    )

    data = {
        "index_id": actual_index_id,
        "file_ids": file_ids,
        "original_file_paths": file_paths, 
    }
    
    # Add file metadata if provided
    # RAG API expects 'files_meta_data' as a JSON-encoded string
    # Format: JSON array of objects, e.g., [{"source_url": "...", "source_title": "..."}, ...]
    if file_metadata and len(file_metadata) == len(file_paths):
        data["files_meta_data"] = json.dumps(file_metadata)

    try:
        for file_path in file_paths:
            logger.info(f"   _____ File path _____: {file_path}")
            file_name = os.path.basename(file_path)
            files.append(("files", (file_name, open(file_path, "rb"), "application/octet-stream")))

        logger.info(f"   _____ File path _____: {file_paths}")
        logger.info(f"📤 (SYNC) Sending RAG index request to {RAG_INDEX_URL}")
        logger.info(f"   Files: {[f[1][0] for f in files]}")
        logger.info(f"   Data: {data}")

        with httpx.Client(timeout=httpx.Timeout(180.0)) as client:
            response = client.post(RAG_INDEX_URL, files=files, data=data)
            response.raise_for_status()
            payload = response.json()
            _check_rag_index_response_payload(payload)
            return payload

    except Exception as e:
        raise RuntimeError(f"Sync RAG indexing failed: {e}") from e

    finally:
        for _, (fn, f, ct) in files:
            try:
                f.close()
            except Exception:
                pass


# ------------------------------------------------------------
#  PUBLIC ENTRY POINT
# ------------------------------------------------------------
async def rag_index_files(
    user_id: str,
    file_paths: List[str],
    company_id: str,
    is_role_based: bool = False,
    index_id: str = None,
    file_metadata: List[dict] = None,
    rag_file_logical_names: Optional[List[str]] = None,
):
    """
    Public entry point — automatically detects async/sync context.
    Ensures no event loop conflict.

    Note: ``_async_rag_index_files`` raises ``RuntimeError`` on RAG failures; that must not
    trigger the sync fallback (only "no running loop" should), or we double-post to the
    indexer and see OpenSearch ``version_conflict_engine_exception`` (409).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Called from a synchronous context with no running loop
        return _sync_rag_index_files(
            user_id,
            file_paths,
            company_id,
            is_role_based,
            index_id,
            file_metadata,
            rag_file_logical_names,
        )
    return await _async_rag_index_files(
        user_id,
        file_paths,
        company_id,
        is_role_based,
        index_id,
        file_metadata,
        rag_file_logical_names,
    )


# ------------------------------------------------------------
#  REMOVE INDEXED FILES
# ------------------------------------------------------------
async def _async_rag_remove_indexed_files(
    index_id: str,
    file_ids: Optional[List[str]] = None,
    delete_entire_index: bool = False,
):
    if delete_entire_index:
        payload = {"index_id": index_id, "delete_entire_index": True}
    elif file_ids:
        payload = {"index_id": index_id, "file_ids": file_ids}
    else:
        return {"message": "No file_ids to remove"}

    logger.info(f"Sending RAG remove request to {RAG_REMOVE_URL}: {payload}")

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(RAG_REMOVE_URL, json=payload)
        logger.info(f"RAG remove response: {response.status_code}, body={response.text[:500]}")
        response.raise_for_status()
        result = response.json()
        _check_rag_remove_response_payload(result)
        return result


def _sync_rag_remove_indexed_files(
    index_id: str,
    file_ids: Optional[List[str]] = None,
    delete_entire_index: bool = False,
):
    if delete_entire_index:
        payload = {"index_id": index_id, "delete_entire_index": True}
    elif file_ids:
        payload = {"index_id": index_id, "file_ids": file_ids}
    else:
        return {"message": "No file_ids to remove"}

    logger.info(f"(SYNC) Sending RAG remove request to {RAG_REMOVE_URL}: {payload}")

    with httpx.Client(timeout=httpx.Timeout(120.0)) as client:
        response = client.post(RAG_REMOVE_URL, json=payload)
        response.raise_for_status()
        result = response.json()
        _check_rag_remove_response_payload(result)
        return result


async def rag_remove_indexed_files(index_id: str, file_ids: List[str]):
    """
    Remove indexed chunks from OpenSearch for the given ``file_ids``.

    Call this when DAVI deletes a document/source from the DB so re-upload does not duplicate chunks.
    """
    if not file_ids:
        return None

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _sync_rag_remove_indexed_files(index_id, file_ids)
    return await _async_rag_remove_indexed_files(index_id, file_ids)


async def rag_delete_entire_index(index_id: str):
    """Remove all chunks in an OpenSearch index (chat rename, admin/company purge)."""
    if not index_id:
        return None

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _sync_rag_remove_indexed_files(index_id, delete_entire_index=True)
    return await _async_rag_remove_indexed_files(index_id, delete_entire_index=True)


# ------------------------------------------------------------
#  QUERY ENDPOINT (ALWAYS ASYNC)
# ------------------------------------------------------------
async def rag_query(pass_ids: str, question: str, file_names: List[str], company_id: str, index_id: str = None):
    """
    Query RAG API — async only.
    
    Args:
        pass_ids: Comma-separated file IDs to filter
        question: User's question
        file_names: List of file names
        company_id: Company identifier
        index_id: Optional custom index_id (e.g., for webchat). If None, uses company_id
    """
    # Use custom index_id if provided, otherwise use company_id
    actual_index_id = index_id if index_id else company_id

    # Convert comma-separated string to array for RAG API
    file_id_list = pass_ids.split(",") if isinstance(pass_ids, str) else pass_ids
    if isinstance(file_id_list, str):
        file_id_list = [file_id_list]
    
    payload = {
        "query": question,
        "index_id": actual_index_id,
        "filters": {
            "field": "file_id",
            "operator": "in",
            "value": file_id_list,
        },
    }

    logger.info(f"📤 Sending RAG request payload is:  {payload}")

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            response = await client.post(RAG_QUERY_URL, json=payload)
            logger.info(f"📤 Response from RAG is : {response}")            
            response.raise_for_status()
            return response.json()

    except httpx.HTTPError as e:
        raise RuntimeError(f"RAG query failed: {e}") from e
