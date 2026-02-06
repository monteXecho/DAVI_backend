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
        access_token: str,
        root_path: str = "/DAVI",
        user_id_from_token: Optional[str] = None
    ):
        """
        Initialize Nextcloud storage provider with Keycloak SSO.
        
        Args:
            url: Nextcloud server URL (e.g., "https://nextcloud.example.com")
            username: Nextcloud username (email from Keycloak) - used as fallback
            access_token: Keycloak access token for OIDC authentication
            root_path: Root path in Nextcloud where DAVI files are stored
            user_id_from_token: Optional user ID from Keycloak token (sub or preferred_username).
                              Nextcloud may use this instead of email as the user identifier.
                              If not provided, username (email) will be used.
        """
        self.base_url = url.rstrip("/")
        self.username = username
        self.access_token = access_token
        self.root_path = root_path.strip("/")
        
        # CRITICAL: Nextcloud WebDAV path must match the authenticated user's ID
        # Nextcloud may use:
        # 1. Keycloak's 'sub' claim (UUID) if "Use unique user ID" is enabled
        # 2. Keycloak's 'preferred_username' if configured
        # 3. Email address if that's what Nextcloud is configured to use
        # CRITICAL: Nextcloud is configured with mappingUid=email
        # We MUST use email as the user ID, not preferred_username or sub
        # username parameter is the email address
        # user_id_from_token might be preferred_username or sub, but we ignore it if email is available
        
        # Log what we're using for debugging - decode token to see what's available
        import jwt
        email_from_token = None
        preferred_username = None
        sub = None
        try:
            decoded = jwt.decode(access_token, options={"verify_signature": False})
            preferred_username = decoded.get('preferred_username')
            sub = decoded.get('sub')
            email_from_token = decoded.get('email')
            azp = decoded.get('azp')
            
            logger.info(
                f"Token claims - email: {email_from_token}, "
                f"preferred_username: {preferred_username}, "
                f"sub: {sub}, azp: {azp}, "
                f"username param (email): {username}, "
                f"user_id_from_token passed: {user_id_from_token}"
            )
        except Exception as e:
            logger.warning(f"Could not decode token for debugging: {e}")
        
        # ALWAYS prioritize email (username parameter) over user_id_from_token
        # Nextcloud expects email as the user ID (mappingUid=email)
        if username and "@" in username:
            # username is email - use it
            nextcloud_user_id = username
            logger.info(f"✅ Using email as Nextcloud user ID: {username}")
        elif email_from_token:
            # Fallback to email from token if username param is not email
            nextcloud_user_id = email_from_token
            logger.warning(f"⚠️  username param is not email, using email from token: {email_from_token}")
        elif user_id_from_token:
            # Last resort: use user_id_from_token (might be preferred_username or sub)
            nextcloud_user_id = user_id_from_token
            logger.error(
                f"❌ CRITICAL: No email available! Using user_id_from_token: {user_id_from_token}. "
                f"This may fail if Nextcloud expects email. Token has preferred_username={preferred_username}, sub={sub}"
            )
        else:
            # Absolute last resort
            nextcloud_user_id = username
            logger.error(f"❌ CRITICAL: No valid user ID found! Using username param as-is: {username}")
        
        # WebDAV endpoint is typically at /remote.php/dav/files/{user_id}/
        # The user_id must match what Nextcloud expects based on OIDC configuration
        self.webdav_base = f"{self.base_url}/remote.php/dav/files/{quote(nextcloud_user_id)}"
        self.nextcloud_user_id = nextcloud_user_id
        
        # Full path including root
        self.storage_root = f"{self.webdav_base}/{self.root_path}".rstrip("/")
        
        # Session cookie for authenticated requests (will be set after OIDC login)
        self._session_cookie = None
        
        # Validate access token is present and valid
        if not access_token:
            raise StorageError("access_token is required for Nextcloud authentication")
        if not isinstance(access_token, str) or not access_token.strip():
            raise StorageError("access_token must be a non-empty string")
        
        # Cache for the actual Nextcloud user ID (queried from Nextcloud API)
        self._actual_nextcloud_user_id = None
        
        # Cache for exchanged token (from nextcloud_dev client)
        # DAVI uses DAVI_frontend_demo client, but Nextcloud expects nextcloud_dev client
        self._exchanged_token = None
        self._original_token = access_token  # Keep original token for exchange
        
        # Log token claims for debugging
        import jwt
        try:
            decoded = jwt.decode(access_token, options={"verify_signature": False})
            preferred_username = decoded.get('preferred_username')
            sub = decoded.get('sub')
            logger.info(
                f"NextcloudStorageProvider initialized: "
                f"base_url={self.base_url}, email={self.username}, "
                f"nextcloud_user_id={self.nextcloud_user_id}, "
                f"user_id_from_token={user_id_from_token}, "
                f"token_preferred_username={preferred_username}, "
                f"token_sub={sub}, "
                f"webdav_base={self.webdav_base}, "
                f"root={self.root_path}"
            )
            
            # CRITICAL: Nextcloud is configured with mappingUid=email
            # We MUST use email, NOT preferred_username or sub
            # Verify we're using email (as required by Nextcloud mappingUid=email)
            email_from_token = decoded.get('email')
            if not self.nextcloud_user_id or "@" not in self.nextcloud_user_id:
                logger.error(
                    f"❌ CRITICAL ERROR: nextcloud_user_id is not an email! "
                    f"Value: {self.nextcloud_user_id}, Expected: email address. "
                    f"Nextcloud is configured with mappingUid=email, so we MUST use email. "
                    f"Token has email={email_from_token}, preferred_username={preferred_username}, sub={sub}"
                )
            elif self.nextcloud_user_id != email_from_token and self.nextcloud_user_id != self.username:
                logger.warning(
                    f"⚠️  WARNING: nextcloud_user_id ({self.nextcloud_user_id}) doesn't match "
                    f"email from token ({email_from_token}) or username param ({self.username}). "
                    f"This may cause authentication failures."
                )
            else:
                logger.info(
                    f"✅ Using email as Nextcloud user ID: {self.nextcloud_user_id} "
                    f"(matches token email: {email_from_token}, username param: {self.username})"
                )
        except Exception as e:
            logger.warning(f"Could not decode token for user ID fix: {e}")
            logger.info(
                f"NextcloudStorageProvider initialized: "
                f"base_url={self.base_url}, email={self.username}, "
                f"nextcloud_user_id={self.nextcloud_user_id}, "
                f"webdav_base={self.webdav_base}, root={self.root_path}"
            )
    
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
        timeout: float = 30.0,
        retry_on_503: bool = True,
        max_retries: int = 3
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
        
        # Nextcloud WebDAV authentication with Keycloak OIDC Bearer token
        # Nextcloud must have "Allow API calls and WebDAV requests with OIDC token" enabled
        # This allows each user to access only their own folders using their Keycloak token
        
        # Start with default headers
        request_headers = {
            "Content-Type": "application/octet-stream",
        }
        
        # Add Authorization header with Bearer token (CRITICAL - must be present)
        # Use exchanged token if available (from nextcloud_dev), otherwise use original token
        token_to_use = self._exchanged_token or self.access_token
        
        if not token_to_use:
            logger.error("No access token available for Nextcloud authentication!")
            raise StorageError("No access token available for Nextcloud authentication")
        
        # Ensure token is a string and not empty
        if not isinstance(token_to_use, str) or not token_to_use.strip():
            logger.error(f"Invalid access token type: {type(token_to_use)}, value: {token_to_use[:20] if token_to_use else 'None'}...")
            raise StorageError("Invalid access token format - must be a non-empty string")
        
        # Set Authorization header - CRITICAL: Must be exactly "Bearer <token>" with single space
        bearer_token = token_to_use.strip()
        request_headers["Authorization"] = f"Bearer {bearer_token}"
        logger.debug(f"Authorization header set: Bearer <token length: {len(bearer_token)}>")
        
        # Merge any additional headers (but don't let them override Authorization)
        if headers:
            for key, value in headers.items():
                if key.lower() != "authorization":  # Protect Authorization header
                    request_headers[key] = value
        
        # If we have a session cookie from previous OIDC login, we can use it as fallback
        # But Bearer token should work if Nextcloud is properly configured
        if self._session_cookie and not self.access_token:
            # Only use session cookie if we don't have a token
            request_headers["Cookie"] = self._session_cookie
            request_headers.pop("Authorization", None)
        
        # Before making WebDAV requests:
        # 1. Exchange token if needed (DAVI_frontend_demo -> nextcloud_dev)
        # 2. Skip user info endpoint check - it requires web session, but WebDAV works with Bearer token
        try:
            await self._exchange_token_if_needed()
        except Exception as e:
            logger.debug(f"Token exchange failed (non-critical): {e}")
        
        # Don't call _get_actual_nextcloud_user_id() here - it requires web session
        # WebDAV works fine with email as user ID (which we already have in self.nextcloud_user_id)
        
        try:
            # Debug: Log what we're sending (but don't log the full token)
            auth_header = request_headers.get("Authorization", "NOT SET")
            token_preview = auth_header[:50] + "..." if len(auth_header) > 50 else auth_header
            logger.info(f"Making {method} request to: {url}")
            logger.info(f"Authorization header present: {auth_header != 'NOT SET'}")
            logger.info(f"Authorization header preview: {token_preview}")
            logger.info(f"Request headers keys: {list(request_headers.keys())}")
            logger.info(f"Using WebDAV base: {self.webdav_base}")
            
            # Verify Authorization header is actually set
            if "Authorization" not in request_headers:
                logger.error("CRITICAL: Authorization header is missing from request headers!")
                raise StorageError("Authorization header is missing - cannot authenticate with Nextcloud")
            
            # Retry logic for 503 Service Unavailable (database locked) errors
            import asyncio
            last_exception = None
            
            for attempt in range(max_retries if retry_on_503 else 1):
                try:
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        response = await client.request(
                            method=method,
                            url=url,
                            content=content,
                            headers=request_headers
                        )
                        
                        # Debug: Log response headers
                        logger.debug(f"Response status: {response.status_code}")
                        logger.debug(f"Response headers: {dict(response.headers)}")
                        
                        # Handle 503 Service Unavailable (database locked) with retry
                        if response.status_code == 503:
                            error_text = response.text[:500] if response.text else ""
                            is_db_locked = "database is locked" in error_text.lower() or "dbalexception" in error_text.lower()
                            
                            # If we have retries left, retry
                            if retry_on_503 and attempt < max_retries - 1:
                                wait_time = (2 ** attempt) * 0.5  # Exponential backoff: 0.5s, 1s, 2s
                                if is_db_locked:
                                    logger.warning(
                                        f"⚠️  Nextcloud database is locked (503 Service Unavailable). "
                                        f"Retrying in {wait_time:.1f}s (attempt {attempt + 1}/{max_retries})... "
                                        f"This is a Nextcloud infrastructure issue, not a DAVI problem."
                                    )
                                else:
                                    logger.warning(
                                        f"⚠️  Nextcloud returned 503 Service Unavailable. "
                                        f"Retrying in {wait_time:.1f}s (attempt {attempt + 1}/{max_retries})..."
                                    )
                                await asyncio.sleep(wait_time)
                                continue  # Retry the request
                            else:
                                # No retries left - raise error with helpful message
                                raise StorageError(
                                    f"Nextcloud database is locked (503 Service Unavailable) after {max_retries} attempts. "
                                    f"This is a Nextcloud infrastructure issue. Please check Nextcloud logs and database. "
                                    f"Error: {error_text[:200]}"
                                )
                        
                        # If we get here, either:
                        # 1. Response is not 503, or
                        # 2. We've exhausted retries, or
                        # 3. retry_on_503 is False
                        # Continue with normal error handling
                        
                        # If 401 Unauthorized, check if Nextcloud OIDC is properly configured
                        if response.status_code == 401:
                            error_text = response.text[:500] if response.text else ""
                            # Check if error message indicates Bearer token was received but rejected
                            error_lower = error_text.lower()
                            bearer_received = "bearer token" in error_lower or "authorization: bearer" in error_lower
                            
                            if bearer_received:
                                logger.error(
                                    f"Nextcloud WebDAV authentication failed with 401. "
                                    f"⚠️  Nextcloud RECEIVED the Bearer token but REJECTED it as incorrect. "
                                    f"This usually means:\n"
                                    f"  1. Token is from wrong client (should be 'nextcloud_dev', not 'DAVI_frontend_demo')\n"
                                    f"  2. Token exchange is not working (check Keycloak token exchange configuration)\n"
                                    f"  3. Nextcloud OIDC Bearer token settings are not fully enabled\n\n"
                                    f"CRITICAL: You MUST enable ALL of these in Nextcloud → Settings → Administration → OpenID Connect:\n"
                                    f"  ✅ 'Do you want to allow API calls and WebDAV requests that are authenticated with an OIDC ID token or access token?' → YES\n"
                                    f"  ✅ 'This automatically provisions the user, when sending API and WebDAV requests with a Bearer token...' → YES\n"
                                    f"  ✅ Auto-provisioning → ENABLED\n"
                                    f"  ✅ Bearer token check → ENABLED\n\n"
                                    f"Error: {error_text[:200]}"
                                )
                            else:
                                logger.error(
                                    f"Nextcloud WebDAV authentication failed with 401. "
                                    f"CRITICAL: Ensure Nextcloud OIDC setting 'Allow API calls and WebDAV requests "
                                    f"that are authenticated with an OIDC ID token or access token' is ENABLED. "
                                    f"Also verify 'Auto provisioning and Bearer token check' is enabled. "
                                    f"Error: {error_text[:200]}"
                                )
                            
                            logger.error(
                                f"Request was sent with Authorization header: {auth_header != 'NOT SET'}. "
                                f"Header preview: {token_preview}. "
                                f"Token client (azp): {self._get_token_client()}. "
                                f"If header was sent but Nextcloud says it's missing, check Nextcloud OIDC configuration."
                            )
                            
                            # Try OIDC session as fallback (though Bearer token should work if configured)
                            if not self._session_cookie:
                                logger.info("Attempting OIDC session authentication as fallback...")
                                try:
                                    auth_success = await self._authenticate_via_oidc()
                                    if auth_success and self._session_cookie:
                                        request_headers["Cookie"] = self._session_cookie
                                        request_headers.pop("Authorization", None)
                                        logger.info(f"Retrying {method} request with OIDC session cookie...")
                                        response = await client.request(
                                            method=method,
                                            url=url,
                                            content=content,
                                            headers=request_headers
                                        )
                                        # If session cookie worked, return the response
                                        if response.status_code != 401:
                                            logger.info(f"✅ OIDC session authentication succeeded for {method} {url}")
                                            return response
                                        else:
                                            logger.warning(f"OIDC session cookie also returned 401 for {method} {url}")
                                    else:
                                        logger.warning("OIDC session authentication failed - no session cookie obtained")
                                except Exception as oidc_error:
                                    logger.warning(f"OIDC fallback authentication failed: {oidc_error}")
                                except Exception as e:
                                    logger.error(f"OIDC session authentication also failed: {e}")
                        
                        # Log non-2xx responses for debugging
                        if not (200 <= response.status_code < 300):
                            error_text = response.text[:500] if response.text else "No error message"
                            logger.warning(
                                f"Nextcloud {method} {path}: {response.status_code} - {error_text}"
                            )
                            
                            # Provide helpful error message for 401
                            if response.status_code == 401:
                                logger.error(
                                    "Nextcloud WebDAV authentication failed with 401. "
                                    "CRITICAL CONFIGURATION ISSUE: Nextcloud is NOT accepting Bearer tokens. "
                                    "You MUST enable these settings in Nextcloud → Settings → Administration → OpenID Connect:\n"
                                    "  1. ✅ 'Do you want to allow API calls and WebDAV requests that are authenticated "
                                    "with an OIDC ID token or access token?' → YES/ENABLED\n"
                                    "  2. ✅ 'This automatically provisions the user, when sending API and WebDAV requests "
                                    "with a Bearer token...' → YES/ENABLED\n"
                                    "  3. ✅ Auto-provisioning → ENABLED\n"
                                    "See NEXTCLOUD_BEARER_TOKEN_TROUBLESHOOTING.md for detailed instructions."
                                )
                        
                        return response
                        
                except httpx.TimeoutException as e:
                    # Timeout errors - retry if enabled and not last attempt
                    if retry_on_503 and attempt < max_retries - 1:
                        wait_time = (2 ** attempt) * 0.5
                        logger.warning(
                            f"⚠️  Nextcloud request timeout. Retrying in {wait_time:.1f}s "
                            f"(attempt {attempt + 1}/{max_retries})..."
                        )
                        await asyncio.sleep(wait_time)
                        last_exception = e
                        continue
                    else:
                        raise StorageError(f"Nextcloud request timeout after {max_retries} attempts: {e}") from e
                except httpx.RequestError as e:
                    # Other request errors - retry if enabled and not last attempt
                    if retry_on_503 and attempt < max_retries - 1:
                        wait_time = (2 ** attempt) * 0.5
                        logger.warning(
                            f"⚠️  Nextcloud request error. Retrying in {wait_time:.1f}s "
                            f"(attempt {attempt + 1}/{max_retries}): {e}"
                        )
                        await asyncio.sleep(wait_time)
                        last_exception = e
                        continue
                    else:
                        raise StorageError(f"Nextcloud request failed after {max_retries} attempts: {e}") from e
            
            # If we reach here, we've exhausted all retries without returning
            # This should not happen in normal flow, but handle it gracefully
            if last_exception:
                raise StorageError(
                    f"Nextcloud request failed after {max_retries} retries. "
                    f"Last error: {last_exception}"
                ) from last_exception
            else:
                raise StorageError(
                    f"Nextcloud request failed: Unexpected error after {max_retries} attempts. "
                    f"This should not happen - please check the retry logic."
                )
        except StorageError:
            # Re-raise StorageError as-is (already has proper error message)
            raise
        except Exception as e:
            # Catch any other unexpected errors
            logger.error(f"Unexpected error in Nextcloud request: {e}", exc_info=True)
            raise StorageError(f"Unexpected error during Nextcloud request: {e}") from e
    
    async def _exchange_token_if_needed(self):
        """
        Exchange the DAVI token (from DAVI_frontend_demo) for a Nextcloud token (from nextcloud_dev).
        This is needed because Nextcloud is configured for nextcloud_dev client, but DAVI uses DAVI_frontend_demo.
        
        CRITICAL: If the original token already has nextcloud_dev in audience, use it directly!
        Token exchange often removes the audience, making it worse.
        
        CRITICAL: Nextcloud's user_oidc app may require ID token instead of access token for WebDAV.
        We'll try to get both and prefer ID token if available.
        """
        # If we already have an exchanged token, use it
        if self._exchanged_token:
            self.access_token = self._exchanged_token
            return
        
        # Import config first (before using variables)
        from app.core.config import (
            KEYCLOAK_HOST, KEYCLOAK_REALM,
            NEXTCLOUD_KEYCLOAK_CLIENT_ID, NEXTCLOUD_KEYCLOAK_CLIENT_SECRET
        )
        
        # Check if token exchange is needed
        # Decode token to check the client (azp claim) and audience (aud)
        try:
            import jwt
            decoded = jwt.decode(self._original_token, options={"verify_signature": False})
            token_client = decoded.get("azp", "")
            token_audience = decoded.get("aud", [])
            
            # Log token structure for debugging
            logger.info(
                f"Token analysis - azp: {token_client}, "
                f"aud: {token_audience}, "
                f"sub: {decoded.get('sub', 'N/A')}, "
                f"email: {decoded.get('email', 'N/A')}"
            )
            
            # Check if audience includes nextcloud_dev
            has_nextcloud_audience = (
                isinstance(token_audience, list) and NEXTCLOUD_KEYCLOAK_CLIENT_ID in token_audience
            ) or token_audience == NEXTCLOUD_KEYCLOAK_CLIENT_ID
            
            # CRITICAL: If original token already has nextcloud_dev in audience, use it directly!
            # Token exchange often removes the audience, so using original is better
            if has_nextcloud_audience:
                logger.info(
                    f"✅ Original token already has {NEXTCLOUD_KEYCLOAK_CLIENT_ID} in audience: {token_audience}. "
                    f"Using original token directly (token exchange would remove audience)."
                )
                # Use original token - it already has correct audience
                self._exchanged_token = self._original_token
                self.access_token = self._original_token
                return
            
            # If token is already from nextcloud_dev (even without audience), might work
            if token_client == NEXTCLOUD_KEYCLOAK_CLIENT_ID:
                logger.info(
                    f"Token is from {NEXTCLOUD_KEYCLOAK_CLIENT_ID} client but audience is {token_audience}. "
                    f"Will try using it, but token exchange might be needed if Nextcloud rejects it."
                )
                # Try using it first, exchange only if it fails
                return
            
            # Token is from DAVI_frontend_demo or missing audience, need to exchange
            logger.info(
                f"Token is from {token_client}, audience: {token_audience}. "
                f"Exchanging for {NEXTCLOUD_KEYCLOAK_CLIENT_ID} token with correct audience..."
            )
            
            if not NEXTCLOUD_KEYCLOAK_CLIENT_SECRET:
                logger.warning("NEXTCLOUD_KEYCLOAK_CLIENT_SECRET not configured, cannot exchange token")
                return
            
            # Exchange token using Keycloak token exchange
            # Request BOTH access token and ID token - Nextcloud may need ID token
            token_exchange_url = f"{KEYCLOAK_HOST}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token"
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Request BOTH access token and ID token with correct audience
                # Nextcloud may need ID token, and we need nextcloud_dev in audience
                response = await client.post(
                    token_exchange_url,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                        "client_id": NEXTCLOUD_KEYCLOAK_CLIENT_ID,
                        "client_secret": NEXTCLOUD_KEYCLOAK_CLIENT_SECRET,
                        "subject_token": self._original_token,
                        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                        "audience": NEXTCLOUD_KEYCLOAK_CLIENT_ID,  # Explicitly request nextcloud_dev audience
                        "requested_issuer": f"{KEYCLOAK_HOST}/realms/{KEYCLOAK_REALM}"  # Ensure correct issuer
                    }
                )
                
                # If access token exchange works, also try to get ID token
                if response.status_code == 200:
                    data = response.json()
                    # Check if we got the right audience
                    exchanged_access_token = data.get("access_token")
                    if exchanged_access_token:
                        try:
                            exchanged_decoded = jwt.decode(exchanged_access_token, options={"verify_signature": False})
                            exchanged_aud = exchanged_decoded.get("aud", [])
                            has_correct_audience = (
                                isinstance(exchanged_aud, list) and NEXTCLOUD_KEYCLOAK_CLIENT_ID in exchanged_aud
                            ) or exchanged_aud == NEXTCLOUD_KEYCLOAK_CLIENT_ID
                            
                            if not has_correct_audience:
                                logger.warning(
                                    f"⚠️  Exchanged token audience is {exchanged_aud}, expected {NEXTCLOUD_KEYCLOAK_CLIENT_ID}. "
                                    f"This may cause Nextcloud to reject the token. Check Keycloak Audience Mapper configuration."
                                )
                        except:
                            pass
                
                if response.status_code == 200:
                    data = response.json()
                    exchanged_access_token = data.get("access_token")
                    exchanged_id_token = data.get("id_token")  # Nextcloud may need ID token
                    
                    # Prefer ID token if available (Nextcloud user_oidc often requires it)
                    if exchanged_id_token:
                        self._exchanged_token = exchanged_id_token
                        self.access_token = exchanged_id_token
                        logger.info("✅ Successfully exchanged token - using ID token for Nextcloud (recommended)")
                        
                        # Verify the exchanged token structure
                        try:
                            exchanged_decoded = jwt.decode(exchanged_id_token, options={"verify_signature": False})
                            logger.info(
                                f"Exchanged ID token - azp: {exchanged_decoded.get('azp')}, "
                                f"aud: {exchanged_decoded.get('aud')}, "
                                f"iss: {exchanged_decoded.get('iss')}"
                            )
                        except:
                            pass
                        return
                    elif exchanged_access_token:
                        self._exchanged_token = exchanged_access_token
                        self.access_token = exchanged_access_token
                        logger.info("✅ Successfully exchanged token - using access token for Nextcloud")
                        
                        # Verify the exchanged token structure
                        try:
                            exchanged_decoded = jwt.decode(exchanged_access_token, options={"verify_signature": False})
                            logger.info(
                                f"Exchanged access token - azp: {exchanged_decoded.get('azp')}, "
                                f"aud: {exchanged_decoded.get('aud')}, "
                                f"iss: {exchanged_decoded.get('iss')}"
                            )
                        except:
                            pass
                        return
                    else:
                        logger.warning("Token exchange succeeded but no access_token or id_token in response")
                else:
                    error_data = response.json() if response.text else {}
                    error_msg = error_data.get("error_description", error_data.get("error", "Unknown error"))
                    
                    # Provide specific guidance for common errors
                    if "audience" in error_msg.lower():
                        logger.error(
                            f"Token exchange failed: {error_msg}. "
                            f"CRITICAL: Add 'nextcloud_dev' to the audience of DAVI_frontend_demo client in Keycloak. "
                            f"See FIX_TOKEN_EXCHANGE_AUDIENCE.md for instructions."
                        )
                    else:
                        logger.warning(
                            f"Token exchange failed: {response.status_code} - {error_msg}. "
                            f"Will try using original token. Make sure token exchange is enabled in Keycloak."
                        )
        except Exception as e:
            logger.warning(f"Token exchange error: {e}. Will try using original token.")
    
    async def _get_actual_nextcloud_user_id(self) -> str:
        """
        Query Nextcloud's user info endpoint to get the actual user ID that Nextcloud recognizes.
        This is critical because Nextcloud may use a different identifier than what we expect.
        
        Returns:
            The actual Nextcloud user ID (username) that Nextcloud recognizes
            
        Raises:
            StorageError: If the query fails
        """
        if self._actual_nextcloud_user_id:
            logger.debug(f"Using cached Nextcloud user ID: {self._actual_nextcloud_user_id}")
            return self._actual_nextcloud_user_id
        
        logger.info(
            f"Querying Nextcloud user info endpoint to resolve user ID. "
            f"Current fallback user ID: {self.nextcloud_user_id} (email: {self.username})"
        )
        
        # Note: User info endpoint might fail even with Bearer token enabled
        # if user doesn't exist yet or endpoint requires session.
        # Since WebDAV is working, we can use the fallback email.
        
        # Query Nextcloud's user info endpoint
        user_info_url = f"{self.base_url}/ocs/v2.php/cloud/user?format=json"
        
        try:
            token_to_use = self._exchanged_token or self.access_token
            logger.debug(f"Using {'exchanged' if self._exchanged_token else 'original'} token for user info query")
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    user_info_url,
                    headers={
                        "Authorization": f"Bearer {token_to_use}",
                        "OCS-APIRequest": "true"
                    }
                )
                
                logger.info(f"Nextcloud user info endpoint response: {response.status_code}")
                
                if response.status_code == 200:
                    data = response.json()
                    # Nextcloud returns user info in ocs.data format
                    if "ocs" in data and "data" in data["ocs"]:
                        user_data = data["ocs"]["data"]
                        # The 'id' field is the Nextcloud user ID (username)
                        actual_user_id = user_data.get("id") or user_data.get("user-id")
                        if actual_user_id:
                            self._actual_nextcloud_user_id = str(actual_user_id)
                            # Update webdav_base with the actual user ID
                            old_webdav_base = self.webdav_base
                            self.webdav_base = f"{self.base_url}/remote.php/dav/files/{quote(self._actual_nextcloud_user_id)}"
                            self.storage_root = f"{self.webdav_base}/{self.root_path}".rstrip("/")
                            logger.info(
                                f"Nextcloud user ID resolved: {actual_user_id} "
                                f"(from email: {self.username}, token sub: {self.nextcloud_user_id}). "
                                f"Updated webdav_base from {old_webdav_base} to {self.webdav_base}"
                            )
                            return self._actual_nextcloud_user_id
                
                # If user info endpoint returns 401, it's likely a Nextcloud OIDC limitation:
                # The endpoint may require a web session, not just Bearer token.
                # This is OK - WebDAV works with Bearer token, so we use email as fallback.
                if response.status_code == 401:
                    error_data = response.json() if response.text else {}
                    error_msg = error_data.get("ocs", {}).get("meta", {}).get("message", "Unauthorized")
                    logger.info(
                        f"Nextcloud user info endpoint returned 401: {error_msg}. "
                        f"This is expected - the endpoint may require a web session (Nextcloud OIDC limitation). "
                        f"Using email as user ID ({self.nextcloud_user_id}) - WebDAV works fine with Bearer token."
                    )
                
                # If user info endpoint doesn't work, log and use fallback
                logger.warning(
                    f"Could not get user ID from user info endpoint: {response.status_code} - {response.text[:200]}. "
                    f"Using fallback: {self.nextcloud_user_id}"
                )
                
        except Exception as e:
            logger.warning(f"Failed to query Nextcloud user info: {e}. Using fallback: {self.nextcloud_user_id}")
        
        # Fallback: Use email (username parameter) - Nextcloud expects email
        # CRITICAL: Nextcloud is configured with mappingUid=email, so we MUST use email
        # Since user info endpoint failed, we use email as fallback
        if self.username and "@" in self.username:
            # username is email - use it
            logger.info(
                f"User info endpoint failed, using email from username param: {self.username} "
                f"(Nextcloud expects email with mappingUid=email)."
            )
            # Update webdav_base with email
            old_webdav_base = self.webdav_base
            old_storage_root = self.storage_root
            self._actual_nextcloud_user_id = self.username
            self.nextcloud_user_id = self.username  # Update this too
            self.webdav_base = f"{self.base_url}/remote.php/dav/files/{quote(self.username)}"
            self.storage_root = f"{self.webdav_base}/{self.root_path}".rstrip("/")
            logger.info(
                f"✅ Updated webdav_base with email: {old_webdav_base} → {self.webdav_base}, "
                f"storage_root: {old_storage_root} → {self.storage_root}"
            )
            return self._actual_nextcloud_user_id
        else:
            # Last resort: try to extract email from token
            try:
                import jwt
                decoded = jwt.decode(self._original_token, options={"verify_signature": False})
                email_from_token = decoded.get("email")
                if email_from_token:
                    logger.warning(
                        f"username param is not email, using email from token: {email_from_token} "
                        f"(Nextcloud expects email with mappingUid=email)."
                    )
                    # Update webdav_base with email from token
                    old_webdav_base = self.webdav_base
                    old_storage_root = self.storage_root
                    self._actual_nextcloud_user_id = email_from_token
                    self.nextcloud_user_id = email_from_token  # Update this too
                    self.webdav_base = f"{self.base_url}/remote.php/dav/files/{quote(email_from_token)}"
                    self.storage_root = f"{self.webdav_base}/{self.root_path}".rstrip("/")
                    logger.info(
                        f"✅ Updated webdav_base with email from token: {old_webdav_base} → {self.webdav_base}, "
                        f"storage_root: {old_storage_root} → {self.storage_root}"
                    )
                    return self._actual_nextcloud_user_id
            except Exception as e:
                logger.debug(f"Could not extract email from token for fallback: {e}")
        
        # Final fallback: use whatever was passed in (might be wrong, but better than nothing)
        self._actual_nextcloud_user_id = self.nextcloud_user_id
        logger.error(
            f"❌ CRITICAL: No email available! Using current nextcloud_user_id: {self._actual_nextcloud_user_id}. "
            f"This may fail if Nextcloud expects email (mappingUid=email). "
            f"If Nextcloud returns 404 'Principal not found', the user may need to log into Nextcloud "
            f"via web UI first, or Nextcloud auto-provisioning needs to be enabled."
        )
        return self._actual_nextcloud_user_id
    
    def _get_token_client(self) -> str:
        """Get the client ID (azp) from the current token for debugging."""
        try:
            import jwt
            token_to_check = self._exchanged_token or self.access_token
            if token_to_check:
                decoded = jwt.decode(token_to_check, options={"verify_signature": False})
                return decoded.get("azp", "unknown")
        except:
            pass
        return "unknown"
    
    async def _authenticate_via_oidc(self) -> bool:
        """
        Authenticate with Nextcloud using Keycloak OIDC token.
        This gets a session cookie that can be used for WebDAV requests.
        
        Returns:
            True if authentication succeeded, False otherwise
        """
        try:
            # Nextcloud OIDC app uses the OIDC token to establish a session
            # We'll try to access Nextcloud's OIDC endpoint with the Bearer token
            # and extract the session cookie from the response
            
            # Method 1: Try Nextcloud's OIDC login endpoint
            oidc_login_url = f"{self.base_url}/apps/user_oidc/login"
            
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                # Try to authenticate using Bearer token
                auth_headers = {
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json"
                }
                
                # Try POST to OIDC login
                response = await client.post(
                    oidc_login_url,
                    headers=auth_headers,
                    json={"access_token": self.access_token}
                )
                
                # Check for session cookie in response
                if "Set-Cookie" in response.headers:
                    cookies = response.headers.get_list("Set-Cookie")
                    for cookie in cookies:
                        if "nc_sessionid" in cookie or "oc_sessionPassphrase" in cookie or "nc_sameSiteCookielax" in cookie:
                            # Extract the cookie value
                            cookie_parts = cookie.split(";")[0].split("=", 1)
                            if len(cookie_parts) == 2:
                                cookie_name = cookie_parts[0]
                                cookie_value = cookie_parts[1]
                                # Build full cookie string
                                self._session_cookie = f"{cookie_name}={cookie_value}"
                                logger.info("Successfully authenticated with Nextcloud OIDC via login endpoint")
                                return True
                
                # Method 2: Try accessing user info endpoint to establish session
                user_info_url = f"{self.base_url}/ocs/v2.php/cloud/user"
                response = await client.get(
                    user_info_url,
                    headers={"Authorization": f"Bearer {self.access_token}"}
                )
                
                if response.status_code == 200:
                    # Check for session cookie
                    if "Set-Cookie" in response.headers:
                        cookies = response.headers.get_list("Set-Cookie")
                        for cookie in cookies:
                            if "nc_sessionid" in cookie or "oc_sessionPassphrase" in cookie:
                                cookie_parts = cookie.split(";")[0].split("=", 1)
                                if len(cookie_parts) == 2:
                                    cookie_name = cookie_parts[0]
                                    cookie_value = cookie_parts[1]
                                    self._session_cookie = f"{cookie_name}={cookie_value}"
                                    logger.info("Successfully authenticated with Nextcloud via user info endpoint")
                                    return True
                
                logger.warning("OIDC authentication did not return a session cookie. Nextcloud may not be configured for OIDC WebDAV.")
                return False
                
        except Exception as e:
            logger.error(f"Failed to authenticate with Nextcloud OIDC: {e}")
            return False
    
    async def create_folder(self, path: str) -> bool:
        """
        Create a folder in Nextcloud using WebDAV MKCOL.
        
        Args:
            path: Logical path to the folder (relative to storage root)
            
        Returns:
            True if folder was created, False if it already exists
        """
        # Don't call _get_actual_nextcloud_user_id() - use email directly
        
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
        """
        Check if a folder exists in Nextcloud.
        
        Args:
            path: Logical path to the folder (relative to storage root)
            
        Returns:
            True if folder exists, False otherwise
        """
        full_path = self._get_full_path(path)
        # Ensure path ends with / for folders
        if not full_path.endswith("/"):
            full_path = full_path + "/"
        
        try:
            response = await self._make_request("PROPFIND", full_path, headers={"Depth": "0"})
            exists = response.status_code == 207  # 207 Multi-Status means resource exists
            if not exists:
                logger.debug(f"Folder does not exist: {path} (status: {response.status_code})")
            return exists
        except Exception as e:
            logger.warning(f"Error checking folder existence for {path}: {e}")
            return False
    
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
        
        # Don't call _get_actual_nextcloud_user_id() - use email directly
        
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
    
    async def delete_folder(self, path: str, recursive: bool = True) -> bool:
        """
        Delete a folder from Nextcloud using WebDAV DELETE.
        
        WebDAV DELETE should recursively delete folders and all their contents,
        but Nextcloud may require files to be deleted first in some cases.
        
        Args:
            path: Logical path to the folder (relative to storage root)
            recursive: If True (default), delete all files in the folder first, then the folder.
                      If False, attempt direct DELETE (may fail if folder contains files).
            
        Returns:
            True if folder was deleted, False if it didn't exist
        """
        full_path = self._get_full_path(path)
        # Ensure path ends with / for folders in WebDAV
        if not full_path.endswith("/"):
            full_path = full_path + "/"
        
        # First, try direct DELETE (WebDAV should handle recursive deletion)
        response = await self._make_request("DELETE", full_path)
        
        if response.status_code == 204:
            logger.info(f"✅ Successfully deleted Nextcloud folder (direct DELETE): {path}")
            return True
        elif response.status_code == 404:
            logger.debug(f"Folder not found in Nextcloud (may have been already deleted): {path}")
            return False
        elif response.status_code in (409, 403, 500) and recursive:
            # 409 Conflict or 403 Forbidden might mean folder contains files
            # 500 might be a temporary error
            error_text = response.text[:500] if response.text else ""
            logger.warning(
                f"⚠️  Direct DELETE failed for folder '{path}' (status: {response.status_code}). "
                f"Folder may contain files. Attempting recursive deletion by deleting files first..."
            )
            
            # Try to delete all files in the folder first, then delete the folder
            try:
                # List all files in the folder recursively
                files = await self.list_files(path, recursive=True)
                
                if files:
                    logger.info(
                        f"🗑️  Found {len(files)} file(s) in folder '{path}'. "
                        f"Deleting files first, then folder..."
                    )
                    
                    # Delete all files
                    deleted_files = 0
                    failed_files = []
                    for file_info in files:
                        file_path = file_info.get("path", "")
                        if file_path:
                            try:
                                await self.delete_file(file_path)
                                deleted_files += 1
                            except Exception as e:
                                failed_files.append((file_path, str(e)))
                                logger.warning(f"Failed to delete file '{file_path}' in folder '{path}': {e}")
                    
                    if failed_files:
                        logger.error(
                            f"❌ Failed to delete {len(failed_files)} file(s) in folder '{path}'. "
                            f"Folder deletion may fail. Failed files: {[f[0] for f in failed_files[:5]]}"
                        )
                        # Continue anyway - try to delete folder
                    
                    logger.info(
                        f"✅ Deleted {deleted_files} file(s) from folder '{path}'. "
                        f"Now attempting to delete folder..."
                    )
                
                # Also delete subfolders recursively
                subfolders = await self.list_folders(path, recursive=True)
                if subfolders:
                    logger.info(
                        f"🗑️  Found {len(subfolders)} subfolder(s) in folder '{path}'. "
                        f"Deleting subfolders first..."
                    )
                    
                    # Sort by depth (deepest first) to delete nested folders first
                    subfolders.sort(key=lambda f: f.get("depth", 0), reverse=True)
                    
                    deleted_subfolders = 0
                    for folder_info in subfolders:
                        folder_path = folder_info.get("path", "")
                        if folder_path and folder_path != path:  # Don't delete the parent folder yet
                            try:
                                # Recursively delete subfolder (this will delete its files too)
                                await self.delete_folder(folder_path, recursive=True)
                                deleted_subfolders += 1
                            except Exception as e:
                                logger.warning(f"Failed to delete subfolder '{folder_path}': {e}")
                    
                    logger.info(f"✅ Deleted {deleted_subfolders} subfolder(s) from folder '{path}'.")
                
                # Now try to delete the folder again
                logger.info(f"🔄 Retrying folder deletion after cleaning up contents: {path}")
                response = await self._make_request("DELETE", full_path)
                
                if response.status_code == 204:
                    logger.info(f"✅ Successfully deleted Nextcloud folder (after recursive cleanup): {path}")
                    return True
                elif response.status_code == 404:
                    logger.info(f"Folder '{path}' was already deleted during cleanup")
                    return False
                else:
                    error_text = response.text[:500] if response.text else ""
                    raise StorageError(
                        f"Failed to delete folder '{path}' even after deleting contents: "
                        f"{response.status_code} {error_text}"
                    )
                    
            except StorageError:
                # Re-raise StorageError as-is
                raise
            except Exception as e:
                # If recursive deletion fails, log and re-raise original error
                logger.error(
                    f"❌ Recursive deletion failed for folder '{path}': {e}",
                    exc_info=True
                )
                raise StorageError(
                    f"Failed to delete folder '{path}': {response.status_code} {response.text}. "
                    f"Recursive cleanup also failed: {e}"
                ) from e
        else:
            # Other error codes - raise with details
            error_text = response.text[:500] if response.text else ""
            raise StorageError(
                f"Failed to delete folder '{path}': {response.status_code} {error_text}"
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
        # Don't call _get_actual_nextcloud_user_id() - use email directly (already set)
        
        full_path = self._get_full_path(path)
        
        # Ensure path ends with / for folders in WebDAV PROPFIND
        if not full_path.endswith("/"):
            full_path = full_path + "/"
        
        depth = "infinity" if recursive else "1"
        
        # PROPFIND request body
        propfind_body = """<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
    <d:resourcetype/>
  </d:prop>
</d:propfind>"""
        
        # Use longer timeout for recursive listing of folders with many files
        timeout = 60.0 if recursive else 30.0
        
        response = await self._make_request(
            "PROPFIND",
            full_path,
            content=propfind_body.encode(),
            headers={"Depth": depth, "Content-Type": "application/xml"},
            timeout=timeout,
            retry_on_503=True,  # Retry on database locked errors
            max_retries=3
        )
        
        # Handle 404 - path doesn't exist, return empty list
        if response.status_code == 404:
            logger.info(f"Path {path} does not exist in Nextcloud, returning empty list")
            return []
        
        # Handle 503 - database locked (should have been retried, but log if it still fails)
        if response.status_code == 503:
            error_text = response.text[:500] if response.text else ""
            raise StorageError(
                f"Nextcloud database is locked (503 Service Unavailable) after retries. "
                f"This is a Nextcloud infrastructure issue. Please check Nextcloud logs and database. "
                f"Error: {error_text[:200]}"
            )
        
        if response.status_code != 207:
            error_text = response.text[:500] if response.text else "No error message"
            raise StorageError(
                f"Failed to list folders {path}: {response.status_code} {error_text}"
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
        # Don't call _get_actual_nextcloud_user_id() - it requires web session
        # Use email directly (already set in self.nextcloud_user_id during initialization)
        
        full_path = self._get_full_path(path)
        
        # Ensure path ends with / for folders in WebDAV PROPFIND
        if not full_path.endswith("/"):
            full_path = full_path + "/"
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
        
        try:
            response = await self._make_request(
                "PROPFIND",
                full_path,
                content=propfind_body.encode(),
                headers={"Depth": depth, "Content-Type": "application/xml"}
            )
        except StorageError as e:
            # If authentication fails, provide helpful error message
            error_msg = str(e)
            if "401" in error_msg or "NotAuthenticated" in error_msg:
                logger.error(
                    f"❌ Nextcloud authentication failed for list_files. "
                    f"Path: {path}, Full path: {full_path}. "
                    f"Error: {error_msg[:200]}"
                )
                # Re-raise with more context
                raise StorageError(
                    f"Failed to list files {path}: Authentication failed. "
                    f"Nextcloud is rejecting Bearer tokens. "
                    f"Please enable 'Allow API calls and WebDAV requests with OIDC token' in Nextcloud OIDC settings. "
                    f"Original error: {error_msg[:200]}"
                )
            raise
        
        # Handle 404 - path doesn't exist, return empty list
        if response.status_code == 404:
            logger.info(f"Path {path} does not exist in Nextcloud, returning empty list")
            return []
        
        if response.status_code != 207:
            error_text = response.text[:500] if response.text else ""
            raise StorageError(
                f"Failed to list files {path}: {response.status_code} {error_text}"
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
