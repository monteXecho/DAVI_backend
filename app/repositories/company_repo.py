import logging
import uuid
import copy
import os
import re
import io
import aiofiles
import pandas as pd
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

    async def find_user_by_email(self, email: str):
        user = await self.users.find_one({"email": email})
        if not user:
            return None
        return {"email": user["email"], "role": "company_user"}

    async def get_admin_documents(self, company_id: str, admin_id: str):
        """
        Get all documents uploaded by the admin, grouped by role and folder,
        including which users (by role) have access.
        """
        # --- Step 1: Fetch all admin documents ---
        docs_cursor = self.documents.find({
            "company_id": company_id,
            "user_id": admin_id
        })
        docs = await docs_cursor.to_list(None)

        # Return empty dict if no documents found instead of raising exception
        if not docs:
            return {}

        result = defaultdict(lambda: {"folders": []})

        # --- Step 2: Group documents by upload_type (role name) ---
        for doc in docs:
            upload_type = doc.get("upload_type")
            path = doc.get("path", "")
            file_name = doc.get("file_name")

            # Skip invalid documents
            if not upload_type or upload_type == "document":
                continue

            # Try to extract folder name relative to the upload_type
            folder_name = "Uncategorized"
            try:
                parts = path.split(f"/{upload_type}/")[1].split("/")
                if len(parts) > 1:
                    folder_name = os.path.join(*parts[:-1])
            except Exception:
                pass

            # Find or create folder entry
            folder_entry = next(
                (f for f in result[upload_type]["folders"] if f["name"] == folder_name),
                None
            )
            if not folder_entry:
                folder_entry = {"name": folder_name, "documents": []}
                result[upload_type]["folders"].append(folder_entry)

            folder_entry["documents"].append({
                "file_name": file_name,
                "path": path,
                "uploaded_at": doc.get("uploaded_at") or doc.get("created_at"),
                "assigned_to": []
            })

        # --- Step 3: Find all users who belong to any of these roles ---
        role_names = list(result.keys())
        if role_names:  # Only query users if we have roles with documents
            users_cursor = self.users.find({
                "company_id": company_id,
                "assigned_roles": {"$in": role_names}
            })
            users = await users_cursor.to_list(None)

            # --- Step 4: Map users to roles ---
            for user in users:
                user_id = user.get("user_id")
                user_name = user.get("name")
                user_email = user.get('email')
                for role in user.get("assigned_roles", []):
                    if role in result:
                        for folder in result[role]["folders"]:
                            for doc_entry in folder["documents"]:
                                doc_entry["assigned_to"].append({"name": user_name, "email": user_email})

        # --- Step 5: Return structured response ---
        return dict(result)

    async def delete_documents(
        self,
        company_id: str,
        admin_id: str,
        documents_to_delete: List[dict]
    ) -> int:
        """
        Delete multiple documents by file name and role.
        
        Args:
            company_id: The company ID
            admin_id: The admin ID who uploaded the documents
            documents_to_delete: List of dicts with 'fileName' and 'role' keys
        
        Returns:
            Number of documents successfully deleted
        """
        deleted_count = 0

        for doc_info in documents_to_delete:
            file_name = doc_info.get("fileName")
            role_name = doc_info.get("role")
            path = doc_info.get("path")

            if not file_name or not role_name:
                continue  # Skip invalid entries

            try:
                # Build query to find the document
                query = {
                    "company_id": company_id,
                    "user_id": admin_id,
                    "file_name": file_name,
                    "upload_type": role_name
                }

                # If path is provided, use it for more precise matching
                if path:
                    query["path"] = path

                # Find the document to get its path for file deletion
                document = await self.documents.find_one(query)
                if not document:
                    continue

                file_path = document.get("path")

                # Delete the physical file
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                        logger.info(f"Deleted physical file: {file_path}")
                    except Exception as e:
                        logger.warning(f"Failed to delete physical file {file_path}: {str(e)}")

                # Delete the database record
                delete_result = await self.documents.delete_one({"_id": document["_id"]})
                if delete_result.deleted_count > 0:
                    deleted_count += 1

                    # Decrement document count for the role
                    await self.roles.update_one(
                        {"company_id": company_id, "name": role_name},
                        {"$inc": {"document_count": -1}}
                    )

            except Exception as e:
                logger.error(f"Error deleting document {file_name} for role {role_name}: {str(e)}")
                continue

        return deleted_count

    async def add_users_from_email_file(
        self,
        company_id: str,
        admin_id: str,
        file_content: bytes,
        file_extension: str
    ) -> dict:
        """
        Add multiple users from CSV/Excel file containing email addresses.
        Handles emails in column headers, data cells, or anywhere in the file.
        """
        try:
            emails = []
            
            print(f"DEBUG: Processing file with extension: {file_extension}")
            print(f"DEBUG: File size: {len(file_content)} bytes")

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

                    # Create user document
                    user_doc = {
                        "user_id": str(uuid.uuid4()),
                        "company_id": company_id,
                        "added_by_admin_id": admin_id,
                        "email": email,
                        "name": name,
                        "company_role": "company_user",
                        "assigned_roles": [],
                        "created_at": datetime.utcnow(),
                        "updated_at": datetime.utcnow(),
                    }

                    await self.users.insert_one(user_doc)

                    results["successful"].append({
                        "email": email,
                        "name": name,
                        "user_id": user_doc["user_id"]
                    })

                except Exception as e:
                    results["failed"].append({
                        "email": email,
                        "error": str(e)
                    })

            return results

        except Exception as e:
            logger.error(f"Error processing user upload file: {str(e)}")
            raise

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

    async def delete_users(self, company_id: str, user_ids: list[str]) -> int:
        result = await self.users.delete_many({
            "company_id": company_id,
            "user_id": {"$in": user_ids}
        })

        if result.deleted_count > 0:
            await self.documents.delete_many({"user_id": {"$in": user_ids}})

        return result.deleted_count
    

    # ---------------- Company Roles ---------------- #
    async def add_or_update_role(self, company_id: str, admin_id: str, role_name: str, folders: list[str]) -> dict:
        """
        Create or update a company role with given subfolders.
        Ensures folders exist on disk under /app/uploads/documents/roleBased/{company_id}/.
        Also removes deleted folders and their documents.
        """
        folders = [f.strip("/") for f in folders if f.strip()]

        existing_role = await self.roles.find_one({
            "company_id": company_id,
            "name": role_name
        })

        if existing_role:
            # Get current folders to identify which ones were removed
            current_folders = set(existing_role.get("folders", []))
            new_folders = set(folders)
            
            # Find folders that were removed
            removed_folders = current_folders - new_folders
            
            # Delete documents and folders that were removed
            for removed_folder in removed_folders:
                # Delete documents associated with the removed folder
                folder_path_pattern = os.path.join(
                    UPLOAD_ROOT, "roleBased", company_id, admin_id, role_name, removed_folder
                )
                
                # Delete database records for documents in the removed folder
                delete_docs_result = await self.documents.delete_many({
                    "company_id": company_id,
                    "user_id": admin_id,
                    "upload_type": role_name,
                    "path": {"$regex": f".*{re.escape(removed_folder)}.*"}
                })
                
                # Delete the physical folder and its contents
                folder_full_path = os.path.join(
                    UPLOAD_ROOT, "roleBased", company_id, admin_id, role_name, removed_folder
                )
                if os.path.exists(folder_full_path):
                    for root, dirs, files in os.walk(folder_full_path, topdown=False):
                        for file in files:
                            try:
                                os.remove(os.path.join(root, file))
                            except Exception as e:
                                logger.warning(f"Failed to delete file {file}: {str(e)}")
                        for dir in dirs:
                            try:
                                os.rmdir(os.path.join(root, dir))
                            except Exception as e:
                                logger.warning(f"Failed to delete directory {dir}: {str(e)}")
                    try:
                        os.rmdir(folder_full_path)
                    except Exception as e:
                        logger.warning(f"Failed to delete folder {folder_full_path}: {str(e)}")
                
                logger.info(f"Removed folder '{removed_folder}' and deleted {delete_docs_result.deleted_count} documents")

            # Update the role with the new folder list (complete replacement)
            await self.roles.update_one(
                {"_id": existing_role["_id"]},
                {"$set": {"folders": folders, "updated_at": datetime.utcnow()}}
            )
            status = "role_updated"
            updated_folders = folders
        else:
            await self.roles.insert_one({
                "company_id": company_id,
                "name": role_name,
                "added_by_admin_id": admin_id,
                "folders": folders,
                "assigned_user_count": 0,
                "document_count": 0,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            })
            updated_folders = folders
            status = "role_created"

        # Ensure new folders exist on disk
        base_path = os.path.join(UPLOAD_ROOT, "roleBased", company_id, admin_id, role_name)
        for folder in updated_folders:
            full_path = os.path.join(base_path, folder)
            os.makedirs(full_path, exist_ok=True)

        return {
            "status": status,
            "company_id": company_id,
            "role_name": role_name,
            "folders": updated_folders,
        }

    async def list_roles(self, company_id: str, admin_id: str) -> list[dict]:
        """List all roles for a given company."""
        cursor = self.roles.find({"company_id": company_id, "added_by_admin_id": admin_id})
        roles = await cursor.to_list(length=None)
        return [
            {
                "name": r.get("name"),
                "folders": r.get("folders", []),
                "user_count": r.get("assigned_user_count", 0),
                "document_count": r.get("document_count", 0)
            }
            for r in roles
        ]

    async def delete_roles(self, company_id: str, role_names: List[str], admin_id: str) -> dict:
        """Delete one or multiple roles by name, remove them from users' assigned_roles, and delete related documents/folders."""

        deleted_roles = []
        total_users_updated = 0
        total_documents_deleted = 0

        for role_name in role_names:
            # --- Verify role exists ---
            role = await self.roles.find_one({"company_id": company_id, "name": role_name})
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

    async def upload_document_for_role(
        self,
        company_id: str,
        admin_id: str,
        role_name: str,
        folder_name: str,
        file: UploadFile
    ) -> dict:
        """
        Upload a document under a specific role and folder for the given company admin.

        - Ensures files are stored under: /uploads/documents/roleBased/{company_id}/{admin_id}/{role}/{folder}/
        - Prevents duplicate uploads (same file name under same role/folder).
        """
        # ✅ Validate and sanitize inputs
        file_name = (file.filename or "").strip()
        safe_folder = folder_name.strip("/ ")

        if not file_name:
            raise HTTPException(status_code=400, detail="Missing file name")

        # ✅ Build target directory
        base_path = os.path.join(
            UPLOAD_ROOT, "roleBased", company_id, admin_id, role_name, safe_folder
        )
        os.makedirs(base_path, exist_ok=True)

        file_path = os.path.join(base_path, file_name)

        # ✅ Check for duplicate before uploading
        existing_doc = await self.documents.find_one({
            "company_id": company_id,
            "user_id": admin_id,
            "upload_type": role_name,
            "path": file_path
        })

        if existing_doc:
            raise HTTPException(
                status_code=409,
                detail=f"A document named '{file_name}' already exists in folder '{safe_folder}' for role '{role_name}'."
            )

        # ✅ Save file asynchronously
        try:
            async with aiofiles.open(file_path, "wb") as f:
                content = await file.read()
                await f.write(content)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"File save failed: {str(e)}")

        # ✅ Prepare metadata for DB
        doc_record = {
            "company_id": company_id,
            "user_id": admin_id,
            "file_name": file_name,
            "upload_type": role_name,
            "path": file_path,
            "uploaded_at": datetime.utcnow(),
        }

        # ✅ Insert metadata
        await self.documents.insert_one(doc_record)

        # ✅ Increment document count for this role
        await self.roles.update_one(
            {"company_id": company_id, "name": role_name},
            {"$inc": {"document_count": 1}}
        )

        return {
            "success": True,
            "status": "uploaded",
            "role": role_name,
            "folder": safe_folder,
            "file_name": file_name,
            "path": file_path,
        }

    # ---------------- Shared ---------------- #
    async def get_user_with_documents(self, email: str):
        # 1️⃣ Find the user (from either collection)
        user = await self.admins.find_one({"email": email})
        user_type = "admin"
        if not user:
            user = await self.users.find_one({"email": email})
            user_type = "company_user"

        if not user:
            return None

        user_id = user["user_id"]
        company_id = user["company_id"]

        # 2️⃣ Base query — all documents directly owned by the user
        query = {"user_id": user_id}
        owned_docs_cursor = self.documents.find(query)
        owned_docs = [d async for d in owned_docs_cursor]

        # 3️⃣ If company user → also include role-based documents from the admin
        role_based_docs = []
        if user_type == "company_user":
            assigned_roles = user.get("assigned_roles", [])
            added_by_admin_id = user.get("added_by_admin_id")

            if assigned_roles and added_by_admin_id:
                # Fetch documents uploaded by that admin and matching role types
                role_query = {
                    "user_id": added_by_admin_id,
                    "company_id": company_id,
                    "upload_type": {"$in": assigned_roles},
                }
                role_cursor = self.documents.find(role_query)
                role_based_docs = [d async for d in role_cursor]

        # 4️⃣ Merge and format results
        all_docs = owned_docs + role_based_docs
        formatted_docs = [
            {
                "file_name": doc["file_name"],
                "upload_type": doc.get("upload_type", "document"),
                "path": doc.get("path", ""),
            }
            for doc in all_docs
        ]

        # 5️⃣ Generate pass_ids
        pass_ids = []
        for doc in formatted_docs:
            fn = doc["file_name"]
            upload_type = doc.get("upload_type", "document")

            # If upload_type is "document" → private
            # Otherwise → role-based (shared)
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

    async def add_user_by_admin(self, company_id: str, added_by_admin_id: str, email: str, company_role: str, assigned_role: str):
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
            "assigned_roles": [assigned_role],
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
            "assigned_roles": user_doc["assigned_roles"],
            "name": None,
            "documents": [],
        }

    async def update_user(self, company_id: str, user_id: str, name: str, email: str, assigned_roles: list[str]) -> bool:
        """
        Update user info and adjust assigned_user_count for affected roles.
        If roles were added or removed, increment/decrement counts accordingly.
        """
        now = datetime.utcnow()

        # Fetch the existing user
        user_doc = await self.users.find_one({"company_id": company_id, "user_id": user_id})
        if not user_doc:
            raise HTTPException(status_code=404, detail="User not found")

        old_roles = set(user_doc.get("assigned_roles", []))
        new_roles = set(assigned_roles)

        # --- Detect role changes ---
        added_roles = new_roles - old_roles
        removed_roles = old_roles - new_roles

        # --- Update user document ---
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

        # --- Update assigned_user_count for roles ---
        # Increment count for newly added roles
        if added_roles:
            await self.roles.update_many(
                {"company_id": company_id, "name": {"$in": list(added_roles)}},
                {"$inc": {"assigned_user_count": 1}},
            )

        # Decrement count for removed roles
        if removed_roles:
            await self.roles.update_many(
                {"company_id": company_id, "name": {"$in": list(removed_roles)}},
                {"$inc": {"assigned_user_count": -1}},
            )

        return True

    # ---------------- Debug / Inspection ---------------- #
    async def get_all_collections_data(self) -> dict:
        """Return all documents from companies, admins, users, and documents collections."""
        companies = await self.companies.find().to_list(None)
        admins = await self.admins.find().to_list(None)
        users = await self.users.find().to_list(None)
        documents = await self.documents.find().to_list(None)
        roles = await self.roles.find().to_list(None)

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
            "roles": serialize(roles)
        }

    async def clear_all_data(self) -> dict:
        """⚠️ DANGER: Delete all data in all collections."""
        await self.companies.delete_many({})
        await self.admins.delete_many({})
        await self.users.delete_many({})
        await self.documents.delete_many({})
        return {"status": "All collections cleared"}
