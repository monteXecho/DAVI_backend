import os
from dotenv import load_dotenv

load_dotenv(".env.local")

MAX_TOKENS = int(os.getenv("MAX_TOKENS", "1024"))
KEYCLOAK_PUBLIC_KEY = os.getenv("KEYCLOAK_PUBLIC_KEY ", "")


# Nextcloud Storage Configuration
# These are used for the storage abstraction layer
# DAVI uses Nextcloud as the storage backend while maintaining
# logical folder structure and permissions in MongoDB
NEXTCLOUD_URL = os.getenv("NEXTCLOUD_URL", "https://demo.daviapp.nl/nextcloud")
NEXTCLOUD_USERNAME = os.getenv("NEXTCLOUD_USERNAME", "davi")
NEXTCLOUD_PASSWORD = os.getenv("NEXTCLOUD_PASSWORD", "rbKbe-ppcpE-mzEQ5-KLk29-aJs29")
NEXTCLOUD_ROOT_PATH = os.getenv("NEXTCLOUD_ROOT_PATH", "/DAVI")
