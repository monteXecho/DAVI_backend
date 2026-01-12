"""
Storage provider interface and factory.

Defines the abstract interface that all storage providers must implement.
DAVI uses this abstraction to support multiple storage backends while
maintaining DAVI as the logical source of truth.
"""

from abc import ABC, abstractmethod
from typing import List, Optional, BinaryIO
from app.core.config import NEXTCLOUD_URL, NEXTCLOUD_USERNAME, NEXTCLOUD_PASSWORD, NEXTCLOUD_ROOT_PATH


class StorageProvider(ABC):
    """
    Abstract base class for storage providers.
    
    All storage operations go through this interface, ensuring DAVI
    maintains control over folder structure and permissions regardless
    of the underlying storage backend.
    """
    
    @abstractmethod
    async def create_folder(self, path: str) -> bool:
        """
        Create a folder at the specified path.
        
        Args:
            path: Full path to the folder (relative to storage root)
            
        Returns:
            True if folder was created, False if it already exists
            
        Raises:
            StorageError: If folder creation fails
        """
        pass
    
    @abstractmethod
    async def folder_exists(self, path: str) -> bool:
        """
        Check if a folder exists at the specified path.
        
        Args:
            path: Full path to the folder (relative to storage root)
            
        Returns:
            True if folder exists, False otherwise
        """
        pass
    
    @abstractmethod
    async def upload_file(
        self,
        file_path: str,
        content: BinaryIO,
        content_length: Optional[int] = None
    ) -> str:
        """
        Upload a file to storage.
        
        Args:
            file_path: Full path where the file should be stored (relative to storage root)
            content: File-like object containing the file content
            content_length: Optional file size in bytes
            
        Returns:
            Storage path of the uploaded file (canonical path)
            
        Raises:
            StorageError: If upload fails
        """
        pass
    
    @abstractmethod
    async def download_file(self, path: str) -> BinaryIO:
        """
        Download a file from storage.
        
        Args:
            path: Full path to the file (relative to storage root)
            
        Returns:
            File-like object containing the file content
            
        Raises:
            StorageError: If file doesn't exist or download fails
        """
        pass
    
    @abstractmethod
    async def delete_file(self, path: str) -> bool:
        """
        Delete a file from storage.
        
        Args:
            path: Full path to the file (relative to storage root)
            
        Returns:
            True if file was deleted, False if it didn't exist
            
        Raises:
            StorageError: If deletion fails
        """
        pass
    
    @abstractmethod
    async def list_folders(
        self,
        path: str,
        recursive: bool = False
    ) -> List[dict]:
        """
        List folders in a directory.
        
        Args:
            path: Path to list (relative to storage root)
            recursive: If True, recursively list all subfolders
            
        Returns:
            List of folder dictionaries with keys:
            - path: Full path to the folder
            - name: Folder name
            - depth: Depth level (0 = root level)
            
        Raises:
            StorageError: If listing fails
        """
        pass
    
    @abstractmethod
    async def file_exists(self, path: str) -> bool:
        """
        Check if a file exists at the specified path.
        
        Args:
            path: Full path to the file (relative to storage root)
            
        Returns:
            True if file exists, False otherwise
        """
        pass
    
    @abstractmethod
    def get_canonical_path(self, path: str) -> str:
        """
        Get the canonical storage path for a given logical path.
        
        This ensures consistent path representation regardless of
        how the path was originally specified.
        
        Args:
            path: Logical path (may be relative or absolute)
            
        Returns:
            Canonical storage path (relative to storage root)
        """
        pass


class StorageError(Exception):
    """Base exception for storage operations."""
    pass


def get_storage_provider() -> StorageProvider:
    """
    Factory function to get the configured storage provider.
    
    Currently returns NextcloudStorageProvider. In the future,
    this could be extended to support multiple providers based on
    configuration.
    
    Returns:
        Configured StorageProvider instance
    """
    # Import here to avoid circular import
    from app.storage.nextcloud_provider import NextcloudStorageProvider
    
    # For now, always use Nextcloud
    # In the future, this could check config to select provider
    if not NEXTCLOUD_URL or not NEXTCLOUD_USERNAME or not NEXTCLOUD_PASSWORD:
        raise StorageError(
            "Nextcloud storage is not configured. "
            "Please set NEXTCLOUD_URL, NEXTCLOUD_USERNAME, and NEXTCLOUD_PASSWORD environment variables."
        )
    
    return NextcloudStorageProvider(
        url=NEXTCLOUD_URL,
        username=NEXTCLOUD_USERNAME,
        password=NEXTCLOUD_PASSWORD,
        root_path=NEXTCLOUD_ROOT_PATH
    )
