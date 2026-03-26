import os
import httpx
import asyncio
import logging
import json
from typing import List

# RAG_INDEX_URL = "http://host.docker.internal:1416/davi_indexing/run"
# RAG_QUERY_URL = "http://host.docker.internal:1416/davi_query/run"

RAG_INDEX_URL = "https://demo.daviapp.nl/rag/davi_indexing/run"
RAG_QUERY_URL = "https://demo.daviapp.nl/rag/davi_query/run"

logger = logging.getLogger("uvicorn")


# ------------------------------------------------------------
#  INTERNAL ASYNC HELPER
# ------------------------------------------------------------
async def _async_rag_index_files(user_id: str, file_paths: List[str], company_id: str, is_role_based: bool = False, index_id: str = None, file_metadata: List[dict] = None):
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
    """
    files = []
    
    # Use custom index_id if provided, otherwise use company_id
    actual_index_id = index_id if index_id else company_id
    
    # Generate file_ids based on document type
    # Private documents: {user_id}--{filename}
    # Role-based documents: {company_id}-{admin_id}--{filename}
    # Publicchat: {index_id}--{filename} (when index_id starts with "publicchat-")
    # Documentchat: {index_id}--{filename} (when index_id starts with "documentchat-")
    # Webchat: {index_id}--{filename} (when index_id starts with "webchat-")
    if actual_index_id and (actual_index_id.startswith("publicchat-") or 
                            actual_index_id.startswith("documentchat-") or 
                            actual_index_id.startswith("webchat-")):
        # For publicchat, documentchat, and webchat, use index_id as the file_id prefix
        file_ids = [f"{actual_index_id}--{os.path.basename(fp)}" for fp in file_paths]
    elif is_role_based:
        file_ids = [f"{company_id}-{user_id}--{os.path.basename(fp)}" for fp in file_paths]
    else:
        file_ids = [f"{user_id}--{os.path.basename(fp)}" for fp in file_paths]
    
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
            
            # Check if RAG service returned an error in the result (even with 200 status)
            if isinstance(result, dict):
                result_data = result.get("result", {})
                if isinstance(result_data, dict):
                    message = result_data.get("message", "")
                    # Check for error indicators in the message
                    if message and ("Error" in message or "error" in message.lower() or "failed" in message.lower() or "Failed" in message):
                        error_detail = result_data.get("error", message)
                        logger.error(f"RAG indexing error detected: {error_detail}")
                        raise RuntimeError(f"RAG indexing failed: {error_detail}")
            
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
def _sync_rag_index_files(user_id: str, file_paths: List[str], company_id: str, is_role_based: bool = False, index_id: str = None, file_metadata: List[dict] = None):
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
    """
    files = []
    
    # Use custom index_id if provided, otherwise use company_id
    actual_index_id = index_id if index_id else company_id
    
    # Generate file_ids based on document type
    # Private documents: {user_id}--{filename}
    # Role-based documents: {company_id}-{admin_id}--{filename}
    # Publicchat: {index_id}--{filename} (when index_id starts with "publicchat-")
    # Documentchat: {index_id}--{filename} (when index_id starts with "documentchat-")
    # Webchat: {index_id}--{filename} (when index_id starts with "webchat-")
    if actual_index_id and (actual_index_id.startswith("publicchat-") or 
                            actual_index_id.startswith("documentchat-") or 
                            actual_index_id.startswith("webchat-")):
        # For publicchat, documentchat, and webchat, use index_id as the file_id prefix
        file_ids = [f"{actual_index_id}--{os.path.basename(fp)}" for fp in file_paths]
    elif is_role_based:
        file_ids = [f"{company_id}-{user_id}--{os.path.basename(fp)}" for fp in file_paths]
    else:
        file_ids = [f"{user_id}--{os.path.basename(fp)}" for fp in file_paths]
    
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
async def rag_index_files(user_id: str, file_paths: List[str], company_id: str, is_role_based: bool = False, index_id: str = None, file_metadata: List[dict] = None):
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
        file_metadata: Optional list of metadata dicts for each file (e.g., [{"url": "...", "title": "..."}])
    """
    try:
        loop = asyncio.get_running_loop()
        # If we're already inside an event loop, stay async
        return await _async_rag_index_files(user_id, file_paths, company_id, is_role_based, index_id, file_metadata)
    except RuntimeError:
        # If called from sync context, use the sync fallback
        return _sync_rag_index_files(user_id, file_paths, company_id, is_role_based, index_id, file_metadata)


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
