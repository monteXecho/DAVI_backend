import os
from dotenv import load_dotenv

load_dotenv(".env.local")

MAX_TOKENS = int(os.getenv("MAX_TOKENS", "1024"))
KEYCLOAK_PUBLIC_KEY = os.getenv("KEYCLOAK_PUBLIC_KEY ", "")


# Nextcloud Storage Configuration
# DAVI uses Nextcloud as the storage backend with Keycloak SSO authentication.
# Each user authenticates to Nextcloud using their Keycloak access token.
# No admin username/password is needed - all authentication goes through Keycloak.
NEXTCLOUD_URL = os.getenv("NEXTCLOUD_URL", "http://localhost:8081")
NEXTCLOUD_ROOT_PATH = os.getenv("NEXTCLOUD_ROOT_PATH", "/DAVI")
