from bson.json_util import dumps
import json
import os

from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorDatabase
from typing import Optional
import uuid

DEFAULT_MODULES = {
    "Documenten chat": {"desc": "AI-zoek & Q&A over geuploade documenten.", "enabled": False},
    "VGC module": {"desc": "Vaste gezichten criterium controles & rapportage.", "enabled": False},
    "3-uurs regeling": {"desc": "Toetsing + logica voor afwijkvensters.", "enabled": False},
    "BKR": {"desc": "Beroepskracht-kindratio berekenen & bewaken.", "enabled": False},
}

class CompanyRepository:
    def __init__(self, db: AsyncIOMotorDatabase):
        self.collection = db.companies


    async def _reindex_companies(self):
        """Reassign sequential ids c1, c2… after create/delete."""
        cursor = self.collection.find().sort("created_at", 1)
        idx = 1
        async for company in cursor:
            await self.collection.update_one(
                {"_id": company["_id"]},
                {"$set": {"company_id": f"c{idx}", "updated_at": datetime.utcnow()}}
            )
            idx += 1


    async def _reindex_admins(self, company_id: str):
        """Reassign sequential ids a1, a2… inside a company."""
        company = await self.collection.find_one({"company_id": company_id})
        if not company:
            return None
        admins = company.get("admins", [])
        for idx, admin in enumerate(admins, start=1):
            admin["admin_id"] = f"a{idx}"

        await self.collection.update_one(
            {"company_id": company_id},
            {"$set": {"admins": admins, "updated_at": datetime.utcnow()}}
        )
        return admins



    async def get_all_companies(self):
        cursor = self.collection.find().sort("company_id", 1)
        companies = []
        async for company in cursor:
            companies.append({
                "id": company.get("company_id"),
                "name": company["name"],
                "admins": [
                    {
                        "id": admin.get("admin_id"),
                        "user_id": admin["user_id"],
                        "name": admin["name"],
                        "email": admin["email"],
                        "modules": [
                            {"name": k, "desc": v["desc"], "enabled": v["enabled"]}
                            for k, v in admin["modules"].items()
                        ],
                        "documents": [
                            {
                                "user_id": doc.get("user_id"),
                                "file_name": doc.get("file_name"),
                                "uploaded_at": doc.get("uploaded_at")
                            }
                            for doc in admin.get("documents", [])
                        ]
                    }
                    for admin in company.get("admins", [])
                ]
            })
        return {"companies": companies}



    async def create_company(self, name: str) -> dict:
        now = datetime.utcnow()
        doc = {
            "name": name,
            "company_id": None, 
            "admins": [],
            "created_at": now,
            "updated_at": now,
        }
        await self.collection.insert_one(doc)
        await self._reindex_companies()
        return {"name": name}


    async def add_admin(self, company_id: str, name: str, email: str, modules: Optional[dict]) -> dict:
        company = await self.collection.find_one({"company_id": company_id})
        if not company:
            return None

        for admin in company.get("admins", []):
            if admin["email"].lower() == email.lower():
                raise ValueError("Admin with this email already exists in the company")

        admin_modules = DEFAULT_MODULES.copy()
        if modules:
            for k, v in modules.items():
                if k in admin_modules:
                    admin_modules[k]["enabled"] = v.get("enabled", False)

        admin_doc = {
            "admin_id": None, 
            "user_id": str(uuid.uuid4()),  
            "name": name,
            "email": email,
            "modules": admin_modules,
        }

        result = await self.collection.update_one(
            {"company_id": company_id},
            {"$push": {"admins": admin_doc}, "$set": {"updated_at": datetime.utcnow()}}
        )

        if result.matched_count == 0:
            return None

        admins = await self._reindex_admins(company_id)
        return {"company_id": company_id, "admins": admins}


    async def assign_modules(self, company_id: str, admin_id: str, modules: dict) -> Optional[dict]:
        company = await self.collection.find_one({"company_id": company_id})
        if not company:
            return None

        admins = company.get("admins", [])
        for admin in admins:
            if admin.get("admin_id") == admin_id:
                for k, v in modules.items():
                    if k in admin["modules"]:
                        admin["modules"][k]["enabled"] = v.get("enabled", False)
                break
        else:
            return None 

        await self.collection.update_one(
            {"company_id": company_id},
            {"$set": {"admins": admins, "updated_at": datetime.utcnow()}}
        )
        return {"company_id": company_id, "admin_id": admin_id, "modules": admins}


    async def delete_company(self, company_id: str) -> bool:
        result = await self.collection.delete_one({"company_id": company_id})
        if result.deleted_count > 0:
            await self._reindex_companies()
            return True
        return False


    async def delete_admin(self, company_id: str, admin_id: str) -> bool:
        result = await self.collection.update_one(
            {"company_id": company_id},
            {"$pull": {"admins": {"admin_id": admin_id}}, "$set": {"updated_at": datetime.utcnow()}}
        )
        if result.modified_count > 0:
            await self._reindex_admins(company_id)
            return True
        return False

    async def find_admin_by_email(self, email: str):
        company = await self.collection.find_one(
            {"admins.email": email},
            {"admins.$": 1}
        )
        if not company or "admins" not in company or not company["admins"]:
            return None

        return {
            "email": company["admins"][0]["email"],
            "role": "company_admin"
        }

    
    async def find_admin_full_by_email(self, email: str) -> Optional[dict]:
        company = await self.collection.find_one(
            {"admins.email": email},
            {"admins.$": 1}
        )
        if not company or "admins" not in company or not company["admins"]:
            return None

        return company["admins"][0]  # full admin document (with user_id, modules, etc.)


    async def add_admin_document(self, user_id: str, file_name: str):
        existing = await self.collection.find_one({
            "admins.user_id": user_id,
            "admins.documents.file_name": file_name,
        })
        if existing:
            return None  # duplicate
        
        result = await self.collection.update_one(
            {"admins.user_id": user_id},
            {
                "$push": {
                    "admins.$.documents": {
                        "user_id": user_id,
                        "file_name": file_name,
                        "uploaded_at": datetime.utcnow(),
                    }
                }
            },
        )
        return result.modified_count > 0
    

    async def find_company_by_user_email(self, email: str):
        """
        Search for a company where either an admin or a user has the given email.
        """
        return await self.collection.find_one({
            "$or": [
                {"admins.email": email},
                {"users.email": email}
            ]
        })

    async def get_user_with_documents(self, email: str):
        """
        Look up a user (admin or company user) by email and
        return their documents with resolved paths.
        """
        company = await self.find_company_by_user_email(email)
        if not company:
            return None

        # check admins
        matched_user = next(
            (admin for admin in company.get("admins", []) if admin.get("email") == email),
            None
        )

        # check users if not found in admins
        if not matched_user:
            matched_user = next(
                (user for user in company.get("users", []) if user.get("email") == email),
                None
            )

        if not matched_user:
            return None

        # resolve document file paths
        docs = matched_user.get("documents", [])
        doc_list = [
            {
                "file_path": os.path.join("/app/uploads/document", doc["user_id"], doc["file_name"]),
                "file_name": doc["file_name"],
            }
            for doc in docs
        ]

        return {
            "user_id": matched_user.get("user_id"),
            "email": matched_user.get("email"),
            "documents": doc_list,
        }

