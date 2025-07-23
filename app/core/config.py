import os
from dotenv import load_dotenv

load_dotenv(".env.local")

ELASTIC_HOST = os.getenv("ELASTIC_HOST", "http://localhost:9200")
ELASTIC_USER = os.getenv("ELASTIC_USER", "elastic")
ELASTIC_PASSWORD = os.getenv("ELASTIC_PASSWORD", "elastic")
ELASTIC_INDEX = os.getenv("ELASTIC_INDEX", "haystack_test")
OPENAI_API_URL = os.getenv("OPENAI_API_URL", "https://api.openai.com/v1")
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "1024"))

AVAILABLE_MODELS = [
    'granite-3.2-8b-instruct@q8_0',
    'granite-3.2-8b-instruct@f16',
    'llama-3.3-70b-versatile',
    'OpenAI'
]
