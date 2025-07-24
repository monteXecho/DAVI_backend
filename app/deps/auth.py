from fastapi import FastAPI, Depends, HTTPException
from fastapi.security import OAuth2AuthorizationCodeBearer
from typing import Annotated
import jwt
import requests

from jwt import algorithms

app = FastAPI()

oauth_2_scheme = OAuth2AuthorizationCodeBearer(
    tokenUrl="http://localhost:8080/realms/DAVI/protocol/openid-connect/token",
    authorizationUrl="http://localhost:8080/realms/DAVI/protocol/openid-connect/auth",
)

JWKS_URL = "http://localhost:8080/realms/DAVI/protocol/openid-connect/certs"

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
            audience="account",  # update as needed
        )
        return payload

    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")
