"""
Storage provider interface and factory.

Defines the abstract interface that all storage providers must implement.
DAVI uses this abstraction to support multiple storage backends while
maintaining DAVI as the logical source of truth.
"""

from abc import ABC, abstractmethod
from typing import List, Optional, BinaryIO
from app.core.config import NEXTCLOUD_URL, NEXTCLOUD_ROOT_PATH


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
    async def delete_folder(self, path: str) -> bool:
        """
        Delete a folder from storage.
        
        Args:
            path: Full path to the folder (relative to storage root)
            
        Returns:
            True if folder was deleted, False if it didn't exist
            
        Raises:
            StorageError: If deletion fails
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


async def exchange_token_for_nextcloud(
    davi_token: str,
    nextcloud_client_id: str,
    nextcloud_client_secret: str,
    keycloak_host: str,
    realm: str
) -> Optional[str]:
    """
    Exchange a token from DAVI client for a token from Nextcloud client using Keycloak token exchange.
    
    Args:
        davi_token: Token from DAVI_frontend_demo client
        nextcloud_client_id: Nextcloud client ID (nextcloud_dev)
        nextcloud_client_secret: Nextcloud client secret
        keycloak_host: Keycloak server URL
        realm: Keycloak realm name
        
    Returns:
        Exchanged token from nextcloud_dev client, or None if exchange fails
    """
    token_exchange_url = f"{keycloak_host}/realms/{realm}/protocol/openid-connect/token"
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                token_exchange_url,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                    "client_id": nextcloud_client_id,
                    "client_secret": nextcloud_client_secret,
                    "subject_token": davi_token,
                    "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                    "requested_token_type": "urn:ietf:params:oauth:token-type:access_token"
                }
            )
            
            if response.status_code == 200:
                data = response.json()
                exchanged_token = data.get("access_token")
                if exchanged_token:
                    logger.info("Successfully exchanged DAVI token for Nextcloud token")
                    return exchanged_token
                else:
                    logger.warning("Token exchange succeeded but no access_token in response")
            else:
                logger.warning(
                    f"Token exchange failed: {response.status_code} - {response.text[:200]}. "
                    f"Will try using original token."
                )
    except Exception as e:
        logger.warning(f"Token exchange error: {e}. Will try using original token.")
    
    return None


def get_storage_provider(
    username: Optional[str] = None,
    access_token: Optional[str] = None,
    url: Optional[str] = None,
    root_path: Optional[str] = None,
    user_id_from_token: Optional[str] = None
) -> StorageProvider:
    """
    Factory function to get the configured storage provider with Keycloak SSO.
    
    Args:
        username: Nextcloud username (email from Keycloak). If not provided, will raise error.
        access_token: Keycloak access token for OIDC authentication. Required.
        url: Nextcloud server URL. If not provided, uses NEXTCLOUD_URL from config.
        root_path: Root path in Nextcloud. If not provided, uses NEXTCLOUD_ROOT_PATH from config.
        user_id_from_token: Optional user ID from Keycloak token (sub claim or preferred_username).
                          Nextcloud may use this instead of email as the user identifier.
                          This is critical because Nextcloud's WebDAV path must match the authenticated user's ID.
                          If not provided, username (email) will be used.
    
    Returns:
        Configured StorageProvider instance
    
    Raises:
        StorageError: If required parameters are missing
    """
    # Import here to avoid circular import
    from app.storage.nextcloud_provider import NextcloudStorageProvider
    
    provider_url = url or NEXTCLOUD_URL
    provider_username = username
    provider_access_token = access_token
    provider_root_path = root_path or NEXTCLOUD_ROOT_PATH
    
    if not provider_url:
        raise StorageError(
            "Nextcloud storage is not configured. "
            "Please set NEXTCLOUD_URL environment variable."
        )
    
    if not provider_username or not provider_access_token:
        raise StorageError(
            "Nextcloud authentication requires username and access_token. "
            "Both must be provided when calling get_storage_provider()."
        )
    
    return NextcloudStorageProvider(
        url=provider_url,
        username=provider_username,
        access_token=provider_access_token,
        root_path=provider_root_path,
        user_id_from_token=user_id_from_token
    )
