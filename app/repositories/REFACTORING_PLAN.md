# Repository Refactoring Plan

## Current State
- `company_repo_legacy.py`: 3816 lines, 61 methods - Monolithic repository
- `company_repo.py`: Facade pattern, delegates to legacy for unmigrated methods

## Target Architecture

### Domain Repositories (Single Responsibility)
1. **admin_repo.py** ✅ - Admin CRUD, module assignment
2. **user_repo.py** - User CRUD, bulk operations, teamlid management
3. **role_repo.py** - Role CRUD, assignment, user count management
4. **folder_repo.py** - Folder CRUD, Nextcloud sync
5. **guest_access_repo.py** - Guest access management, teamlid permissions
6. **nextcloud_sync_repo.py** - Nextcloud document synchronization
7. **document_operations_repo.py** - Document upload/delete operations

### Existing Repositories (Keep)
- `base_repo.py` - Base class with collections
- `limits_repo.py` - Resource limits
- `modules_repo.py` - Module permissions
- `company_core_repo.py` - Company CRUD
- `document_repo.py` - Document metadata

### Migration Strategy
1. Create domain repositories with all methods from legacy
2. Update `company_repo.py` to compose all repositories
3. Keep `company_repo_legacy.py` as fallback during transition
4. Gradually remove legacy dependency once all methods are migrated

## Method Distribution

### AdminRepository
- create_admin
- get_admin_by_id
- get_admins_by_company
- find_admin_by_email
- update_admin
- assign_modules
- delete_admin

### UserRepository
- add_user
- add_user_by_admin
- add_users_from_email_file
- delete_users
- delete_users_by_admin
- update_user
- find_user_by_email
- get_users_by_company
- get_users_by_company_admin
- get_all_users_created_by_admin_id
- assign_teamlid_permissions
- remove_teamlid_role

### RoleRepository
- add_or_update_role
- list_roles
- delete_roles
- delete_roles_by_admin
- assign_role_to_user
- get_role_by_name
- _get_roles_to_assign
- _update_role_user_counts
- _decrease_role_user_counts

### FolderRepository
- add_folders
- get_folders
- delete_folders

### GuestAccessRepository
- upsert_guest_access
- get_guest_access
- list_guest_workspaces_for_user
- disable_guest_access

### NextcloudSyncRepository
- sync_documents_from_nextcloud

### DocumentOperationsRepository
- upload_document_for_folder
- delete_private_documents
- delete_documents
- get_admin_documents
- get_all_private_documents

