import os
import httpx
import asyncio
import logging
from typing import List

RAG_INDEX_URL = "http://host.docker.internal:1416/davi_indexing/run"
RAG_QUERY_URL = "http://host.docker.internal:1416/davi_query/run"


logger = logging.getLogger("uvicorn")


# ------------------------------------------------------------
#  INTERNAL ASYNC HELPER
# ------------------------------------------------------------
async def _async_rag_index_files(user_id: str, file_paths: List[str], company_id: str, is_role_based: bool = False, index_id: str = None):
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
    """
    files = []
    
    # Use custom index_id if provided, otherwise use company_id
    actual_index_id = index_id if index_id else company_id
    
    # Generate file_ids based on document type
    # Private documents: {user_id}--{filename}
    # Role-based documents: {company_id}-{admin_id}--{filename}
    if is_role_based:
        file_ids = [f"{company_id}-{user_id}--{os.path.basename(fp)}" for fp in file_paths]
    else:
        file_ids = [f"{user_id}--{os.path.basename(fp)}" for fp in file_paths]
    
    data = {
        "index_id": actual_index_id,
        "file_ids": file_ids,
        "original_file_paths": file_paths, 
    }

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
            logger.info(f"✅ RAG response: {response.status_code}, body={response.text[:300]}")

            response.raise_for_status()
            return response.json()

    finally:
        for _, (fn, f, ct) in files:
            try:
                f.close()
            except Exception:
                pass


# ------------------------------------------------------------
#  INTERNAL SYNC HELPER (NO EVENT LOOP)
# ------------------------------------------------------------
def _sync_rag_index_files(user_id: str, file_paths: List[str], company_id: str, is_role_based: bool = False, index_id: str = None):
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
    """
    files = []
    
    # Use custom index_id if provided, otherwise use company_id
    actual_index_id = index_id if index_id else company_id
    
    # Generate file_ids based on document type
    # Private documents: {user_id}--{filename}
    # Role-based documents: {company_id}-{admin_id}--{filename}
    if is_role_based:
        file_ids = [f"{company_id}-{user_id}--{os.path.basename(fp)}" for fp in file_paths]
    else:
        file_ids = [f"{user_id}--{os.path.basename(fp)}" for fp in file_paths]
    
    data = {
        "index_id": actual_index_id,
        "file_ids": file_ids,
        "original_file_paths": file_paths, 
    }

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
            return response.json()

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
async def rag_index_files(user_id: str, file_paths: List[str], company_id: str, is_role_based: bool = False, index_id: str = None):
    """
    Public entry point — automatically detects async/sync context.
    Ensures no event loop conflict.
    
    Args:
        user_id: User ID (for private docs) or admin_id (for role-based docs)
        file_paths: List of file paths to index
        company_id: Company identifier
        is_role_based: If True, file_id format will be {company_id}-{user_id}--{filename}
                      If False, file_id format will be {user_id}--{filename}
        index_id: Optional custom index_id (e.g., for webchat). If None, uses company_id
    """
    try:
        loop = asyncio.get_running_loop()
        # If we're already inside an event loop, stay async
        return await _async_rag_index_files(user_id, file_paths, company_id, is_role_based, index_id)
    except RuntimeError:
        # If called from sync context, use the sync fallback
        return _sync_rag_index_files(user_id, file_paths, company_id, is_role_based, index_id)


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

    payload = {
        "query": question,
        "index_id": actual_index_id,
        "filters": {
            "field": "file_id",
            "operator": "in",
            "value": pass_ids,
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
