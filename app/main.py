import os
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.api.ask import ask_router
from app.api.upload import upload_router
from app.api.super_admin import super_admin_router
from app.api.super_admin_stats import super_admin_stats_router
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

# Middleware to track user activity for online user detection
@app.middleware("http")
async def track_user_activity(request: Request, call_next):
    """
    Track user activity for online user detection.
    Updates last_activity timestamp when users make authenticated API calls.
    """
    # Skip tracking for certain paths
    skip_paths = ["/docs", "/openapi.json", "/redoc", "/health", "/favicon.ico", "/"]
    
    if any(request.url.path.startswith(path) for path in skip_paths):
        return await call_next(request)
    
    # Process request first
    response = await call_next(request)
    
    # Track activity after request (non-blocking)
    # Extract user from Authorization header if present
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        try:
            from app.deps.db import db
            from datetime import datetime
            import jwt
            
            # Get token from header
            token = auth_header.replace("Bearer ", "")
            
            # Decode token to get email (without full validation to avoid circular dependency)
            try:
                # Decode without verification (just to get email for activity tracking)
                unverified_payload = jwt.decode(token, options={"verify_signature": False})
                user_email = unverified_payload.get("email")
                
                if user_email and db:
                    now = datetime.utcnow()
                    # Update both collections (non-blocking, fire and forget)
                    try:
                        await db.company_admins.update_one(
                            {"email": user_email},
                            {"$set": {"last_activity": now}},
                            upsert=False
                        )
                    except:
                        pass
                    try:
                        await db.company_users.update_one(
                            {"email": user_email},
                            {"$set": {"last_activity": now}},
                            upsert=False
                        )
                    except:
                        pass
            except:
                # If token decode fails, just skip activity tracking
                pass
        except Exception as e:
            # Don't fail the request if activity tracking fails
            pass
    
    return response


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

# Create output directory if it doesn't exist
HIGHLIGHTED_DIR = os.path.join(os.getcwd(), "output", "highlighted")
os.makedirs(HIGHLIGHTED_DIR, exist_ok=True)

# Mount highlighted directory for static file serving
app.mount("/highlighted", StaticFiles(directory=HIGHLIGHTED_DIR), name="highlighted")



@app.get("/")
def root():
    return {"message": "Welcome to the MijnDavi RAG API. Use /ask endpoint to querdy."}

app.include_router(ask_router)
app.include_router(upload_router)
app.include_router(super_admin_router)
app.include_router(super_admin_stats_router)
app.include_router(auth_router)
app.include_router(company_admin_router)

##  uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
