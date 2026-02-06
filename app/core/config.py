import os
from dotenv import load_dotenv

load_dotenv(".env.local")

MAX_TOKENS = int(os.getenv("MAX_TOKENS", "1024"))
KEYCLOAK_PUBLIC_KEY = os.getenv("KEYCLOAK_PUBLIC_KEY ", "")

# Keycloak Configuration
KEYCLOAK_HOST = os.getenv("KEYCLOAK_HOST", "http://host.docker.internal:8080")
KEYCLOAK_REALM = os.getenv("KEYCLOAK_REALM", "DAVI")

# Nextcloud Keycloak Client Configuration
# Nextcloud uses a different client (nextcloud_dev) than DAVI (DAVI_frontend_demo)
# We use Keycloak token exchange to convert tokens between clients
NEXTCLOUD_KEYCLOAK_CLIENT_ID = os.getenv("NEXTCLOUD_KEYCLOAK_CLIENT_ID", "nextcloud_dev")
NEXTCLOUD_KEYCLOAK_CLIENT_SECRET = os.getenv("NEXTCLOUD_KEYCLOAK_CLIENT_SECRET", "b8plClMZgoyvg0pU2JHaW3LprRSiVZN5")

# Nextcloud Storage Configuration
# DAVI uses Nextcloud as the storage backend with Keycloak SSO authentication.
# Each user authenticates to Nextcloud using their Keycloak access token.
# No admin username/password is needed - all authentication goes through Keycloak.
NEXTCLOUD_URL = os.getenv("NEXTCLOUD_URL", "http://localhost:8081")
NEXTCLOUD_ROOT_PATH = os.getenv("NEXTCLOUD_ROOT_PATH", "/DAVI")
