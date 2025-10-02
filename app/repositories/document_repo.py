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

    async def add_document(
        self,
        company_id: str,
        user_id: str,
        file_name: str,
        upload_type: str,
        path: str
    ) -> Optional[Dict[str, Any]]:
        doc = {
            "company_id": company_id,
            "user_id": user_id,
            "file_name": file_name,
            "upload_type": upload_type,
            "path": path,
            "created_at": datetime.utcnow(),
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


