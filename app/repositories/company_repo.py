import uuid
import copy
from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorDatabase
from typing import Optional

BASE_DOC_URL = "https://your-backend.com/documents/download"

DEFAULT_MODULES = {
    "Documenten chat": {"desc": "AI-zoek & Q&A over geuploade documenten.", "enabled": False},
    "VGC module": {"desc": "Vaste gezichten criterium controles & rapportage.", "enabled": False},
    "3-uurs regeling": {"desc": "Toetsing + logica voor afwijkvensters.", "enabled": False},
    "BKR": {"desc": "Beroepskracht-kindratio berekenen & bewaken.", "enabled": False},
}


def serialize_modules(modules: dict) -> list:
    return [{"name": k, "desc": v["desc"], "enabled": v["enabled"]} for k, v in modules.items()]


def serialize_documents(docs_cursor, user_id: str):
    return [
        {
            "file_name": d["file_name"],
            "file_url": f"{BASE_DOC_URL}/{user_id}/{d['file_name']}",
        }
        for d in docs_cursor
    ]


class CompanyRepository:
    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db
        self.companies = db.companies
        self.admins = db.company_admins
        self.users = db.company_users
        self.documents = db.documents

    async def get_admins_by_company(self, company_id: str):
        return await self.admins.find({"company_id": company_id}).to_list(None)

    async def get_users_by_company(self, company_id: str):
        return await self.users.find({"company_id": company_id}).to_list(None)

    async def get_admin_by_id(self, company_id: str, admin_id: str):
        return await self.admins.find_one({"company_id": company_id, "admin_id": admin_id})


    # ---------------- Companies ---------------- #
    async def create_company(self, name: str) -> dict:
        now = datetime.utcnow()
        company_id = str(uuid.uuid4())
        doc = {
            "company_id": company_id,
            "name": name,
            "created_at": now,
            "updated_at": now,
        }
        await self.companies.insert_one(doc)
        return {
            "id": company_id,
            "name": name,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }

    async def get_all_companies(self):
        companies_cursor = self.companies.find().sort("name", 1)
        companies = []

        async for company in companies_cursor:
            company_id = company["company_id"]

            # Fetch admins
            admins_cursor = self.admins.find({"company_id": company_id})
            admins = []
            async for admin in admins_cursor:
                docs_cursor = self.documents.find({"user_id": admin["user_id"]})
                documents = [
                    {
                        "file_name": d["file_name"],
                        "file_url": f"{BASE_DOC_URL}/{d['user_id']}/{d['file_name']}",
                    }
                    async for d in docs_cursor
                ]
                admins.append({
                    "id": admin["admin_id"],
                    "user_id": admin["user_id"],
                    "name": admin["name"],
                    "email": admin["email"],
                    "modules": serialize_modules(admin["modules"]),
                    "documents": documents,
                })

            # Fetch users
            users_cursor = self.users.find({"company_id": company_id})
            users = []
            async for user in users_cursor:
                docs_cursor = self.documents.find({"user_id": user["user_id"]})
                documents = [
                    {
                        "file_name": d["file_name"],
                        "file_url": f"{BASE_DOC_URL}/{d['user_id']}/{d['file_name']}",
                    }
                    async for d in docs_cursor
                ]
                users.append({
                    "id": user["user_id"],
                    "name": user["name"],
                    "email": user["email"],
                    "documents": documents,
                })

            companies.append({
                "id": company_id,
                "name": company["name"],
                "admins": admins,
                "users": users,
            })

        return {"companies": companies}

    async def delete_company(self, company_id: str) -> bool:
        await self.admins.delete_many({"company_id": company_id})
        await self.users.delete_many({"company_id": company_id})
        await self.documents.delete_many({"company_id": company_id})
        result = await self.companies.delete_one({"company_id": company_id})
        return result.deleted_count > 0

    # ---------------- Admins ---------------- #
    async def add_admin(self, company_id: str, name: str, email: str, modules: Optional[dict] = None):
        if await self.admins.find_one({"company_id": company_id, "email": email}):
            raise ValueError("Admin with this email already exists")

        admin_modules = copy.deepcopy(DEFAULT_MODULES)
        if modules:
            for k, v in modules.items():
                if k in admin_modules:
                    admin_modules[k]["enabled"] = v.get("enabled", False)

        admin_doc = {
            "admin_id": str(uuid.uuid4()),
            "company_id": company_id,
            "user_id": str(uuid.uuid4()),
            "name": name,
            "email": email,
            "modules": admin_modules,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        await self.admins.insert_one(admin_doc)

        return {
            "id": admin_doc["admin_id"],
            "user_id": admin_doc["user_id"],
            "company_id": admin_doc["company_id"],
            "name": admin_doc["name"],
            "email": admin_doc["email"],
            "modules": serialize_modules(admin_doc["modules"]),
            "documents": [],  # empty until uploaded
        }

    async def delete_admin(self, company_id: str, admin_id: str) -> bool:
        admin = await self.admins.find_one({"company_id": company_id, "admin_id": admin_id})
        if not admin:
            return False

        result = await self.admins.delete_one({"company_id": company_id, "admin_id": admin_id})
        if result.deleted_count > 0:
            await self.documents.delete_many({"user_id": admin["user_id"]})
            return True
        return False

    async def assign_modules(self, company_id: str, admin_id: str, modules: dict) -> Optional[dict]:
        admin = await self.admins.find_one({"company_id": company_id, "admin_id": admin_id})
        if not admin:
            return None

        for k, v in modules.items():
            if k in admin["modules"]:
                admin["modules"][k]["enabled"] = v.get("enabled", False)

        await self.admins.update_one(
            {"company_id": company_id, "admin_id": admin_id},
            {"$set": {"modules": admin["modules"], "updated_at": datetime.utcnow()}},
        )

        # return clean version
        return {
            "id": admin["admin_id"],
            "user_id": admin["user_id"],
            "company_id": admin["company_id"],
            "name": admin["name"],
            "email": admin["email"],
            "modules": serialize_modules(admin["modules"]),
        }

    async def find_admin_by_email(self, email: str):
        admin = await self.admins.find_one({"email": email})
        if not admin:
            return None
        return {"email": admin["email"], "role": "company_admin"}

    # ---------------- Users ---------------- #
    async def add_user(self, company_id: str, name: str, email: str):
        if await self.users.find_one({"company_id": company_id, "email": email}):
            raise ValueError("User with this email already exists")

        user_doc = {
            "user_id": str(uuid.uuid4()),
            "company_id": company_id,
            "name": name,
            "email": email,
            "created_at": datetime.utcnow(),
        }
        await self.users.insert_one(user_doc)

        return {
            "id": user_doc["user_id"],
            "company_id": user_doc["company_id"],
            "name": user_doc["name"],
            "email": user_doc["email"],
            "documents": [],
        }

    async def delete_user(self, company_id: str, user_id: str) -> bool:
        result = await self.users.delete_one({"company_id": company_id, "user_id": user_id})
        if result.deleted_count > 0:
            await self.documents.delete_many({"user_id": user_id})
            return True
        return False

    # ---------------- Shared ---------------- #
    async def get_user_with_documents(self, email: str):
        user = await self.admins.find_one({"email": email})
        role = "company_admin"
        if not user:
            user = await self.users.find_one({"email": email})
            role = "company_user"
        if not user:
            return None

        docs_cursor = self.documents.find({"user_id": user["user_id"]})
        documents = [
            {
                "file_name": d["file_name"],
                "file_url": f"{BASE_DOC_URL}/{d['user_id']}/{d['file_name']}",
            }
            async for d in docs_cursor
        ]

        return {
            "id": user.get("admin_id", user.get("user_id")),
            "user_id": user["user_id"],
            "company_id": user["company_id"],
            "name": user["name"],
            "email": user["email"],
            "role": role,
            "modules": serialize_modules(user["modules"]) if "modules" in user else [],
            "documents": documents,
        }
