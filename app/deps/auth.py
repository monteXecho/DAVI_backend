from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import OAuth2AuthorizationCodeBearer
from typing import Annotated
import jwt
import requests
import os

from jwt import algorithms
from keycloak import KeycloakAdmin

app = FastAPI()

KEYCLOAK_HOST = os.getenv("KEYCLOAK_HOST", "host.docker.internal")
REALM = "DAVI"

# --- URLs ---
JWKS_URL = f"http://{KEYCLOAK_HOST}:8080/realms/{REALM}/protocol/openid-connect/certs"
TOKEN_URL = f"http://{KEYCLOAK_HOST}:8080/realms/{REALM}/protocol/openid-connect/token"
AUTH_URL = f"http://{KEYCLOAK_HOST}:8080/realms/{REALM}/protocol/openid-connect/auth"

# --- OAuth2 ---
oauth_2_scheme = OAuth2AuthorizationCodeBearer(
    tokenUrl=TOKEN_URL,
    authorizationUrl=AUTH_URL,
)

# --- Keycloak Admin Client ---
keycloak_admin = KeycloakAdmin(
    server_url=f"http://{KEYCLOAK_HOST}:8080/",
    realm_name="DAVI",  # target realm
    client_id="DAVI_client",
    client_secret_key="b5WpZUjTp4K8cvqn16TAtJtxhSZ66sA6",
    verify=True,
)

_kc_admin = None

def get_keycloak_admin():
    global _kc_admin
    if _kc_admin is None:
        _kc_admin = KeycloakAdmin(
        server_url=f"http://{KEYCLOAK_HOST}:8080/",
        realm_name="DAVI",  
        client_id="DAVI_client",
        client_secret_key="b5WpZUjTp4K8cvqn16TAtJtxhSZ66sA6",
        verify=True,
        )
    return _kc_admin

# --- Helpers for JWT validation ---
def get_signing_key(token: str):
    jwks = requests.get(JWKS_URL).json()
    unverified_header = jwt.get_unverified_header(token)
    kid = unverified_header.get("kid")

    for key in jwks["keys"]:
        if key["kid"] == kid and key.get("use") == "sig" and key.get("alg") == "RS256":
            public_key = algorithms.RSAAlgorithm.from_jwk(key)
            return public_key

    raise HTTPException(status_code=401, detail="Invalid token: signing key not found")


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
