import os
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.api.ask import ask_router
from app.api.upload import upload_router
from app.api.super_admin import super_admin_router
from app.api.auth import auth_router
from app.api.company_admin import company_admin_router

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

# Middleware to handle 413 errors and provide better error messages
@app.middleware("http")
async def handle_large_requests(request: Request, call_next):
    try:
        response = await call_next(request)
        return response
    except Exception as e:
        # Check if it's a 413 error or payload too large error
        error_str = str(e).lower()
        if "413" in error_str or "payload too large" in error_str or "request entity too large" in error_str:
            return JSONResponse(
                status_code=413,
                content={
                    "detail": "File too large. Please ensure your reverse proxy (e.g., Nginx) allows uploads larger than 1MB. Contact your administrator to increase client_max_body_size."
                }
            )
        raise

# HIGHLIGHTED_DIR = os.path.join("/var/opt/DAVI_backend/output/highlighted")
# app.mount("/highlighted", StaticFiles(directory=HIGHLIGHTED_DIR), name="highlighted")


HIGHLIGHTED_DIR = os.path.join(os.getcwd(), "output", "highlighted")
app.mount("/highlighted", StaticFiles(directory=HIGHLIGHTED_DIR), name="highlighted")



@app.get("/")
def root():
    return {"message": "Welcome to the MijnDavi RAG API. Use /ask endpoint to querdy."}

app.include_router(ask_router)
app.include_router(upload_router)
app.include_router(super_admin_router)
app.include_router(auth_router)
app.include_router(company_admin_router)

##  uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
