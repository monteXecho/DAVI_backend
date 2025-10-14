import uuid
import copy
import os
from pathlib import Path
from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorDatabase
from typing import Optional

BASE_DOC_URL = "https://your-backend.com/documents/download"

# Base path for all uploads
UPLOAD_ROOT = "/app/uploads/documents"

# Ensure root folder exists at startup
os.makedirs(UPLOAD_ROOT, exist_ok=True)

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
        self.roles = db.company_roles
        self.documents = db.documents

    async def get_admins_by_company(self, company_id: str):
        return await self.admins.find({"company_id": company_id}).to_list(None)

    async def get_users_by_company(self, company_id: str):
        return await self.users.find({"company_id": company_id}).to_list(None)

    async def get_users_by_company_admin(self, admin_id: str):
        return await self.users.find({"added_by_admin_id": admin_id}, {"_id": 0}).to_list(None)

    async def get_admin_by_id(self, company_id: str, user_id: str):
        return await self.admins.find_one({"company_id": company_id, "user_id": user_id})


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
                    "id": admin["user_id"],
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
            "user_id": admin_doc["user_id"],
            "company_id": admin_doc["company_id"],
            "name": admin_doc["name"],
            "email": admin_doc["email"],
            "modules": serialize_modules(admin_doc["modules"]),
            "documents": [],  # empty until uploaded
        }

    async def delete_admin(self, company_id: str, user_id: str) -> bool:
        admin = await self.admins.find_one({"company_id": company_id, "user_id": user_id})
        if not admin:
            return False

        result = await self.admins.delete_one({"company_id": company_id, "user_id": user_id})
        if result.deleted_count > 0:
            await self.documents.delete_many({"user_id": admin["user_id"]})
            return True
        return False

    async def assign_modules(self, company_id: str, user_id: str, modules: dict) -> Optional[dict]:
        admin = await self.admins.find_one({"company_id": company_id, "user_id": user_id})
        if not admin:
            return None

        for k, v in modules.items():
            if k in admin["modules"]:
                admin["modules"][k]["enabled"] = v.get("enabled", False)

        await self.admins.update_one(
            {"company_id": company_id, "user_id": user_id},
            {"$set": {"modules": admin["modules"], "updated_at": datetime.utcnow()}},
        )

        # return clean version
        return {
            "id": admin["user_id"],
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

    # ---------------- Company Roles ---------------- #
    async def add_or_update_role(self, company_id: str, role_name: str, folders: list[str]) -> dict:
        """
        Create or update a company role with given subfolders.
        Ensures folders exist on disk under /app/uploads/documents/roleBased/{company_id}/.
        """
        folders = [f.strip("/") for f in folders if f.strip()]

        existing_role = await self.roles.find_one({
            "company_id": company_id,
            "name": role_name
        })

        if existing_role:
            # Merge without duplicates
            current_folders = set(existing_role.get("folders", []))
            updated_folders = sorted(current_folders.union(folders))
            await self.roles.update_one(
                {"_id": existing_role["_id"]},
                {"$set": {"folders": updated_folders, "updated_at": datetime.utcnow()}}
            )
            status = "role_updated"
        else:
            await self.roles.insert_one({
                "company_id": company_id,
                "name": role_name,
                "folders": folders,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            })
            updated_folders = folders
            status = "role_created"

        # Ensure folders exist on disk
        base_path = os.path.join(UPLOAD_ROOT, "roleBased", company_id)
        for folder in updated_folders:
            full_path = os.path.join(base_path, folder)
            os.makedirs(full_path, exist_ok=True)

        return {
            "status": status,
            "company_id": company_id,
            "role_name": role_name,
            "folders": updated_folders,
        }
    
    
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
            "id": user.get("user_id", user.get("user_id")),
            "user_id": user["user_id"],
            "company_id": user["company_id"],
            "name": user["name"],
            "email": user["email"],
            "role": role,
            "modules": serialize_modules(user["modules"]) if "modules" in user else [],
            "documents": documents,
        }

    async def add_user_by_admin(self, company_id: str, added_by_admin_id: str, email: str, company_role: str):
        """Company admin creates a user under their company."""
        # Prevent duplicates
        if await self.users.find_one({"company_id": company_id, "email": email}):
            raise ValueError("User with this email already exists in this company")

        user_doc = {
            "user_id": str(uuid.uuid4()),
            "company_id": company_id,
            "added_by_admin_id": added_by_admin_id,  # tracking who added
            "email": email,
            "company_role": company_role,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "name": None,  # filled later by user
        }
        await self.users.insert_one(user_doc)

        return {
            "user_id": user_doc["user_id"],
            "company_id": company_id,
            "email": email,
            "company_role": company_role,
            "added_by_admin_id": added_by_admin_id,
            "name": None,
            "documents": [],
        }

    async def update_user(self, company_id: str, user_id: str, name: str, email: str, company_role: str) -> bool:
        now = datetime.utcnow()
        target_is_admin = company_role == "company_admin"

        # Fetch existing docs (could exist in either collection)
        admin_doc = await self.admins.find_one({"company_id": company_id, "user_id": user_id})
        user_doc = await self.users.find_one({"company_id": company_id, "user_id": user_id})

        # --- Target: admin ---
        if target_is_admin:
            # 1) Already an admin -> update fields, remove stray user doc if exists
            if admin_doc:
                await self.admins.update_one(
                    {"company_id": company_id, "user_id": user_id},
                    {"$set": {"name": name, "email": email, "updated_at": now}}
                )
                if user_doc:
                    await self.users.delete_one({"company_id": company_id, "user_id": user_id})
                return True

            # 2) Exists as user -> move user -> admin (preserve created_at if present)
            if user_doc:
                created_at = user_doc.get("created_at", now)
                new_admin = {
                    "company_id": company_id,
                    "user_id": user_id,
                    "name": name,
                    "email": email,
                    "modules": copy.deepcopy(DEFAULT_MODULES),
                    "created_at": created_at,
                    "updated_at": now,
                }
                # Insert admin, then remove user doc
                await self.admins.insert_one(new_admin)
                await self.users.delete_one({"company_id": company_id, "user_id": user_id})
                return True

            # 3) Not found anywhere -> create admin record (preserve user_id)
            new_admin = {
                "company_id": company_id,
                "user_id": user_id,
                "name": name,
                "email": email,
                "modules": copy.deepcopy(DEFAULT_MODULES),
                "created_at": now,
                "updated_at": now,
            }
            await self.admins.insert_one(new_admin)
            return True

        # --- Target: company_user ---
        else:
            # 1) Already a user -> update fields, remove stray admin doc if exists
            if user_doc:
                await self.users.update_one(
                    {"company_id": company_id, "user_id": user_id},
                    {"$set": {"name": name, "email": email, "company_role": company_role, "updated_at": now}}
                )
                if admin_doc:
                    await self.admins.delete_one({"company_id": company_id, "user_id": user_id})
                return True

            # 2) Exists as admin -> move admin -> user (preserve created_at if present)
            if admin_doc:
                created_at = admin_doc.get("created_at", now)
                # If admin has info about who added them, preserve if meaningful.
                added_by_admin_id = admin_doc.get("user_id")  # fallback (could be None)
                new_user = {
                    "company_id": company_id,
                    "user_id": user_id,
                    "name": name,
                    "email": email,
                    "company_role": company_role,
                    "added_by_admin_id": added_by_admin_id,
                    "created_at": created_at,
                    "updated_at": now,
                }
                await self.users.insert_one(new_user)
                await self.admins.delete_one({"company_id": company_id, "user_id": user_id})
                return True

            # 3) Not found anywhere -> create user record
            new_user = {
                "company_id": company_id,
                "user_id": user_id,
                "name": name,
                "email": email,
                "company_role": company_role,
                "added_by_admin_id": None,
                "created_at": now,
                "updated_at": now,
            }
            await self.users.insert_one(new_user)
            return True


    # ---------------- Debug / Inspection ---------------- #
    async def get_all_collections_data(self) -> dict:
        """Return all documents from companies, admins, users, and documents collections."""
        companies = await self.companies.find().to_list(None)
        admins = await self.admins.find().to_list(None)
        users = await self.users.find().to_list(None)
        documents = await self.documents.find().to_list(None)

        # Convert ObjectId and datetime objects to strings for JSON safety
        def serialize(obj):
            if isinstance(obj, datetime):
                return obj.isoformat()
            if isinstance(obj, dict):
                return {k: serialize(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [serialize(v) for v in obj]
            return str(obj)

        return {
            "companies": serialize(companies),
            "company_admins": serialize(admins),
            "company_users": serialize(users),
            "documents": serialize(documents),
        }

    async def clear_all_data(self) -> dict:
        """⚠️ DANGER: Delete all data in all collections."""
        await self.companies.delete_many({})
        await self.admins.delete_many({})
        await self.users.delete_many({})
        await self.documents.delete_many({})
        return {"status": "All collections cleared"}

