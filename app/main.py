from fastapi import FastAPI
from app.api.ask import ask_router

app = FastAPI(
    title="MijnDAVI API",
    description="API for answering questions based on documents stored in a vector database.\n\n© 2025 by Rick Hoekman.",
    version="1.0.0"
)

@app.get("/")
def root():
    return {"message": "Welcome to the MijnDavi RAG API. Use /ask endpoint to query."}

app.include_router(ask_router)

##  uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
