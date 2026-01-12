from datetime import datetime
from typing import Optional, List, Dict, Any
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError


class DocumentRepository:
    def __init__(self, db: AsyncIOMotorDatabase):
        self.collection = db.documents

    async def init_indexes(self):
        """Initialize indexes (call once at startup)."""
        await self.collection.create_index(
            [("company_id", 1), ("user_id", 1), ("file_name", 1)],
            unique=True,
        )

    async def document_exists(
        self,
        company_id: str,
        user_id: str,
        file_name: str,
        upload_type: str
    ) -> bool:
        """Check if a document already exists for the given parameters."""
        existing = await self.collection.find_one({
            "company_id": company_id,
            "user_id": user_id,
            "file_name": file_name,
            "upload_type": upload_type,
        })
        return existing is not None

    async def add_document(
        self,
        company_id: str,
        user_id: str,
        file_name: str,
        upload_type: str,
        path: str,
        storage_path: Optional[str] = None,
        source: str = "manual_upload"
    ) -> Optional[Dict[str, Any]]:
        """
        Add a document record to MongoDB.
        
        Args:
            company_id: Company identifier
            user_id: User identifier
            file_name: Name of the file
            upload_type: Type of upload ("document" for private, folder name for role-based)
            path: Local file path (for backward compatibility) or storage path
            storage_path: Optional canonical storage path (e.g., Nextcloud path)
            source: Source of the document ("manual_upload" or "imported")
        """
        doc = {
            "company_id": company_id,
            "user_id": user_id,
            "file_name": file_name,
            "upload_type": upload_type,
            "path": path,  # Keep for backward compatibility
            "created_at": datetime.utcnow(),
            # Storage metadata (Scenario E1, A1 support)
            "storage_path": storage_path,  # Canonical path in Nextcloud
            "source": source,  # "manual_upload" or "imported"
        }
        try:
            await self.collection.insert_one(doc)
            return doc
        except DuplicateKeyError:
            return None

    async def get_documents_by_user(self, user_id: str) -> List[Dict[str, Any]]:
        return await self.collection.find({"user_id": user_id}).to_list(length=None)

    async def get_documents_by_company(self, company_id: str) -> List[Dict[str, Any]]:
        return await self.collection.find({"company_id": company_id}).to_list(length=None)

    async def delete_documents_by_user(self, user_id: str) -> int:
        result = await self.collection.delete_many({"user_id": user_id})
        return result.deleted_count

    async def delete_documents_by_company(self, company_id: str) -> int:
        result = await self.collection.delete_many({"company_id": company_id})
        return result.deleted_count


