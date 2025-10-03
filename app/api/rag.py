import os
import httpx
from typing import List

RAG_INDEX_URL = os.getenv("RAG_INDEX_URL", "http://rag-service:1416/davi_indexing/run")
RAG_QUERY_URL = os.getenv("RAG_QUERY_URL", "http://rag-service:1416/davi_query/run")


async def rag_index_files(user_id: str, file_paths: List[str]):
    """
    Send files to RAG indexing API with user-specific file_ids.
    file_ids = <user_id>--<file-name>
    """
    files = []
    data = []

    try:
        for file_path in file_paths:
            file_name = os.path.basename(file_path)
            file_id = f"{user_id}--{file_name}"

            # Open file and add to multipart
            files.append(
                ("files", (file_name, open(file_path, "rb"), "application/octet-stream"))
            )
            data.append(("file_ids", file_id))

        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(RAG_INDEX_URL, files=files, data=data)
            response.raise_for_status()
            return response.json()

    finally:
        # close opened files
        for _, (fn, f, ct) in files:
            f.close()


async def rag_query(user_id: str, question: str, file_names: List[str]):
    """
    Send query to RAG API with filters for only this user's files.
    """
    file_ids = [f"{user_id}--{fn}" for fn in file_names]

    payload = {
        "query": question,
        "filters": {
            "field": "file_id",
            "operator": "in",
            "value": file_ids,
        },
    }

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(RAG_QUERY_URL, json=payload)
        response.raise_for_status()
        return response.json()