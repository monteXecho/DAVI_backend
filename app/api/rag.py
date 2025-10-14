import os
import httpx
import asyncio
import logging
from typing import List

RAG_INDEX_URL = "http://host.docker.internal:1416/davi_indexing/run"
RAG_QUERY_URL = "http://host.docker.internal:1416/davi_query/run"

logger = logging.getLogger("uvicorn")


# ------------------------------------------------------------
# 🔹 INTERNAL ASYNC HELPER
# ------------------------------------------------------------
async def _async_rag_index_files(user_id: str, file_paths: List[str], company_id: str):
    """
    Internal async implementation for RAG indexing.
    Uses httpx.AsyncClient for non-blocking uploads.
    """
    files = []
    # ✅ use dict instead of list of tuples
    data = {
        "index_id": company_id,
        "file_ids": [f"{user_id}--{os.path.basename(fp)}" for fp in file_paths],
    }

    try:
        for file_path in file_paths:
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
# 🔹 INTERNAL SYNC HELPER (NO EVENT LOOP)
# ------------------------------------------------------------
def _sync_rag_index_files(user_id: str, file_paths: List[str], company_id: str):
    """
    Sync-safe RAG call.
    Runs in its own threadpool or standalone sync context.
    """
    files = []
    data = {
        "index_id": company_id,
        "file_ids": [f"{user_id}--{os.path.basename(fp)}" for fp in file_paths],
    }

    try:
        for file_path in file_paths:
            file_name = os.path.basename(file_path)
            files.append(("files", (file_name, open(file_path, "rb"), "application/octet-stream")))

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
# 🔹 PUBLIC ENTRY POINT
# ------------------------------------------------------------
async def rag_index_files(user_id: str, file_paths: List[str], company_id: str):
    """
    Public entry point — automatically detects async/sync context.
    Ensures no event loop conflict.
    """
    try:
        loop = asyncio.get_running_loop()
        # If we're already inside an event loop, stay async
        return await _async_rag_index_files(user_id, file_paths, company_id)
    except RuntimeError:
        # If called from sync context, use the sync fallback
        return _sync_rag_index_files(user_id, file_paths, company_id)


# ------------------------------------------------------------
# 🔹 QUERY ENDPOINT (ALWAYS ASYNC)
# ------------------------------------------------------------
async def rag_query(user_id: str, question: str, file_names: List[str], company_id: str):
    """
    Query RAG API — async only.
    """
    file_ids = [f"{user_id}--{fn}" for fn in file_names]

    payload = {
        "query": question,
        "index_id": company_id,
        "filters": {
            "field": "file_id",
            "operator": "in",
            "value": file_ids,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            response = await client.post(RAG_QUERY_URL, json=payload)
            logger.info(f"📤 Response from RAG is : {response}")            
            response.raise_for_status()
            return response.json()

    except httpx.HTTPError as e:
        raise RuntimeError(f"RAG query failed: {e}") from e
