import logging
import uuid
import copy
import os
import re
import io
import aiofiles
import pandas as pd
import shutil
from fastapi import HTTPException, UploadFile
from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorDatabase
from typing import Optional
from pymongo.errors import DuplicateKeyError
from collections import defaultdict
from typing import List

logger = logging.getLogger(__name__)

BASE_DOC_URL = "https://your-backend.com/documents/download"

# Base path for all uploads
UPLOAD_ROOT = "/app/uploads/documents"

# Ensure root folder exists at startup
os.makedirs(UPLOAD_ROOT, exist_ok=True)

DEFAULT_MODULES = {
    "Documenten chat": {"desc": "AI-zoek & Q&A over geuploade documenten.", "enabled": False},
    "GGD Checks": {"desc": "Automatische GGD-controles, inclusief BKR-bewaking, afwijkingslogica en rapportage.", "enabled": False}
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
        self.folders = db.company_folders
        self.guest_access = db.company_guest_access

    async def get_admins_by_company(self, company_id: str):
        return await self.admins.find({"company_id": company_id}).to_list(None)

    async def get_users_by_company(self, company_id: str):
        return await self.users.find({"company_id": company_id}).to_list(None)

    async def get_users_by_company_admin(self, admin_id: str):
        return await self.users.find({"added_by_admin_id": admin_id}, {"_id": 0}).to_list(None)

    async def get_admin_by_id(self, company_id: str, user_id: str):
        return await self.admins.find_one({"company_id": company_id, "user_id": user_id})
    
    async def get_role_by_name(self, company_id: str, admin_id: str, role_name: str):
        return await self.roles.find_one({"company_id": company_id, "added_by_admin_id": admin_id, "name": role_name})


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
    async def add_admin(self, company_id: str, admin_id: str, name: str, email: str, modules: Optional[dict] = None):
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
            "added_by_admin_id": admin_id,
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
            "documents": [], 
        }

    async def reassign_admin(self, company_id: str, admin_id: str, name: str, email: str):
        # Check if another admin already uses this email
        existing = await self.admins.find_one({
            "company_id": company_id,
            "email": email,
        })
        if existing:
            raise ValueError("Admin with this email already exists")

        # Update only the fields you want to change
        update_result = await self.admins.update_one(
            {"company_id": company_id, "user_id": admin_id},
            {
                "$set": {
                    "name": name,
                    "email": email,
                    "updated_at": datetime.utcnow(),
                }
            }
        )

        if update_result.matched_count == 0:
            raise ValueError("Admin not found")

        # Return updated fields
        updated_admin = await self.admins.find_one({"company_id": company_id, "user_id": admin_id})

        return {
            "user_id": updated_admin["user_id"],
            "company_id": updated_admin["company_id"],
            "name": updated_admin["name"],
            "email": updated_admin["email"],
            "modules": serialize_modules(updated_admin["modules"]),
            "documents": updated_admin.get("documents", []),
        }


    async def delete_admin(self, company_id: str, user_id: str, admin_id: str = None) -> bool:
        """
        Delete an admin. If admin_id is provided, only deletes if the admin was added by admin_id.
        """
        query = {"company_id": company_id, "user_id": user_id}
        if admin_id:
            # Only delete if added by this admin
            query["added_by_admin_id"] = admin_id
        
        admin = await self.admins.find_one(query)
        if not admin:
            if admin_id:
                raise HTTPException(
                    status_code=403,
                    detail="You can only delete admins that you added."
                )
            return False

        result = await self.admins.delete_one({"company_id": company_id, "user_id": user_id})
        if result.deleted_count > 0:
            await self.documents.delete_many({"user_id": admin["user_id"]})
            # Also delete any guest_access entries where this admin is the guest
            await self.guest_access.delete_many({
                "company_id": company_id,
                "guest_user_id": user_id
            })
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

    async def find_user_by_email(self, email: str):
        user = await self.users.find_one({"email": email})
        if not user:
            return None
        return {"email": user["email"], "role": "company_user"}

    async def get_all_private_documents(self, email: str, document_type: str):
        user_rec = await self.users.find_one({"email": email})
        admin_rec = await self.admins.find_one({"email": email})

        user = user_rec or admin_rec

        if not user:
            return {"documents": []}

        user_id = user["user_id"]
        if not user_id:
            return {"documents": []}

        docs_cursor = self.documents.find({
            "user_id": user_id,
            "upload_type": document_type
        })

        docs = await docs_cursor.to_list(length=None)

        if not docs:
            return {"documents": []}

        result = {"documents": []}

        for doc in docs:
            result["documents"].append({
                "file_name": doc.get("file_name"),
                "upload_type": doc.get("upload_type")
            })

        return result


    async def get_admin_documents(self, company_id: str, admin_id: str):
        try:
            roles_cursor = self.roles.find({
                "company_id": company_id,
                "added_by_admin_id": admin_id
            })
            roles = await roles_cursor.to_list(None)
            
            if not roles:
                return {}
            
            folders_cursor = self.folders.find({
                "company_id": company_id,
                "admin_id": admin_id
            })
            all_folders = await folders_cursor.to_list(None)
            folder_names = [folder["name"] for folder in all_folders]
            
            if not folder_names:
                return {}
            
            docs_cursor = self.documents.find({
                "company_id": company_id,
                "user_id": admin_id,
                "upload_type": {"$in": folder_names}
            })
            documents = await docs_cursor.to_list(None)
            
            users_cursor = self.users.find({
                "company_id": company_id,
                "added_by_admin_id": admin_id
            })
            users = await users_cursor.to_list(None)
            
            role_folders_map = defaultdict(set)
            for role in roles:
                role_name = role["name"]
                for folder_name in role.get("folders", []):
                    role_folders_map[role_name].add(folder_name)
            
            folder_docs_map = defaultdict(list)
            for doc in documents:
                folder_name = doc.get("upload_type")
                if folder_name in folder_names:
                    folder_docs_map[folder_name].append({
                        "file_name": doc.get("file_name"),
                        "path": doc.get("path", ""),
                        "uploaded_at": doc.get("uploaded_at") or doc.get("created_at"),
                        "_id": str(doc.get("_id", ""))
                    })
            
            role_users_map = defaultdict(list)
            for user in users:
                for role_name in user.get("assigned_roles", []):
                    user_info = {
                        "name": user.get("name", ""),
                        "email": user.get("email", "")
                    }
                    if user_info not in role_users_map[role_name]:
                        role_users_map[role_name].append(user_info)
            
            result = {}
            for role in roles:
                role_name = role["name"]
                role_data = {"folders": []}
                
                for folder_name in role_folders_map[role_name]:
                    folder_docs = folder_docs_map.get(folder_name, [])
                    
                    for doc in folder_docs:
                        doc["assigned_to"] = role_users_map.get(role_name, [])
                    
                    folder_entry = {
                        "name": folder_name,
                        "documents": folder_docs
                    }
                    role_data["folders"].append(folder_entry)
                
                if role_data["folders"]:
                    result[role_name] = role_data
            
            return result
            
        except Exception as e:
            print(f"Error in get_admin_documents: {str(e)}")
            return {}


    async def delete_private_documents(
        self,
        email: str,
        documents_to_delete: List[dict]
    ) -> int:

        user_rec = await self.users.find_one({"email": email})
        admin_rec = await self.admins.find_one({"email": email})

        user = user_rec or admin_rec

        user_id = user["user_id"]

        deleted_count = 0

        for doc_info in documents_to_delete:
            file_name = doc_info.get("file_name")

            if not file_name:
                continue 

            try:
                query = {
                    "user_id": user_id,
                    "file_name": file_name,
                    "upload_type": "document"
                }

                document = await self.documents.find_one(query)
                if not document:
                    continue

                file_path = document.get("path")

                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                        logger.info(f"Deleted physical file: {file_path}")
                    except Exception as e:
                        logger.warning(f"Failed to delete physical file {file_path}: {str(e)}")

                delete_result = await self.documents.delete_one({"_id": document["_id"]})
                if delete_result.deleted_count > 0:
                    deleted_count += 1

            except Exception as e:
                logger.error(f"Error deleting document {file_name}: {str(e)}")
                continue

        return deleted_count

    async def delete_documents(
        self,
        company_id: str,
        admin_id: str,
        documents_to_delete: List[dict]
    ) -> int:

        deleted_count = 0

        for doc_info in documents_to_delete:
            file_name = doc_info.get("fileName")
            folder_name = doc_info.get("folderName")
            path = doc_info.get("path")

            if not file_name or not folder_name:
                continue  

            try:
                query = {
                    "company_id": company_id,
                    "user_id": admin_id,
                    "file_name": file_name,
                    "upload_type": folder_name
                }

                if path:
                    query["path"] = path

                document = await self.documents.find_one(query)
                if not document:
                    continue

                file_path = document.get("path")

                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                        logger.info(f"Deleted physical file: {file_path}")
                    except Exception as e:
                        logger.warning(f"Failed to delete physical file {file_path}: {str(e)}")

                delete_result = await self.documents.delete_one({"_id": document["_id"]})
                if delete_result.deleted_count > 0:
                    deleted_count += 1

                    await self.folders.update_one(
                        {"company_id": company_id, 'admin_id': admin_id, "name": folder_name},
                        {"$inc": {"document_count": -1}}
                    )

            except Exception as e:
                logger.error(f"Error deleting document {file_name} for folder {folder_name}: {str(e)}")
                continue

        return deleted_count

    async def get_folders(
        self,
        company_id: str,
        admin_id: str
    ) -> dict:
        # Get existing folders for this company
        existing_folders = await self.folders.find(
            {"company_id": company_id, "admin_id": admin_id}
        ).to_list(length=None)

        # Extract just the folder names
        folder_names = [folder.get("name", "") for folder in existing_folders]

        return {
            "success": True,
            "folders": folder_names,
            "total": len(folder_names)
        }

    async def add_folders(
        self,
        company_id: str,
        admin_id: str,
        folder_names: List[str]
    ) -> dict:
        existing_folders = await self.folders.find(
            {"company_id": company_id, "admin_id": admin_id}
        ).to_list(length=None)
        
        existing_names = {folder["name"].lower() for folder in existing_folders}
        
        duplicated = []
        new_folders = []
        
        for folder_name in folder_names:
            normalized_name = folder_name.lower()
            if normalized_name in existing_names:
                duplicated.append(folder_name)
            else:
                new_folders.append(folder_name)
        
        if new_folders:
            folder_documents = []
            for folder_name in new_folders:
                folder_doc = {
                    "company_id": company_id,
                    "admin_id": admin_id,
                    "name": folder_name.strip(),
                    "document_count": 0,
                    "created_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow(),
                    "status": "active"
                }
                folder_documents.append(folder_doc)
            
            if folder_documents:
                await self.folders.insert_many(folder_documents)
        
        return {
            "success": True,
            "message": "Folders processed successfully",
            "added_folders": new_folders,
            "duplicated_folders": duplicated,
            "total_added": len(new_folders),
            "total_duplicates": len(duplicated)
        }

    async def delete_folders(
        self,
        company_id: str,
        folder_names: List[str],
        role_names: List[str],
        admin_id: str
    ) -> dict:

        deleted_folders = []
        total_documents_deleted = 0

        # Loop through matched role/folder pairs
        for role_name, folder_name in zip(role_names, folder_names):

            # Find the role that contains this folder
            role = await self.roles.find_one({
                "company_id": company_id,
                "added_by_admin_id": admin_id,
                "name": role_name,
                "folders": folder_name   # folder exists in the role's array
            })

            if not role:
                continue

            # Delete all documents belonging to this role/folder
            delete_docs_result = await self.documents.delete_many({
                "company_id": company_id,
                "user_id": admin_id,
                "upload_type": role_name,
                "path": {"$regex": f"/{folder_name}/"}
            })

            total_documents_deleted += delete_docs_result.deleted_count

            # Remove folder from filesystem
            base_path = os.path.join(
                UPLOAD_ROOT,
                "roleBased",
                company_id,
                admin_id,
                role_name,
                folder_name
            )

            if os.path.exists(base_path):
                shutil.rmtree(base_path, ignore_errors=True)

            # Remove folder name from the role document array
            await self.roles.update_one(
                {"_id": role["_id"]},
                {"$pull": {"folders": folder_name}}
            )

            deleted_folders.append({
                "role_name": role_name,
                "folder_name": folder_name
            })

        if not deleted_folders:
            raise HTTPException(status_code=404, detail="No matching folders found")

        return {
            "status": "deleted",
            "deleted_folders": deleted_folders,
            "total_documents_deleted": total_documents_deleted
        }



    async def add_users_from_email_file(
        self,
        company_id: str,
        admin_id: str,
        file_content: bytes,
        file_extension: str,
        selected_role: str = None  # Add selected_role parameter
    ) -> dict:
        """
        Add multiple users from CSV/Excel file containing email addresses.
        Handles emails in column headers, data cells, or anywhere in the file.
        """
        try:
            emails = []
            
            print(f"DEBUG: Processing file with extension: {file_extension}")
            print(f"DEBUG: File size: {len(file_content)} bytes")
            print(f"DEBUG: Selected role: {selected_role}")

            # Get roles based on the selected role logic
            roles_to_assign = await self._get_roles_to_assign(company_id, admin_id, selected_role)
            print(f"DEBUG: Roles to assign: {roles_to_assign}")

            # Read the file
            try:
                if file_extension == '.csv':
                    df = pd.read_csv(io.BytesIO(file_content))
                else:
                    df = pd.read_excel(io.BytesIO(file_content))
                
                print(f"DEBUG: DataFrame shape: {df.shape}")
                print(f"DEBUG: DataFrame columns: {df.columns.tolist()}")
                
                # Strategy 1: Check if emails are in COLUMN HEADERS (your case!)
                column_emails = []
                for col_name in df.columns:
                    col_str = str(col_name).strip()
                    if re.match(r'^[a-zA-Z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}$', col_str):
                        column_emails.append(col_str)
                
                if column_emails:
                    print(f"DEBUG: Found {len(column_emails)} emails in column headers: {column_emails}")
                    emails.extend(column_emails)
                
                # Strategy 2: Check data cells (normal case)
                all_cell_emails = []
                for col in df.columns:
                    # Get non-null values from this column
                    col_data = df[col].dropna()
                    if not col_data.empty:
                        for value in col_data:
                            value_str = str(value).strip()
                            if re.match(r'^[a-zA-Z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}$', value_str):
                                all_cell_emails.append(value_str)
                
                if all_cell_emails:
                    print(f"DEBUG: Found {len(all_cell_emails)} emails in data cells: {all_cell_emails}")
                    emails.extend(all_cell_emails)
                
                # Strategy 3: If no emails found in structured data, try text extraction
                if not emails:
                    print("DEBUG: No emails found in structured data, trying text extraction...")
                    # Convert entire DataFrame to string and extract emails
                    df_text = df.to_string()
                    text_emails = self._extract_emails_from_text(df_text)
                    if text_emails:
                        print(f"DEBUG: Found {len(text_emails)} emails via text extraction: {text_emails}")
                        emails.extend(text_emails)
                
            except Exception as file_error:
                print(f"DEBUG: File read failed: {file_error}")
                # Fallback to raw text parsing
                text_content = file_content.decode('utf-8', errors='ignore')
                emails = self._extract_emails_from_text(text_content)

            print(f"DEBUG: Total found emails: {len(emails)} - {emails}")

            if not emails:
                raise ValueError("No valid email addresses found in the file.")

            # Process the emails
            results = {
                "successful": [],
                "failed": [],
                "duplicates": []
            }

            # Track successful user creations for role assignment counting
            successful_users = []

            for email in emails:
                try:
                    # Check for existing user
                    existing_user = await self.users.find_one({
                        "company_id": company_id,
                        "email": email
                    })

                    if existing_user:
                        results["duplicates"].append({
                            "email": email,
                            "user_id": existing_user.get("user_id")
                        })
                        continue

                    # Use email prefix as name
                    name = email.split('@')[0]

                    # Create user document with role assignment
                    user_doc = {
                        "user_id": str(uuid.uuid4()),
                        "company_id": company_id,
                        "added_by_admin_id": admin_id,
                        "email": email,
                        "name": name,
                        "company_role": "company_user",
                        "assigned_roles": roles_to_assign,  # Assign the determined roles
                        "created_at": datetime.utcnow(),
                        "updated_at": datetime.utcnow(),
                    }

                    await self.users.insert_one(user_doc)

                    # Add to successful users for role counting
                    successful_users.append(user_doc)

                    results["successful"].append({
                        "email": email,
                        "name": name,
                        "user_id": user_doc["user_id"],
                        "assigned_roles": roles_to_assign  # Include assigned roles in response
                    })

                except Exception as e:
                    results["failed"].append({
                        "email": email,
                        "error": str(e)
                    })

            # Update assigned_user_count for roles if there are successful users and roles to assign
            if successful_users and roles_to_assign:
                await self._update_role_user_counts(company_id, admin_id, roles_to_assign, len(successful_users))

            return results

        except Exception as e:
            logger.error(f"Error processing user upload file: {str(e)}")
            raise

    async def _get_roles_to_assign(self, company_id: str, admin_id: str, selected_role: str) -> list:
        """
        Determine which roles to assign based on the selected role logic:
        - "Alle rollen": Assign all roles created by this admin
        - "Zonder rol" or empty: Assign no roles (empty list)
        - Specific role: Validate and assign that specific role if created by this admin
        """
        
        # Case 1: No role or "Zonder rol" - assign empty roles
        if not selected_role or selected_role == "Zonder rol":
            print(f"DEBUG: No roles to assign for selected_role: {selected_role}")
            return []
        
        # Case 2: "Alle rollen" - get all roles created by this admin
        if selected_role == "Alle rollen":
            try:
                # Get all roles created by this admin for this company
                all_admin_roles = await self.roles.find({
                    "company_id": company_id,
                    "added_by_admin_id": admin_id
                }).to_list(length=None)
                
                role_names = [role.get("name") for role in all_admin_roles if role.get("name")]
                print(f"DEBUG: Found {len(role_names)} roles for admin {admin_id}: {role_names}")
                return role_names
                
            except Exception as e:
                print(f"DEBUG: Error fetching all admin roles: {e}")
                return []
        
        # Case 3: Specific role - validate and assign if exists
        try:
            # Check if the specific role exists and was created by this admin
            existing_role = await self.roles.find_one({
                "company_id": company_id,
                "added_by_admin_id": admin_id,
                "name": selected_role
            })
            
            if existing_role:
                print(f"DEBUG: Valid role found: {selected_role}")
                return [selected_role]
            else:
                print(f"DEBUG: Role '{selected_role}' not found or not created by this admin")
                # You might want to handle this case differently - either raise error or proceed without role
                return []
                
        except Exception as e:
            print(f"DEBUG: Error validating specific role '{selected_role}': {e}")
            return []

    async def _update_role_user_counts(self, company_id: str, admin_id: str, roles: list, user_count: int):
        """
        Update the assigned_user_count for roles when users are assigned to them.
        More granular approach that handles missing roles gracefully.
        """
        try:
            print(f"DEBUG: Updating role user counts for {len(roles)} roles with {user_count} users")
            
            updated_roles = []
            failed_roles = []
            
            for role_name in roles:
                try:
                    # First check if the role exists
                    existing_role = await self.roles.find_one({
                        "company_id": company_id,
                        "added_by_admin_id": admin_id,
                        "name": role_name
                    })
                    
                    if not existing_role:
                        print(f"DEBUG: Role '{role_name}' not found, skipping count update")
                        failed_roles.append(role_name)
                        continue
                    
                    # Increment the assigned_user_count for the role
                    result = await self.roles.update_one(
                        {
                            "company_id": company_id,
                            "added_by_admin_id": admin_id,
                            "name": role_name
                        },
                        {
                            "$inc": {"assigned_user_count": user_count},
                            "$set": {"updated_at": datetime.utcnow()}
                        }
                    )
                    
                    if result.modified_count > 0:
                        print(f"DEBUG: Successfully updated assigned_user_count for role '{role_name}' by {user_count}")
                        updated_roles.append(role_name)
                    else:
                        print(f"DEBUG: Failed to update role '{role_name}'")
                        failed_roles.append(role_name)
                        
                except Exception as role_error:
                    print(f"DEBUG: Error updating role '{role_name}': {role_error}")
                    failed_roles.append(role_name)
            
            print(f"DEBUG: Role update summary - Updated: {len(updated_roles)}, Failed: {len(failed_roles)}")
            if failed_roles:
                print(f"DEBUG: Failed to update these roles: {failed_roles}")
                
        except Exception as e:
            print(f"DEBUG: Error in _update_role_user_counts: {e}")

    def _extract_emails_from_text(self, text_content):
        """Extract emails from plain text content"""
        email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
        emails = re.findall(email_pattern, text_content)
        
        # Remove duplicates
        seen = set()
        unique_emails = []
        for email in emails:
            if email not in seen:
                seen.add(email)
                unique_emails.append(email)
        
        return unique_emails


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

    async def delete_users(self, company_id: str, user_ids: list[str], admin_id: str = None) -> int:
        """
        Delete users and decrease role user counts.
        Only deletes users added by the specified admin_id.
        """
        try:
            # 1) First, get all users to be deleted to retrieve their assigned roles
            # Only get users that were added by this admin
            users_to_delete = await self.users.find({
                "company_id": company_id,
                "user_id": {"$in": user_ids},
                "added_by_admin_id": admin_id  # Only delete users added by this admin
            }).to_list(length=None)
            
            print(f"DEBUG: Found {len(users_to_delete)} users to delete")

            if not users_to_delete:
                return 0

            # 2) Delete users from MongoDB
            user_ids_to_delete = [u["user_id"] for u in users_to_delete]
            result = await self.users.delete_many({
                "company_id": company_id,
                "user_id": {"$in": user_ids_to_delete}
            })

            deleted_count = result.deleted_count
            
            if deleted_count > 0:
                # 3) Delete user documents
                await self.documents.delete_many({"user_id": {"$in": user_ids_to_delete}})
                
                # 4) Update role user counts if admin_id is provided
                if admin_id and users_to_delete:
                    await self._decrease_role_user_counts(company_id, admin_id, users_to_delete)

            return deleted_count

        except Exception as e:
            logger.error(f"Error in delete_users: {str(e)}")
            raise


    async def _decrease_role_user_counts(self, company_id: str, admin_id: str, deleted_users: list):
        """
        Decrease the assigned_user_count for roles when users with those roles are deleted.
        """
        try:
            # Count how many users had each role
            role_decrement_counts = {}
            
            for user in deleted_users:
                assigned_roles = user.get("assigned_roles", [])
                for role_name in assigned_roles:
                    if role_name in role_decrement_counts:
                        role_decrement_counts[role_name] += 1
                    else:
                        role_decrement_counts[role_name] = 1
            
            print(f"DEBUG: Role decrement counts: {role_decrement_counts}")
            
            # Update each role's count using $inc with negative value
            updated_roles = []
            failed_roles = []
            
            for role_name, decrement_count in role_decrement_counts.items():
                try:
                    # Use $inc with negative value to decrement
                    result = await self.roles.update_one(
                        {
                            "company_id": company_id,
                            "added_by_admin_id": admin_id,
                            "name": role_name
                        },
                        {
                            "$inc": {"assigned_user_count": -decrement_count},
                            "$set": {"updated_at": datetime.utcnow()}
                        }
                    )
                    
                    if result.modified_count > 0:
                        print(f"DEBUG: Successfully decreased assigned_user_count for role '{role_name}' by {decrement_count}")
                        updated_roles.append(role_name)
                    else:
                        print(f"DEBUG: Role '{role_name}' not found or count not updated")
                        failed_roles.append(role_name)
                        
                except Exception as role_error:
                    print(f"DEBUG: Error updating role '{role_name}': {role_error}")
                    failed_roles.append(role_name)
            
            print(f"DEBUG: Role decrement summary - Updated: {len(updated_roles)}, Failed: {len(failed_roles)}")
            if failed_roles:
                print(f"DEBUG: Failed to update these roles: {failed_roles}")
                
        except Exception as e:
            print(f"DEBUG: Error in _decrease_role_user_counts: {e}")
            # Don't raise the error to avoid failing the entire delete operation

    async def delete_users_by_admin(self, company_id: str, admin_id: str, kc_admin=None) -> int:
        """
        Delete all users added by a specific admin.
        Returns the number of users deleted.
        """
        try:
            # Get all users added by this admin
            admin_users = await self.users.find({
                "company_id": company_id,
                "added_by_admin_id": admin_id
            }).to_list(length=None)
            
            user_emails = [user.get("email") for user in admin_users if user.get("email")]
            user_ids = [user.get("user_id") for user in admin_users if user.get("user_id")]
            
            print(f"DEBUG: Found {len(admin_users)} users added by admin {admin_id}")
            
            if not admin_users:
                return 0

            # Delete users from Keycloak if kc_admin is provided
            keycloak_deleted = 0
            if kc_admin:
                for user in admin_users:
                    email = user.get("email")
                    if email:
                        try:
                            kc_users = kc_admin.get_users(query={"email": email})
                            if kc_users:
                                keycloak_user_id = kc_users[0]["id"]
                                kc_admin.delete_user(keycloak_user_id)
                                keycloak_deleted += 1
                                print(f"DEBUG: Deleted Keycloak user: {email}")
                        except Exception as e:
                            print(f"DEBUG: Failed to delete Keycloak user {email}: {e}")

            # Delete user documents
            if user_ids:
                await self.documents.delete_many({"user_id": {"$in": user_ids}})

            # Delete users from MongoDB
            result = await self.users.delete_many({
                "company_id": company_id,
                "added_by_admin_id": admin_id
            })
            
            print(f"DEBUG: Successfully deleted {result.deleted_count} users added by admin {admin_id}")
            
            return result.deleted_count
            
        except Exception as e:
            logger.error(f"Error deleting users for admin {admin_id}: {str(e)}")
            print(f"DEBUG: Error in delete_users_by_admin: {e}")
            return 0

    async def get_all_users_created_by_admin_id(
        self,
        company_id: str,
        admin_id: str
    ) -> List[dict]:
        """
        Get all users and admins that can be managed by this admin:
        1. Users added by this admin (added_by_admin_id = admin_id)
        2. Admins added by this admin (added_by_admin_id = admin_id)
        3. Admins who have teamlid role assigned by this admin (from guest_access where created_by = admin_id)
        """
        try:
            users = []
            seen_user_ids = set()  # Track to avoid duplicates
            
            # 1. Get regular users added by this admin
            users_cursor = self.users.find({
                "company_id": company_id, 
                "added_by_admin_id": admin_id
            })
            
            async for usr in users_cursor:
                user_id = usr.get("user_id")
                if user_id in seen_user_ids:
                    continue
                seen_user_ids.add(user_id)
                
                user_name = usr.get("name")
                user_email = usr.get("email")
                users.append({
                    "id": user_id,
                    "name": user_name if user_name is not None else "",
                    "email": user_email if user_email is not None else "",
                    "roles": usr.get("assigned_roles", []),
                    "type": "user",
                    "added_by_admin_id": usr.get("added_by_admin_id"),
                    "created_at": usr.get("created_at"),
                    "updated_at": usr.get("updated_at")
                })
            
            # 2. Get admins added by this admin
            admins_cursor = self.admins.find({
                "company_id": company_id,
                "added_by_admin_id": admin_id
            })
            
            async for admin in admins_cursor:
                admin_user_id = admin.get("user_id")
                if admin_user_id in seen_user_ids:
                    continue
                seen_user_ids.add(admin_user_id)
                
                admin_name = admin.get("name")
                admin_email = admin.get("email")
                users.append({
                    "id": admin_user_id,
                    "name": admin_name if admin_name is not None else "",
                    "email": admin_email if admin_email is not None else "",
                    "roles": ["Beheerder"],  
                    "type": "admin",
                    "added_by_admin_id": admin.get("added_by_admin_id"),
                    "is_teamlid": admin.get("is_teamlid", False),
                    "created_at": admin.get("created_at"),
                    "updated_at": admin.get("updated_at")
                })
            
            # 3. Get admins who have teamlid role assigned by this admin
            # Check guest_access collection where created_by = admin_id
            teamlid_assignments = self.guest_access.find({
                "company_id": company_id,
                "created_by": admin_id,
                "is_active": True
            })
            
            async for assignment in teamlid_assignments:
                teamlid_user_id = assignment.get("guest_user_id")
                if teamlid_user_id in seen_user_ids:
                    # Already added, but mark as teamlid if not already marked
                    for user in users:
                        if user.get("id") == teamlid_user_id:
                            user["is_teamlid"] = True
                            user["teamlid_assigned_by"] = admin_id
                            break
                    continue
                
                # Find the admin record for this teamlid
                teamlid_admin = await self.admins.find_one({
                    "company_id": company_id,
                    "user_id": teamlid_user_id
                })
                
                if teamlid_admin:
                    seen_user_ids.add(teamlid_user_id)
                    admin_name = teamlid_admin.get("name")
                    admin_email = teamlid_admin.get("email")
                    users.append({
                        "id": teamlid_user_id,
                        "name": admin_name if admin_name is not None else "",
                        "email": admin_email if admin_email is not None else "",
                        "roles": ["Beheerder"],
                        "type": "admin",
                        "added_by_admin_id": teamlid_admin.get("added_by_admin_id"),
                        "is_teamlid": True,
                        "teamlid_assigned_by": admin_id,  # This admin assigned the teamlid role
                        "created_at": teamlid_admin.get("created_at"),
                        "updated_at": teamlid_admin.get("updated_at")
                    })
            
            def get_sort_key(user):
                type_weight = 0 if user.get("type") == "admin" else 1
                
                name = user.get("name")
                email = user.get("email")
                
                sort_name = ""
                if name:
                    sort_name = name.lower()
                elif email:
                    sort_name = email.lower()
                
                return (type_weight, sort_name)
            
            users.sort(key=get_sort_key)
            
            return users
            
        except Exception as e:
            print(f"Error in get_all_users_created_by_admin_id: {str(e)}")
            return []

    async def remove_teamlid_role(
        self,
        company_id: str,
        admin_id: str,  # The admin who wants to remove the teamlid role
        target_admin_id: str  # The admin whose teamlid role to remove
    ) -> bool:
        """
        Remove teamlid role from an admin.
        Only the admin who assigned the teamlid role can remove it.
        """
        # Check if this admin assigned the teamlid role to the target admin
        guest_entry = await self.guest_access.find_one({
            "company_id": company_id,
            "owner_admin_id": admin_id,  # The workspace owner
            "guest_user_id": target_admin_id,  # The teamlid
            "created_by": admin_id,  # Must be created by this admin
            "is_active": True
        })
        
        if not guest_entry:
            raise HTTPException(
                status_code=403,
                detail="You can only remove teamlid roles that you assigned."
            )
        
        # Deactivate the guest_access entry (soft delete)
        result = await self.guest_access.update_one(
            {
                "company_id": company_id,
                "owner_admin_id": admin_id,
                "guest_user_id": target_admin_id,
            },
            {
                "$set": {
                    "is_active": False,
                    "updated_at": datetime.utcnow()
                }
            }
        )
        
        # Also update the admin document to remove teamlid status if this was their only teamlid assignment
        # Check if there are any other active teamlid assignments for this admin
        other_assignments = await self.guest_access.count_documents({
            "company_id": company_id,
            "guest_user_id": target_admin_id,
            "is_active": True
        })
        
        if other_assignments == 0:
            # No other teamlid assignments, remove teamlid status from admin document
            await self.admins.update_one(
                {
                    "company_id": company_id,
                    "user_id": target_admin_id
                },
                {
                    "$set": {
                        "is_teamlid": False,
                        "updated_at": datetime.utcnow()
                    },
                    "$unset": {
                        "teamlid_permissions": "",
                        "assigned_teamlid_by_id": "",
                        "assigned_teamlid_by_name": "",
                        "assigned_teamlid_at": ""
                    }
                }
            )
        
        return result.modified_count > 0

    # ---------------- Company Roles ---------------- #
    async def add_or_update_role(self, company_id: str, admin_id: str, role_name: str, folders: list[str], modules: list, action: str) -> dict:
        """Add or update a role based on the action parameter."""
        
        folders = [f.strip("/") for f in folders if f.strip()]

        existing_role = await self.roles.find_one({
            "company_id": company_id,
            "added_by_admin_id": admin_id,
            "name": role_name
        })

        if action == "create" and existing_role:
            return {
                "status": "error",
                "error_type": "duplicate_role",
                "message": f"Role '{role_name}' already exists",
                "company_id": company_id,
                "role_name": role_name
            }

        modules_dict = None
        if modules is not None:
            if isinstance(modules, list):
                modules_dict = {}
                for module in modules:
                    if isinstance(module, dict):
                        module_name = module.get('name')
                        if module_name:
                            module_data = {k: v for k, v in module.items() if k != 'name'}
                            modules_dict[module_name] = module_data
                    elif hasattr(module, 'dict'):  
                        module_dict = module.dict()
                        module_name = module_dict.get('name')
                        if module_name:
                            module_data = {k: v for k, v in module_dict.items() if k != 'name'}
                            modules_dict[module_name] = module_data
            else:
                modules_dict = modules

        if existing_role:
            update_data = {
                "folders": folders,
                "updated_at": datetime.utcnow()
            }
            
            if modules_dict is not None:
                update_data["modules"] = modules_dict
            
            await self.roles.update_one(
                {"_id": existing_role["_id"]},
                {"$set": update_data}
            )
            status = "role_updated"
            updated_folders = folders
            final_modules = modules_dict if modules_dict is not None else existing_role.get("modules", {})
        else:
            role_data = {
                "company_id": company_id,
                "name": role_name,
                "added_by_admin_id": admin_id,
                "folders": folders,
                "assigned_user_count": 0,
                "document_count": 0,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }
            
            if modules_dict is not None:
                role_data["modules"] = modules_dict
            
            await self.roles.insert_one(role_data)
            updated_folders = folders
            status = "role_created"
            final_modules = modules_dict if modules_dict is not None else {}

        base_path = os.path.join(UPLOAD_ROOT, "roleBased", company_id, admin_id)
        for folder in updated_folders:
            full_path = os.path.join(base_path, folder)
            os.makedirs(full_path, exist_ok=True)

        return {
            "status": status,
            "company_id": company_id,
            "role_name": role_name,
            "folders": updated_folders,
            "modules": final_modules
        }

    async def list_roles(self, company_id: str, admin_id: str) -> list[dict]:
        """List all roles for a given company."""
        cursor = self.roles.find({"company_id": company_id, "added_by_admin_id": admin_id})
        roles = await cursor.to_list(length=None)
        
        result = []
        
        for r in roles:
            role_folders = r.get("folders", [])
            
            if role_folders:
                folder_cursor = self.folders.find({
                    "company_id": company_id,
                    "admin_id": admin_id,
                    "name": {"$in": role_folders}
                })
                
                folders_data = await folder_cursor.to_list(length=None)
                
                total_document_count = sum(
                    folder.get("document_count", 0) 
                    for folder in folders_data
                )
            else:
                total_document_count = 0
            
            result.append({
                "name": r.get("name"),
                "folders": role_folders,
                "user_count": r.get("assigned_user_count", 0),
                "document_count": total_document_count,
                "modules": r.get("modules", [])
            })
        
        return result

    async def delete_roles(self, company_id: str, role_names: List[str], admin_id: str) -> dict:
        """Delete one or multiple roles by name, remove them from users' assigned_roles, and delete related documents/folders."""

        deleted_roles = []
        total_users_updated = 0
        total_documents_deleted = 0

        for role_name in role_names:
            # --- Verify role exists ---
            role = await self.roles.find_one({"company_id": company_id, "added_by_admin_id": admin_id, "name": role_name})
            if not role:
                # Skip if role doesn't exist, but continue with others
                continue

            # --- Delete all documents uploaded by admin for this role ---
            delete_docs_result = await self.documents.delete_many({
                "company_id": company_id,
                "user_id": admin_id,
                "upload_type": role_name
            })

            # --- Remove related folders ---
            base_path = os.path.join(UPLOAD_ROOT, "roleBased", company_id, admin_id, role_name)
            if os.path.exists(base_path):
                for root, dirs, files in os.walk(base_path, topdown=False):
                    for f in files:
                        try:
                            os.remove(os.path.join(root, f))
                        except Exception:
                            pass
                    for d in dirs:
                        try:
                            os.rmdir(os.path.join(root, d))
                        except Exception:
                            pass
                try:
                    os.rmdir(base_path)
                except Exception:
                    pass

            # --- Remove this role from all users' assigned_roles ---
            update_result = await self.users.update_many(
                {"company_id": company_id, "assigned_roles": role_name},
                {"$pull": {"assigned_roles": role_name}}
            )

            # --- Delete the role itself ---
            await self.roles.delete_one({"_id": role["_id"]})

            # --- Track results for this role ---
            deleted_roles.append(role_name)
            total_users_updated += update_result.modified_count
            total_documents_deleted += delete_docs_result.deleted_count

        if not deleted_roles:
            raise HTTPException(status_code=404, detail="No valid roles found to delete")

        # --- Return cleanup result ---
        return {
            "status": "deleted",
            "deleted_roles": deleted_roles,
            "total_users_updated": total_users_updated,
            "total_documents_deleted": total_documents_deleted
        }

    async def assign_role_to_user(self, company_id: str, user_id: str, role_name: str) -> dict:
        """
        Assign a role to a company user (by user_id).
        Adds the role to 'assigned_roles' array (no duplicates)
        and increments the role's assigned_user_count.
        """

        # --- Verify role exists ---
        role = await self.roles.find_one({"company_id": company_id, "name": role_name})
        if not role:
            raise HTTPException(status_code=404, detail=f"Role '{role_name}' not found")

        # --- Verify company user exists ---
        user = await self.users.find_one({"user_id": user_id, "company_id": company_id})
        if not user:
            raise HTTPException(status_code=404, detail=f"User '{user_id}' not found in this company")

        # --- Prepare assigned roles ---
        assigned_roles = user.get("assigned_roles", [])
        if role_name not in assigned_roles:
            assigned_roles.append(role_name)

            # Update user with the new role list
            await self.users.update_one(
                {"user_id": user_id, "company_id": company_id},
                {
                    "$set": {
                        "assigned_roles": assigned_roles,
                        "updated_at": datetime.utcnow()
                    }
                }
            )

            # Increment the count for the role
            await self.roles.update_one(
                {"_id": role["_id"]},
                {"$inc": {"assigned_user_count": 1}}
            )

            status = "role_assigned"
        else:
            status = "role_already_assigned"

        return {
            "status": status,
            "company_id": company_id,
            "user_id": user_id,
            "assigned_roles": assigned_roles
        }

    async def upload_document_for_folder(
        self,
        company_id: str,
        admin_id: str,
        folder_name: str,
        file: UploadFile
    ) -> dict:

        file_name = (file.filename or "").strip()
        safe_folder = folder_name.strip("/ ")

        if not file_name:
            raise HTTPException(status_code=400, detail="Missing file name")

        base_path = os.path.join(
            UPLOAD_ROOT, "roleBased", company_id, admin_id, safe_folder
        )
        os.makedirs(base_path, exist_ok=True)

        file_path = os.path.join(base_path, file_name)

        existing_doc = await self.documents.find_one({
            "company_id": company_id,
            "user_id": admin_id,
            "upload_type": safe_folder,
            "path": file_path
        })

        if existing_doc:
            raise HTTPException(
                status_code=409,
                detail=f"Het document '{file_name}' bestaat al in de map '{safe_folder}'."
            )

        try:
            async with aiofiles.open(file_path, "wb") as f:
                content = await file.read()
                await f.write(content)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"File save failed: {str(e)}")

        doc_record = {
            "company_id": company_id,
            "user_id": admin_id,
            "file_name": file_name,
            "upload_type": safe_folder,
            "path": file_path,
            "uploaded_at": datetime.utcnow(),
        }

        await self.documents.insert_one(doc_record)

        await self.folders.update_one(
            {"company_id": company_id, "admin_id": admin_id, "name": safe_folder},
            {"$inc": {"document_count": 1}}
        )

        return {
            "success": True,
            "status": "uploaded",
            "folder": safe_folder,
            "file_name": file_name,
            "path": file_path,
        }

    async def delete_roles_by_admin(self, company_id: str, admin_id: str) -> int:
        """
        Delete all roles created by a specific admin.
        Returns the number of roles deleted.
        """
        try:
            # Get all roles created by this admin
            admin_roles = await self.roles.find({
                "company_id": company_id,
                "added_by_admin_id": admin_id
            }).to_list(length=None)
            
            role_names = [role.get("name") for role in admin_roles if role.get("name")]
            print(f"DEBUG: Deleting {len(admin_roles)} roles created by admin {admin_id}: {role_names}")
            
            # Simply delete the roles - no need to update users since they're being deleted too
            result = await self.roles.delete_many({
                "company_id": company_id,
                "added_by_admin_id": admin_id
            })
            
            print(f"DEBUG: Successfully deleted {result.deleted_count} roles created by admin {admin_id}")
            return result.deleted_count
            
        except Exception as e:
            logger.error(f"Error deleting roles for admin {admin_id}: {str(e)}")
            print(f"DEBUG: Error in delete_roles_by_admin: {e}")
            return 0

    async def delete_role_documents_by_admin(self, company_id: str, admin_id: str) -> int:
        """
        Delete all role documents uploaded by a specific admin.
        This includes both database records and actual files from the filesystem.
        Returns the number of documents deleted.
        """
        try:
            # Get all role documents uploaded by this admin
            role_documents = await self.documents.find({
                "company_id": company_id,
                "user_id": admin_id,
                "upload_type": {"$exists": True}  # This indicates role-based documents
            }).to_list(length=None)
            
            print(f"DEBUG: Found {len(role_documents)} role documents uploaded by admin {admin_id}")
            
            if not role_documents:
                return 0

            # Delete actual files from filesystem
            files_deleted = 0
            for doc in role_documents:
                file_path = doc.get("path")
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                        files_deleted += 1
                        print(f"DEBUG: Deleted file: {file_path}")
                        
                        # Also delete the directory if it's empty
                        directory = os.path.dirname(file_path)
                        if os.path.exists(directory) and not os.listdir(directory):
                            os.rmdir(directory)
                            print(f"DEBUG: Deleted empty directory: {directory}")
                            
                    except Exception as e:
                        print(f"DEBUG: Failed to delete file {file_path}: {e}")
            
            # Update document counts for roles
            role_doc_counts = {}
            for doc in role_documents:
                role_name = doc.get("upload_type")
                if role_name:
                    if role_name in role_doc_counts:
                        role_doc_counts[role_name] += 1
                    else:
                        role_doc_counts[role_name] = 1
            
            # Decrement document_count for each role
            for role_name, doc_count in role_doc_counts.items():
                try:
                    await self.roles.update_one(
                        {"company_id": company_id, "name": role_name},
                        {"$inc": {"document_count": -doc_count}}
                    )
                    print(f"DEBUG: Decreased document_count for role '{role_name}' by {doc_count}")
                except Exception as e:
                    print(f"DEBUG: Failed to update document_count for role '{role_name}': {e}")

            # Delete database records
            result = await self.documents.delete_many({
                "company_id": company_id,
                "user_id": admin_id,
                "upload_type": {"$exists": True}
            })
            
            print(f"DEBUG: Successfully deleted {result.deleted_count} role document records from database")
            print(f"DEBUG: Successfully deleted {files_deleted} actual files from filesystem")
            
            return result.deleted_count
            
        except Exception as e:
            logger.error(f"Error deleting role documents for admin {admin_id}: {str(e)}")
            print(f"DEBUG: Error in delete_role_documents_by_admin: {e}")
            return 0

    async def delete_company_role_documents(self, company_id: str) -> int:
        """
        Delete all role-based documents for a company.
        Simple delete - no need to update role counts since roles are being deleted.
        Returns the number of documents deleted.
        """
        try:
            # Get all role documents for this company
            role_documents = await self.documents.find({
                "company_id": company_id,
                "upload_type": {"$exists": True}  # This indicates role-based documents
            }).to_list(length=None)
            
            print(f"DEBUG: Found {len(role_documents)} role documents for company {company_id}")
            
            if not role_documents:
                return 0

            # Delete actual files from filesystem
            files_deleted = 0
            for doc in role_documents:
                file_path = doc.get("path")
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                        files_deleted += 1
                        print(f"DEBUG: Deleted file: {file_path}")
                    except Exception as e:
                        print(f"DEBUG: Failed to delete file {file_path}: {e}")

            # Delete database records
            result = await self.documents.delete_many({
                "company_id": company_id,
                "upload_type": {"$exists": True}
            })
            
            print(f"DEBUG: Successfully deleted {result.deleted_count} role document records from database")
            print(f"DEBUG: Successfully deleted {files_deleted} actual role document files from filesystem")
            
            return result.deleted_count
            
        except Exception as e:
            logger.error(f"Error deleting role documents for company {company_id}: {str(e)}")
            print(f"DEBUG: Error in delete_company_role_documents: {e}")
            return 0

    # ---------------- Shared ---------------- #
    async def get_user_with_documents(self, email: str):
        user = await self.admins.find_one({"email": email})
        user_type = "admin"
        if not user:
            user = await self.users.find_one({"email": email})
            user_type = "company_user"

        if not user:
            return None

        user_id = user["user_id"]
        company_id = user["company_id"]

        query = {"user_id": user_id}
        owned_docs_cursor = self.documents.find(query)
        owned_docs = [d async for d in owned_docs_cursor]

        role_based_docs = []
        if user_type == "company_user":
            assigned_roles = user.get("assigned_roles", [])
            added_by_admin_id = user.get("added_by_admin_id")

            if assigned_roles and added_by_admin_id:
                role_query = {
                    "user_id": added_by_admin_id,
                    "company_id": company_id,
                    "upload_type": {"$in": assigned_roles},
                }
                role_cursor = self.documents.find(role_query)
                role_based_docs = [d async for d in role_cursor]

        all_docs = owned_docs + role_based_docs
        formatted_docs = [
            {
                "file_name": doc["file_name"],
                "upload_type": doc.get("upload_type", "document"),
                "path": doc.get("path", ""),
            }
            for doc in all_docs
        ]

        pass_ids = []
        for doc in formatted_docs:
            fn = doc["file_name"]
            upload_type = doc.get("upload_type", "document")

            if upload_type == "document":
                pid = f"{user_id}--{fn}"
            else:
                pid = f"{company_id}-{user.get('added_by_admin_id', user_id)}--{fn}"
            pass_ids.append(pid)

        return {
            "user_id": user_id,
            "company_id": company_id,
            "user_type": user_type,
            "documents": formatted_docs,
            "pass_ids": pass_ids,
        }

    async def get_admin_with_documents_by_id(
        self,
        company_id: str,
        admin_user_id: str,
    ):
        """
        Returns the same structure as get_user_with_documents, but for a known admin user_id.
        """
        admin = await self.admins.find_one(
            {"company_id": company_id, "user_id": admin_user_id}
        )
        if not admin:
            return None

        email = admin["email"]
        return await self.get_user_with_documents(email)


    async def add_user_by_admin(self, company_id: str, added_by_admin_id: str, email: str, company_role: str, assigned_role: str):
        if await self.users.find_one({"company_id": company_id, "added_by_admin_id": added_by_admin_id, "email": email}):
            raise ValueError("User with this email already exists in this company")

        # Handle empty assigned_role
        assigned_roles_list = [assigned_role] if assigned_role and assigned_role.strip() else []

        user_doc = {
            "user_id": str(uuid.uuid4()),
            "company_id": company_id,
            "added_by_admin_id": added_by_admin_id,  
            "email": email, 
            "company_role": company_role,
            "assigned_roles": assigned_roles_list,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "name": None,  
        }
        await self.users.insert_one(user_doc)

        # Update role user count if a role was assigned
        if assigned_roles_list:
            await self._update_role_user_counts(company_id, added_by_admin_id, assigned_roles_list, 1)

        return {
            "user_id": user_doc["user_id"],
            "company_id": company_id,
            "email": email,
            "company_role": company_role,
            "added_by_admin_id": added_by_admin_id,
            "assigned_roles": user_doc["assigned_roles"],
            "name": None,
            "documents": [],
        }

    async def assign_teamlid_permissions(self, company_id: str, admin_id: str, email: str, permissions: dict) -> bool:
        """
        Assign teamlid permissions to a user for a specific admin's workspace.
        Supports multiple teamlid roles - each assignment creates a separate guest_access entry.
        """
        current_admin = await self.admins.find_one({
            "company_id": company_id,
            "user_id": admin_id
        })  

        current_admin_name = current_admin.get("name", "Een beheerder")

        target_in_admins = await self.admins.find_one({
            "company_id": company_id,
            "email": email
        })

        target_in_users = await self.users.find_one({
            "company_id": company_id,
            "email": email
        })

        target_user = target_in_admins or target_in_users
        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")

        # CRITICAL: Only company admins can receive teamlid roles, not regular company users
        if target_in_users and not target_in_admins:
            raise HTTPException(
                status_code=400,
                detail="Teamlid roles can only be assigned to company admins, not regular company users."
            )

        target_user_id = target_user["user_id"]
        target_collection = self.admins  # Always use admins collection since only admins can be teamlids

        # Update user document to mark as teamlid (for backward compatibility)
        # Note: This stores the LAST assignment, but we use guest_access for actual permissions
        update_result = await target_collection.update_one(
            {"company_id": company_id, "email": email},
            {
                "$set": {
                    "is_teamlid": True,
                    "teamlid_permissions": permissions,  # Keep for backward compatibility
                    "assigned_teamlid_by_id": admin_id,  # Keep for backward compatibility
                    "assigned_teamlid_by_name": current_admin_name,
                    "assigned_teamlid_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow(),
                }
            },
        )

        if update_result.modified_count == 0:
            raise HTTPException(status_code=500, detail="Failed to update user")

        # Create/update guest_access entry for this specific teamlid assignment
        # This allows multiple teamlid roles from different admins
        # Each assignment creates a separate entry, so multiple admins can assign teamlid roles
        
        # Helper to convert permission value to boolean (handles string "True"/"False")
        def to_bool(value):
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.lower() in ("true", "1", "yes")
            return bool(value)
        
        role_folder = permissions.get("role_folder_modify_permission", False)
        user_modify = permissions.get("user_create_modify_permission", False)
        document_modify = permissions.get("document_modify_permission", False)
        
        # Map teamlid permissions to guest_access format
        # Note: Each call creates/updates a separate entry for this admin_id + target_user_id pair
        await self.upsert_guest_access(
            company_id=company_id,
            owner_admin_id=admin_id,  # The admin whose workspace this teamlid can access
            guest_user_id=target_user_id,
            can_role_write=to_bool(role_folder),
            can_user_read=to_bool(user_modify),  # Note: teamlid uses user_create_modify_permission
            can_document_read=to_bool(document_modify),
            can_folder_write=to_bool(role_folder),
            created_by=admin_id,
        )

        return True

    async def update_user(
        self, 
        company_id: str, 
        user_id: str, 
        user_type: str, 
        name: str, 
        email: str, 
        assigned_roles: list[str]
    ) -> bool:
        """Update user or admin information."""
        now = datetime.utcnow()

        if user_type == "admin":
            admin_doc = await self.admins.find_one({
                "company_id": company_id, 
                "user_id": user_id
            })
            
            if not admin_doc:
                raise HTTPException(status_code=404, detail="Admin not found")

            await self.admins.update_one(
                {"company_id": company_id, "user_id": user_id},
                {
                    "$set": {
                        "name": name,
                        "email": email,
                        "updated_at": now,
                    }
                },
            )
            
            return True

        else:
            user_doc = await self.users.find_one({
                "company_id": company_id, 
                "user_id": user_id
            })
            
            if not user_doc:
                raise HTTPException(status_code=404, detail="User not found")

            old_roles = set(user_doc.get("assigned_roles", []))
            new_roles = set(assigned_roles)

            added_roles = new_roles - old_roles
            removed_roles = old_roles - new_roles

            await self.users.update_one(
                {"company_id": company_id, "user_id": user_id},
                {
                    "$set": {
                        "name": name,
                        "email": email,
                        "assigned_roles": list(new_roles),
                        "updated_at": now,
                    }
                },
            )

            if added_roles:
                await self.roles.update_many(
                    {"company_id": company_id, "name": {"$in": list(added_roles)}},
                    {"$inc": {"assigned_user_count": 1}},
                )

            if removed_roles:
                await self.roles.update_many(
                    {"company_id": company_id, "name": {"$in": list(removed_roles)}},
                    {"$inc": {"assigned_user_count": -1}},
                )

            return True

    # ---------------- Guest Access ---------------- #
    async def upsert_guest_access(
        self,
        company_id: str,
        owner_admin_id: str,
        guest_user_id: str,
        can_role_write: bool,
        can_user_read: bool,
        can_document_read: bool,
        can_folder_write: bool,
        created_by: str,
    ) -> dict:
        """
        Create or update guest access for a given (owner_admin, guest_user) pair.
        """
        now = datetime.utcnow()
        doc = {
            "company_id": company_id,
            "owner_admin_id": owner_admin_id,
            "guest_user_id": guest_user_id,
            "can_role_write": can_role_write,
            "can_user_read": can_user_read,
            "can_document_read": can_document_read,
            "can_folder_write": can_folder_write,
            "is_active": True,
            "updated_at": now,
            "created_by": created_by,
        }

        await self.guest_access.update_one(
            {
                "company_id": company_id,
                "owner_admin_id": owner_admin_id,
                "guest_user_id": guest_user_id,
            },
            {
                "$set": doc,
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        return doc

    async def list_guest_workspaces_for_user(
        self,
        company_id: str,
        guest_user_id: str,
    ) -> list[dict]:
        """
        Return all active guest-access entries for given user in a company.
        """
        cursor = self.guest_access.find(
            {
                "company_id": company_id,
                "guest_user_id": guest_user_id,
                "is_active": True,
            }
        )
        return [doc async for doc in cursor]

    async def get_guest_access(
        self,
        company_id: str,
        guest_user_id: str,
        owner_admin_id: str,
    ) -> Optional[dict]:
        return await self.guest_access.find_one(
            {
                "company_id": company_id,
                "guest_user_id": guest_user_id,
                "owner_admin_id": owner_admin_id,
                "is_active": True,
            }
        )

    async def disable_guest_access(
        self,
        company_id: str,
        owner_admin_id: str,
        guest_user_id: str,
    ) -> int:
        res = await self.guest_access.update_one(
            {
                "company_id": company_id,
                "owner_admin_id": owner_admin_id,
                "guest_user_id": guest_user_id,
            },
            {"$set": {"is_active": False, "updated_at": datetime.utcnow()}},
        )
        return res.modified_count

    # ---------------- Debug / Inspection ---------------- #
    async def get_all_collections_data(self) -> dict:
        """Return all documents from companies, admins, users, and documents collections."""
        companies = await self.companies.find().to_list(None)
        admins = await self.admins.find().to_list(None)
        users = await self.users.find().to_list(None)
        documents = await self.documents.find().to_list(None)
        roles = await self.roles.find().to_list(None)
        folders = await self.folders.find().to_list(None)
        guests = await self.guest_access.find().to_list(None)

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
            "roles": serialize(roles),
            "folders": serialize(folders),
            "guest_access": serialize(guests)
        }

    async def clear_all_data(self) -> dict:
        await self.companies.delete_many({})
        await self.admins.delete_many({})
        await self.users.delete_many({})
        await self.documents.delete_many({})    
        await self.roles.delete_many({})
        await self.folders.delete_many({})
        await self.guest_access.delete_many({})
        return {"status": "All collections cleared"}
