import os
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
# from app.api.ask import ask_router
from app.api.upload import upload_router
from app.api.super_admin import super_admin_router

app = FastAPI(
    title="MijnDAVI API",
    description="API for answering questions based on documents stored in a vector database.\n\n© 2025 by Rick Hoekman.",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],  
    allow_headers=["*"],
)

# app.mount(
#     "/output/highlighted",
#     StaticFiles(directory=os.path.abspath("output/highlighted")),
#     name="highlighted"
# )

@app.get("/")
def root():
    return {"message": "Welcome to the MijnDavi RAG API. Use /ask endpoint to querdy."}

# app.include_router(ask_router)
app.include_router(upload_router)
app.include_router(super_admin_router)

##  uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
