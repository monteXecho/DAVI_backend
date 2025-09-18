from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorDatabase
from typing import Optional

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
                        "name": admin["name"],
                        "email": admin["email"],
                        "modules": [
                            {"name": k, "desc": v["desc"], "enabled": v["enabled"]}
                            for k, v in admin["modules"].items()
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
            "company_id": None,  # filled in reindex
            "admins": [],
            "created_at": now,
            "updated_at": now,
        }
        await self.collection.insert_one(doc)
        await self._reindex_companies()
        return {"name": name}


    async def add_admin(self, company_id: str, name: str, email: str, modules: Optional[dict]) -> dict:
        admin_modules = DEFAULT_MODULES.copy()
        if modules:
            for k, v in modules.items():
                if k in admin_modules:
                    admin_modules[k]["enabled"] = v.get("enabled", False)

        admin_doc = {
            "admin_id": None,  # filled in reindex
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
        """Update module enabled flags for a specific admin."""
        company = await self.collection.find_one({"company_id": company_id})
        if not company:
            return None

        admins = company.get("admins", [])
        for admin in admins:
            if admin.get("admin_id") == admin_id:
                # update only provided modules, keep desc intact
                for k, v in modules.items():
                    if k in admin["modules"]:
                        admin["modules"][k]["enabled"] = v.get("enabled", False)
                break
        else:
            return None  # admin not found

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
