from motor.motor_asyncio import AsyncIOMotorClient
from fastapi import Depends

MONGO_URI = "mongodb://localhost:27017"
DB_NAME = "DAVI"

client = AsyncIOMotorClient(MONGO_URI)
db = client[DB_NAME]

async def get_db():
    return db
