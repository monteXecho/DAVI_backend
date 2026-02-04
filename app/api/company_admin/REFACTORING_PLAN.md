# Company Admin API Refactoring Plan

## Current State
- `company_admin.py` - 2234 lines (monolithic file)
- `company_admin_legacy.py` - 2098 lines (backup/old version)
- `company_admin/` directory exists with partial structure

## Target Structure

### Domain Routers
1. **users.py** - User management endpoints
2. **roles.py** - Role management endpoints  
3. **folders.py** - Folder management endpoints
4. **documents.py** - Document management endpoints
5. **guest_access.py** - Guest access/teamlid endpoints
6. **stats.py** - Statistics endpoints
7. **debug.py** - Debug/inspection endpoints

### Shared Module
- **shared.py** - Common dependencies and utilities (already exists)

### Main Router
- **main.py** - Composes all domain routers

## Endpoint Distribution

### users.py
- GET /users
- POST /users
- PUT /users/{user_id}
- DELETE /users
- POST /users/teamlid
- POST /users/upload
- POST /users/reset-password
- DELETE /users/teamlid/{target_admin_id}
- POST /users/role/delete
- POST /users/role/add
- GET /user

### roles.py
- POST /roles
- GET /roles
- POST /roles/delete
- POST /roles/assign
- POST /roles/upload/{folder_name}

### folders.py
- POST /folders
- GET /folders
- POST /folders/delete
- GET /folders/import/list
- POST /folders/import
- POST /folders/sync

### documents.py
- GET /documents
- GET /documents/private
- GET /documents/all
- GET /documents/download
- POST /documents/delete
- POST /documents/delete/private

### guest_access.py
- POST /guest-access
- GET /guest-workspaces

### stats.py
- GET /stats

### debug.py
- GET /debug/all-data
- DELETE /debug/clear-all

## Migration Strategy

1. Create domain router files
2. Move endpoints from `company_admin.py` to domain routers
3. Update `main.py` to include domain routers
4. Test all endpoints
5. Remove `company_admin.py` and `company_admin_legacy.py`

