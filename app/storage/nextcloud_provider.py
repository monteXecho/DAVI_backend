"""
Nextcloud storage provider implementation using WebDAV.

This provider implements the StorageProvider interface for Nextcloud
using WebDAV protocol. All file operations go through Nextcloud,
but DAVI maintains the logical folder structure and permissions.
"""

import os
import logging
from typing import List, Optional, BinaryIO
from urllib.parse import urljoin, quote, unquote
import httpx
from app.storage.providers import StorageProvider, StorageError

logger = logging.getLogger(__name__)


class NextcloudStorageProvider(StorageProvider):
    """
    Nextcloud storage provider using WebDAV.
    
    This implementation:
    - Uses WebDAV protocol to interact with Nextcloud
    - Maintains canonical paths relative to configured root
    - Handles authentication automatically
    - Provides error handling and logging
    """
    
    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        root_path: str = "/DAVI"
    ):
        """
        Initialize Nextcloud storage provider.
        
        Args:
            url: Nextcloud server URL (e.g., "https://nextcloud.example.com")
            username: Nextcloud username
            password: Nextcloud password or app password
            root_path: Root path in Nextcloud where DAVI files are stored
        """
        self.base_url = url.rstrip("/")
        self.username = username
        self.password = password
        self.root_path = root_path.strip("/")
        
        # WebDAV endpoint is typically at /remote.php/dav/files/{username}/
        self.webdav_base = f"{self.base_url}/remote.php/dav/files/{quote(self.username)}"
        
        # Full path including root
        self.storage_root = f"{self.webdav_base}/{self.root_path}".rstrip("/")
        
        logger.info(f"NextcloudStorageProvider initialized: {self.base_url}, root={self.root_path}")
    
    def _get_full_path(self, path: str) -> str:
        """
        Convert a logical path to full WebDAV path.
        
        Args:
            path: Logical path (relative to storage root) - can be decoded or encoded
            
        Returns:
            Full WebDAV URL path (for use in HTTP requests)
        """
        # Normalize path: remove leading/trailing slashes, handle relative paths
        normalized = path.strip("/")
        if not normalized:
            return self.storage_root
        
        # Encode each path segment to handle special characters
        # Split by /, encode each segment, then rejoin
        path_segments = normalized.split("/")
        encoded_segments = [quote(segment, safe='') for segment in path_segments]
        encoded_path = "/".join(encoded_segments)
        
        # Join with storage root
        full_path = f"{self.storage_root}/{encoded_path}"
        # Ensure path ends with / for folders in WebDAV
        return full_path
    
    def get_canonical_path(self, path: str) -> str:
        """
        Get canonical storage path.
        
        Args:
            path: Logical path (may be relative or absolute)
            
        Returns:
            Canonical path relative to storage root
        """
        # Remove leading/trailing slashes
        normalized = path.strip("/")
        
        # Remove storage root prefix if present
        if normalized.startswith(self.root_path):
            normalized = normalized[len(self.root_path):].strip("/")
        
        return normalized
    
    async def _make_request(
        self,
        method: str,
        path: str,
        content: Optional[bytes] = None,
        headers: Optional[dict] = None,
        timeout: float = 30.0
    ) -> httpx.Response:
        """
        Make a WebDAV request to Nextcloud.
        
        Args:
            method: HTTP method (GET, PUT, DELETE, MKCOL, PROPFIND)
            path: Full WebDAV path
            content: Optional request body
            headers: Optional additional headers
            timeout: Request timeout in seconds
            
        Returns:
            httpx.Response object
            
        Raises:
            StorageError: If request fails
        """
        # If path is already a full URL, use it; otherwise construct from base_url
        if path.startswith("http"):
            url = path
        elif path.startswith("/"):
            url = f"{self.base_url}{path}"
        else:
            # Relative path - should not happen but handle gracefully
            url = f"{self.base_url}/{path}"
        
        auth = (self.username, self.password)
        request_headers = {
            "Content-Type": "application/octet-stream",
            **(headers or {})
        }
        
        try:
            logger.info(f"Making {method} request to: {url}")
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(
                    method=method,
                    url=url,
                    content=content,
                    headers=request_headers,
                    auth=auth
                )
                
                # Log non-2xx responses for debugging
                if not (200 <= response.status_code < 300):
                    logger.warning(
                        f"Nextcloud {method} {path}: {response.status_code} - {response.text[:200]}"
                    )
                
                return response
        except httpx.TimeoutException as e:
            raise StorageError(f"Nextcloud request timeout: {e}") from e
        except httpx.RequestError as e:
            raise StorageError(f"Nextcloud request failed: {e}") from e
    
    async def create_folder(self, path: str) -> bool:
        """
        Create a folder in Nextcloud using WebDAV MKCOL.
        
        Args:
            path: Logical path to the folder (relative to storage root)
            
        Returns:
            True if folder was created, False if it already exists
        """
        full_path = self._get_full_path(path)
        
        # MKCOL creates a collection (folder) in WebDAV
        response = await self._make_request("MKCOL", full_path)
        
        if response.status_code == 201:
            logger.info(f"Created Nextcloud folder: {path}")
            return True
        elif response.status_code == 405:
            # 405 Method Not Allowed usually means folder already exists
            logger.debug(f"Folder already exists: {path}")
            return False
        elif response.status_code == 409:
            # 409 Conflict - parent directory doesn't exist
            # Create parent directories first
            parent = os.path.dirname(path).strip("/")
            if parent:
                await self.create_folder(parent)
                # Retry creating the folder
                response = await self._make_request("MKCOL", full_path)
                if response.status_code == 201:
                    logger.info(f"Created Nextcloud folder (after creating parent): {path}")
                    return True
                elif response.status_code == 405:
                    return False
            
            raise StorageError(f"Failed to create folder {path}: {response.status_code} {response.text}")
        else:
            raise StorageError(
                f"Failed to create folder {path}: {response.status_code} {response.text}"
            )
    
    async def folder_exists(self, path: str) -> bool:
        """Check if a folder exists in Nextcloud."""
        full_path = self._get_full_path(path)
        response = await self._make_request("PROPFIND", full_path, headers={"Depth": "0"})
        return response.status_code == 207  # 207 Multi-Status means resource exists
    
    async def upload_file(
        self,
        file_path: str,
        content,
        content_length: Optional[int] = None
    ) -> str:
        """
        Upload a file to Nextcloud using WebDAV PUT.
        
        Args:
            file_path: Logical path where file should be stored
            content: File-like object (BinaryIO) or bytes with file content
            content_length: Optional file size
            
        Returns:
            Canonical storage path of the uploaded file
        """
        full_path = self._get_full_path(file_path)
        
        # Read file content - handle both file-like objects and bytes
        if isinstance(content, bytes):
            file_data = content
        elif hasattr(content, 'read'):
            # Try to read from file-like object
            try:
                # Try async read first (for aiofiles)
                try:
                    file_data = await content.read()
                except TypeError:
                    # If that fails, try sync read (for BytesIO, regular files)
                    file_data = content.read()
            except Exception as e:
                logger.error(f"Error reading file content: {e}")
                raise StorageError(f"Failed to read file content: {e}")
        else:
            file_data = content
        
        # Ensure parent directory exists
        parent_dir = os.path.dirname(file_path).strip("/")
        if parent_dir:
            await self.create_folder(parent_dir)
        
        # Upload file
        headers = {}
        if content_length:
            headers["Content-Length"] = str(content_length)
        elif isinstance(file_data, bytes):
            headers["Content-Length"] = str(len(file_data))
        
        response = await self._make_request("PUT", full_path, content=file_data, headers=headers)
        
        if response.status_code in (201, 204):
            logger.info(f"Uploaded file to Nextcloud: {file_path}")
            return self.get_canonical_path(file_path)
        else:
            raise StorageError(
                f"Failed to upload file {file_path}: {response.status_code} {response.text}"
            )
    
    async def download_file(self, path: str) -> BinaryIO:
        """
        Download a file from Nextcloud.
        
        Args:
            path: Logical path to the file
            
        Returns:
            BytesIO object with file content
        """
        from io import BytesIO
        
        full_path = self._get_full_path(path)
        response = await self._make_request("GET", full_path)
        
        if response.status_code == 200:
            return BytesIO(response.content)
        elif response.status_code == 404:
            raise StorageError(f"File not found: {path}")
        else:
            raise StorageError(
                f"Failed to download file {path}: {response.status_code} {response.text}"
            )
    
    async def delete_file(self, path: str) -> bool:
        """Delete a file from Nextcloud."""
        full_path = self._get_full_path(path)
        response = await self._make_request("DELETE", full_path)
        
        if response.status_code in (204, 404):
            return response.status_code == 204
        else:
            raise StorageError(
                f"Failed to delete file {path}: {response.status_code} {response.text}"
            )
    
    async def list_folders(
        self,
        path: str,
        recursive: bool = False
    ) -> List[dict]:
        """
        List folders in Nextcloud using WebDAV PROPFIND.
        
        Args:
            path: Path to list (relative to storage root)
            recursive: If True, recursively list all subfolders
            
        Returns:
            List of folder dictionaries with path, name, and depth
        """
        full_path = self._get_full_path(path)
        depth = "infinity" if recursive else "1"
        
        # PROPFIND request body
        propfind_body = """<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
    <d:resourcetype/>
  </d:prop>
</d:propfind>"""
        
        response = await self._make_request(
            "PROPFIND",
            full_path,
            content=propfind_body.encode(),
            headers={"Depth": depth, "Content-Type": "application/xml"}
        )
        
        # Handle 404 - path doesn't exist, return empty list
        if response.status_code == 404:
            logger.info(f"Path {path} does not exist in Nextcloud, returning empty list")
            return []
        
        if response.status_code != 207:
            raise StorageError(
                f"Failed to list folders {path}: {response.status_code} {response.text}"
            )
        
        # Parse XML response to extract folder paths
        # For simplicity, we'll extract hrefs from the response
        import xml.etree.ElementTree as ET
        
        try:
            root = ET.fromstring(response.text)
            namespaces = {"d": "DAV:"}
            folders = []
            
            base_depth = len(path.strip("/").split("/")) if path.strip("/") else 0
            
            for response_elem in root.findall(".//d:response", namespaces):
                href_elem = response_elem.find("d:href", namespaces)
                if href_elem is None:
                    continue
                
                href = href_elem.text
                if not href:
                    continue
                
                # Extract path relative to storage root
                # href format: /remote.php/dav/files/username/DAVI/path/to/folder/
                if "/remote.php/dav/files/" in href:
                    parts = href.split("/remote.php/dav/files/")[1].split("/")
                    if len(parts) > 1:
                        # Skip username, get path after root
                        storage_path = "/".join(parts[1:]).rstrip("/")
                        if storage_path.startswith(self.root_path):
                            relative_path = storage_path[len(self.root_path):].strip("/")
                        else:
                            relative_path = storage_path.strip("/")
                        
                        # Decode URL-encoded characters (e.g., %20 -> space)
                        relative_path = unquote(relative_path)
                        
                        # Check if it's a collection (folder)
                        resourcetype = response_elem.find("d:propstat/d:prop/d:resourcetype", namespaces)
                        if resourcetype is not None:
                            collection = resourcetype.find("d:collection", namespaces)
                            if collection is not None and relative_path:
                                # Calculate depth
                                depth_level = len(relative_path.split("/")) - 1
                                folder_name = relative_path.split("/")[-1] if "/" in relative_path else relative_path
                                folder_name = unquote(folder_name)  # Decode URL-encoded characters
                                
                                folders.append({
                                    "path": relative_path,
                                    "name": folder_name,
                                    "depth": depth_level
                                })
            
            return folders
        except ET.ParseError as e:
            logger.error(f"Failed to parse PROPFIND response: {e}")
            raise StorageError(f"Failed to parse folder list: {e}")
    
    async def file_exists(self, path: str) -> bool:
        """Check if a file exists in Nextcloud."""
        full_path = self._get_full_path(path)
        response = await self._make_request("HEAD", full_path)
        return response.status_code == 200
    
    async def list_files(
        self,
        path: str,
        recursive: bool = False
    ) -> List[dict]:
        """
        List files in Nextcloud using WebDAV PROPFIND.
        
        Args:
            path: Path to list (relative to storage root)
            recursive: If True, recursively list all files in subfolders
            
        Returns:
            List of file dictionaries with path, name, and size
        """
        full_path = self._get_full_path(path)
        depth = "infinity" if recursive else "1"
        
        # PROPFIND request body - request file properties
        propfind_body = """<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
    <d:resourcetype/>
    <d:getcontentlength/>
    <d:getlastmodified/>
  </d:prop>
</d:propfind>"""
        
        response = await self._make_request(
            "PROPFIND",
            full_path,
            content=propfind_body.encode(),
            headers={"Depth": depth, "Content-Type": "application/xml"}
        )
        
        # Handle 404 - path doesn't exist, return empty list
        if response.status_code == 404:
            logger.info(f"Path {path} does not exist in Nextcloud, returning empty list")
            return []
        
        if response.status_code != 207:
            raise StorageError(
                f"Failed to list files {path}: {response.status_code} {response.text}"
            )
        
        # Parse XML response to extract file paths
        import xml.etree.ElementTree as ET
        
        try:
            root = ET.fromstring(response.text)
            namespaces = {"d": "DAV:"}
            files = []
            
            for response_elem in root.findall(".//d:response", namespaces):
                href_elem = response_elem.find("d:href", namespaces)
                if href_elem is None:
                    continue
                
                href = href_elem.text
                if not href:
                    continue
                
                # Extract path relative to storage root
                if "/remote.php/dav/files/" in href:
                    parts = href.split("/remote.php/dav/files/")[1].split("/")
                    if len(parts) > 1:
                        storage_path = "/".join(parts[1:]).rstrip("/")
                        if storage_path.startswith(self.root_path):
                            relative_path = storage_path[len(self.root_path):].strip("/")
                        else:
                            relative_path = storage_path.strip("/")
                        
                        # Skip if this is the folder itself (ends with /)
                        if not relative_path or relative_path.endswith("/"):
                            continue
                        
                        # Decode URL-encoded characters (e.g., %20 -> space)
                        relative_path = unquote(relative_path)
                        
                        # Check if it's a file (not a collection/folder)
                        resourcetype = response_elem.find("d:propstat/d:prop/d:resourcetype", namespaces)
                        if resourcetype is not None:
                            collection = resourcetype.find("d:collection", namespaces)
                            if collection is None:  # It's a file, not a folder
                                # Get file size
                                size_elem = response_elem.find("d:propstat/d:prop/d:getcontentlength", namespaces)
                                file_size = int(size_elem.text) if size_elem is not None and size_elem.text else 0
                                
                                # Get last modified
                                modified_elem = response_elem.find("d:propstat/d:prop/d:getlastmodified", namespaces)
                                last_modified = modified_elem.text if modified_elem is not None else None
                                
                                # Extract file name and decode it
                                file_name = relative_path.split("/")[-1] if "/" in relative_path else relative_path
                                file_name = unquote(file_name)  # Decode URL-encoded characters
                                
                                files.append({
                                    "path": relative_path,
                                    "name": file_name,
                                    "size": file_size,
                                    "last_modified": last_modified
                                })
            
            return files
        except ET.ParseError as e:
            logger.error(f"Failed to parse PROPFIND response: {e}")
            raise StorageError(f"Failed to parse file list: {e}")
