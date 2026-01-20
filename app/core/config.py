import os
from dotenv import load_dotenv

load_dotenv(".env.local")

MAX_TOKENS = int(os.getenv("MAX_TOKENS", "1024"))
KEYCLOAK_PUBLIC_KEY = os.getenv("KEYCLOAK_PUBLIC_KEY ", "")


# Nextcloud Storage Configuration
# These are used for the storage abstraction layer
# DAVI uses Nextcloud as the storage backend while maintaining
# logical folder structure and permissions in MongoDB
NEXTCLOUD_URL = os.getenv("NEXTCLOUD_URL", "http://localhost:8081/")
NEXTCLOUD_USERNAME = os.getenv("NEXTCLOUD_USERNAME", "admin")
NEXTCLOUD_PASSWORD = os.getenv("NEXTCLOUD_PASSWORD", "XRCjP-KPgyf-747Ki-ETNyo-edBaK")
NEXTCLOUD_ROOT_PATH = os.getenv("NEXTCLOUD_ROOT_PATH", "/DAVI")
