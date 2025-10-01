from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError


class DocumentRepository:
    def __init__(self, db: AsyncIOMotorDatabase):
        self.collection = db.documents
        # Ensure we enforce uniqueness at DB level too
        # (user_id + file_name + upload_type must be unique)
        self.collection.create_index(
            [("user_id", 1), ("file_name", 1), ("upload_type", 1)],
            unique=True,
        )

    async def add_document(self, user_id: str, file_name: str, upload_type: str, path: str) -> dict:
        doc = {
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
            # Alert: this doc already exists
            return None
