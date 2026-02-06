import os
from dotenv import load_dotenv

# Try to load .env.local if it exists (for local development)
# In production, environment variables should be set directly
try:
    load_dotenv(".env.local")
except:
    pass  # .env.local might not exist in production

# Keycloak Configuration
KEYCLOAK_HOST = os.getenv("KEYCLOAK_HOST", "http://host.docker.internal:8080")
KEYCLOAK_REALM = os.getenv("KEYCLOAK_REALM", "DAVI")

# Nextcloud Keycloak Client Configuration
NEXTCLOUD_KEYCLOAK_CLIENT_ID = os.getenv("NEXTCLOUD_KEYCLOAK_CLIENT_ID", "nextcloud_dev")
NEXTCLOUD_KEYCLOAK_CLIENT_SECRET = os.getenv("NEXTCLOUD_KEYCLOAK_CLIENT_SECRET", "")

# Nextcloud Storage Configuration
NEXTCLOUD_URL = os.getenv("NEXTCLOUD_URL", "http://localhost:8081")
NEXTCLOUD_ROOT_PATH = os.getenv("NEXTCLOUD_ROOT_PATH", "/DAVI")

# DAVI Keycloak Client Configuration (for admin operations)
DAVI_KEYCLOAK_CLIENT_ID = os.getenv("DAVI_KEYCLOAK_CLIENT_ID", "DAVI_client")
DAVI_KEYCLOAK_CLIENT_SECRET = os.getenv("DAVI_KEYCLOAK_CLIENT_SECRET", "")

# SSL Verification (set to "false" if using self-signed certificates)
KEYCLOAK_VERIFY_SSL = os.getenv("KEYCLOAK_VERIFY_SSL", "true").lower() == "true"
