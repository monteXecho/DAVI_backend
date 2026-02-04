# Repository Architecture Explanation

## Current Structure

### 1. **company_repo.py** (Facade Pattern)
This is the **main entry point** that all API endpoints use. It:
- Provides a **unified interface** to all repository operations
- **Composes** domain-specific repositories (user_repo, role_repo, etc.)
- **Delegates** to domain repos for migrated methods
- **Falls back** to legacy for unmigrated methods via `__getattr__`

**Why we need it:**
- Single point of access for API endpoints
- Maintains backward compatibility
- Allows gradual migration without breaking changes

### 2. **company_repo_legacy.py** (Temporary Fallback)
This is the **old monolithic file** that:
- Contains all original code (3817 lines)
- Is used as fallback for methods not yet migrated
- Will be **removed** once all methods are migrated

**Why we still have it:**
- Some methods haven't been migrated yet
- Provides fallback via `__getattr__` in company_repo.py
- Ensures nothing breaks during migration

### 3. **Domain Repositories** (user_repo.py, role_repo.py, etc.)
These contain the **actual implementation** for specific domains:
- `user_repo.py` - User management
- `role_repo.py` - Role management
- `folder_repo.py` - Folder operations
- `admin_repo.py` - Admin management
- etc.

**Why we have them:**
- Modular, maintainable code
- Single Responsibility Principle
- Easier to test and extend

## Migration Status

### ✅ Fully Migrated (Using Domain Repos)
- User operations (add, delete, update, query)
- Admin operations (add, delete, update, query)
- Role operations (add, delete, list, assign)
- Folder operations (add, delete, upload)
- Guest access operations
- Nextcloud sync operations
- Limits and modules

### ⚠️ Still Using Legacy (via __getattr__)
- Some edge case methods
- Debug methods (get_all_collections_data, clear_all_data)
- Any method not explicitly defined in company_repo.py

## The Duplication Question

**You're right - there IS duplication, but it's intentional:**

1. **company_repo.py** has methods that **delegate** to domain repos:
   ```python
   async def add_user(...):
       return await self._user_repo.add_user(...)
   ```

2. **company_repo_legacy.py** has the **old implementation**:
   ```python
   async def add_user(...):
       # Old monolithic code
   ```

3. **user_repo.py** has the **new modular implementation**:
   ```python
   async def add_user(...):
       # New clean code
   ```

**Why this duplication exists:**
- Gradual migration strategy
- Zero downtime refactoring
- Easy rollback if needed

## Future Plan

### Phase 1: Complete Migration ✅ (Mostly Done)
- [x] Migrate all critical methods to domain repos
- [x] Update company_repo.py to use domain repos
- [x] Test all functionality

### Phase 2: Remove Legacy (Next Step)
Once we verify everything works:
1. Remove `__getattr__` fallback
2. Remove `company_repo_legacy.py` file
3. Keep it in git history for reference

### Phase 3: Optional Cleanup
- API endpoints could use domain repos directly
- But keeping facade is fine for consistency

## Recommendation

**We can remove `company_repo_legacy.py` now if:**
1. All methods are explicitly defined in `company_repo.py`
2. No methods are using `__getattr__` fallback
3. All tests pass

**Or keep it temporarily:**
- As a safety net
- Until we're 100% confident everything works
- Then remove it in a separate commit

## Summary

- **company_repo.py** = Public API (keep forever)
- **company_repo_legacy.py** = Old code (remove after migration)
- **Domain repos** = New clean code (keep forever)

The duplication is temporary and intentional for safe migration.

