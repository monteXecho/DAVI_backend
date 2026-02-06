from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import OAuth2AuthorizationCodeBearer
from typing import Annotated
import jwt
import requests
import os

from jwt import algorithms
from keycloak import KeycloakAdmin
from app.core.config import (
    KEYCLOAK_HOST, 
    KEYCLOAK_REALM, 
    DAVI_KEYCLOAK_CLIENT_ID, 
    DAVI_KEYCLOAK_CLIENT_SECRET,
    KEYCLOAK_VERIFY_SSL
)

app = FastAPI()

# Validate required configuration
if not KEYCLOAK_HOST:
    raise ValueError("KEYCLOAK_HOST environment variable is required")
if not KEYCLOAK_REALM:
    raise ValueError("KEYCLOAK_REALM environment variable is required")

# --- URLs ---
JWKS_URL = f"{KEYCLOAK_HOST}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/certs"
TOKEN_URL = f"{KEYCLOAK_HOST}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token"
AUTH_URL = f"{KEYCLOAK_HOST}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/auth"

# --- OAuth2 ---
oauth_2_scheme = OAuth2AuthorizationCodeBearer(
    tokenUrl=TOKEN_URL,
    authorizationUrl=AUTH_URL,
)

# --- Keycloak Admin Client ---
keycloak_admin = KeycloakAdmin(
    server_url=f"{KEYCLOAK_HOST}/",
    realm_name=KEYCLOAK_REALM,  # target realm
    client_id=DAVI_KEYCLOAK_CLIENT_ID,
    client_secret_key=DAVI_KEYCLOAK_CLIENT_SECRET,
    verify=KEYCLOAK_VERIFY_SSL,
)

_kc_admin = None

def get_keycloak_admin():
    global _kc_admin
    if _kc_admin is None:
        _kc_admin = KeycloakAdmin(
            server_url=f"{KEYCLOAK_HOST}/",
            realm_name=KEYCLOAK_REALM,  
            client_id=DAVI_KEYCLOAK_CLIENT_ID,
            client_secret_key=DAVI_KEYCLOAK_CLIENT_SECRET,
            verify=KEYCLOAK_VERIFY_SSL,
        )
    return _kc_admin

# --- Helpers for JWT validation ---
def get_signing_key(token: str):
    try:
        response = requests.get(JWKS_URL, timeout=10, verify=KEYCLOAK_VERIFY_SSL)
        response.raise_for_status()  # Raise an exception for bad status codes
        jwks = response.json()
        
        # Validate JWKS structure
        if not isinstance(jwks, dict) or "keys" not in jwks:
            error_detail = f"Invalid JWKS response from Keycloak. Expected 'keys' field. Got: {list(jwks.keys()) if isinstance(jwks, dict) else type(jwks).__name__}"
            print(f"ERROR: {error_detail}. Full response: {jwks}")
            raise HTTPException(
                status_code=502,
                detail=f"Keycloak JWKS endpoint returned invalid format: {error_detail}"
            )
        
        if not isinstance(jwks["keys"], list) or len(jwks["keys"]) == 0:
            raise HTTPException(
                status_code=502,
                detail="Keycloak JWKS endpoint returned no keys"
            )
        
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")

        for key in jwks["keys"]:
            if key["kid"] == kid and key.get("use") == "sig" and key.get("alg") == "RS256":
                public_key = algorithms.RSAAlgorithm.from_jwk(key)
                return public_key

        raise HTTPException(status_code=401, detail="Invalid token: signing key not found")
    
    except requests.exceptions.RequestException as e:
        print(f"ERROR: Failed to fetch JWKS from {JWKS_URL}: {str(e)}")
        raise HTTPException(
            status_code=502,
            detail=f"Failed to connect to Keycloak JWKS endpoint: {str(e)}"
        )
    except ValueError as e:
        print(f"ERROR: Invalid JSON response from JWKS endpoint: {str(e)}")
        raise HTTPException(
            status_code=502,
            detail=f"Keycloak JWKS endpoint returned invalid JSON: {str(e)}"
        )


async def get_current_user(token: Annotated[str, Depends(oauth_2_scheme)]):
    try:
        public_key = get_signing_key(token)

        payload = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            options={"verify_exp": True},
            audience="account",
        )
        # Include raw token for Nextcloud authentication
        payload["_raw_token"] = token
        return payload

    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")


def require_role(required_role: str):
    def role_checker(user=Depends(get_current_user)):
        roles = user.get("realm_access", {}).get("roles", [])
        if required_role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required role: {required_role}",
            )
        return user
    return role_checker


def ensure_role_exists(role_name: str):
    try:
        return keycloak_admin.get_realm_role(role_name)
    except:
        keycloak_admin.create_realm_role({"name": role_name})
        return keycloak_admin.get_realm_role(role_name)
