# MijnDAVI Backend API

© 2025 by MijnDAVI

## Overview

MijnDAVI Backend is a production-ready FastAPI application that powers a Retrieval-Augmented Generation (RAG) system for childcare center management. The system enables managers and staff to query internal documents using natural language and receive accurate, contextual answers from their documentation corpus.

The backend integrates with external RAG services, manages document storage, handles user authentication via Keycloak, and provides comprehensive administrative APIs for multi-tenant company management.

## Architecture

### Core Components

1. **FastAPI Application** (`app/main.py`)
   - RESTful API with automatic OpenAPI documentation
   - CORS middleware for cross-origin requests
   - Static file serving for highlighted PDFs
   - Modular router architecture

2. **Authentication & Authorization** (`app/deps/auth.py`)
   - Keycloak JWT token validation
   - Role-based access control (RBAC)
   - User context management
   - Secure token parsing and validation

3. **Database Layer** (`app/deps/db.py`)
   - MongoDB integration via Motor (async driver)
   - Connection pooling and management
   - Repository pattern implementation

4. **RAG Integration** (`app/api/rag.py`)
   - External RAG service communication
   - Document indexing pipeline
   - Query processing with context
   - Async HTTP client for non-blocking operations

5. **Document Management**
   - PDF upload and storage
   - Document highlighting with snippet extraction
   - Folder and role-based organization
   - Private and public document handling

6. **Multi-Tenant System**
   - Company isolation
   - Workspace management
   - Admin hierarchy (Super Admin → Company Admin → Users)
   - Module-based feature access

## Technology Stack

- **Framework**: FastAPI 0.116.2
- **Database**: MongoDB 6.0 (via Motor async driver)
- **Authentication**: Keycloak (JWT tokens)
- **PDF Processing**: PyMuPDF 1.26.3
- **HTTP Client**: httpx (async)
- **Validation**: Pydantic 1.10.21
- **Logging**: Rich 14.1.0
- **Server**: Uvicorn with ASGI

## Project Structure

```
DAVI_backend/
├── app/
│   ├── main.py                    # FastAPI application entry point
│   ├── api/
│   │   ├── ask.py                 # RAG query endpoint
│   │   ├── upload.py              # Document upload endpoint
│   │   ├── auth.py                # Authentication endpoints
│   │   ├── super_admin.py         # Super admin operations
│   │   ├── company_admin.py       # Company admin operations
│   │   └── rag.py                 # RAG service integration
│   ├── core/
│   │   ├── config.py              # Environment configuration
│   │   └── highlight_snippet_in_pdf.py  # PDF highlighting logic
│   ├── deps/
│   │   ├── auth.py                # Authentication dependencies
│   │   ├── context.py             # Request context management
│   │   └── db.py                  # Database connection
│   ├── models/
│   │   ├── schema.py              # Request/response models
│   │   ├── company_admin_schema.py
│   │   └── company_user_schema.py
│   └── repositories/
│       ├── company_repo.py        # Company data access
│       └── document_repo.py      # Document data access
├── docker-compose.yml             # Local development setup
├── Dockerfile                     # Production container
├── requirements.txt               # Python dependencies
└── README.md                      # This file
```

## Installation

### Prerequisites

- Python 3.12+
- MongoDB 6.0+ (local or remote)
- Docker and Docker Compose (for containerized setup)
- Keycloak instance (for authentication)
- External RAG service (for document querying)

### Local Development Setup

1. **Clone the repository**
   ```bash
   cd DAVI_backend
   ```

2. **Create virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables**
   
   Create a `.env.local` file in the root directory:
   ```env
   # MongoDB Configuration
   MONGO_URI=mongodb://localhost:27017/davi_db
   DB_NAME=davi_db

   # Keycloak Configuration
   KEYCLOAK_PUBLIC_KEY=your-keycloak-public-key

   # RAG Service Configuration
   RAG_INDEX_URL=http://localhost:1416/davi_indexing/run
   RAG_QUERY_URL=http://localhost:1416/davi_query/run

   # OpenAI Configuration (optional)
   OPENAI_API_URL=https://api.openai.com/v1
   MAX_TOKENS=1024
   ```

5. **Start MongoDB** (if using Docker)
   ```bash
   docker-compose up -d mongodb
   ```

6. **Run the development server**
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
   ```

The API will be available at `http://localhost:8000` with interactive documentation at `http://localhost:8000/docs`.

## API Endpoints

### Authentication

- `POST /auth/register` - Register a new user

### RAG Query

- `POST /ask/run` - Query documents using RAG
  - Requires authentication
  - Returns answer with highlighted document snippets
  - Supports multiple LLM models

### Document Management

- `POST /upload` - Upload and index documents
  - Supports PDF files
  - Automatic RAG indexing
  - Folder and role assignment

### Super Admin

- `GET /super-admin/companies` - List all companies
- `POST /super-admin/companies` - Create new company
- `POST /super-admin/companies/{company_id}/admins` - Add company admin
- `PATCH /super-admin/companies/{company_id}/admins/{admin_id}` - Update admin
- `POST /super-admin/companies/{company_id}/admins/{admin_id}/modules` - Assign modules
- `DELETE /super-admin/companies/{company_id}` - Delete company
- `DELETE /super-admin/companies/{company_id}/admins/{admin_id}` - Remove admin

### Company Admin

- `GET /company-admin/user` - Get current user info
- `GET /company-admin/users` - List company users
- `POST /company-admin/users` - Create user
- `POST /company-admin/users/teamlid` - Create team member
- `POST /company-admin/users/upload` - Bulk import users
- `PUT /company-admin/users/{user_id}` - Update user
- `DELETE /company-admin/users` - Delete users
- `POST /company-admin/users/reset-password` - Reset user password
- `GET /company-admin/documents` - List documents
- `GET /company-admin/documents/private` - List private documents
- `POST /company-admin/documents/delete` - Delete documents
- `POST /company-admin/folders` - Create folder
- `GET /company-admin/folders` - List folders
- `POST /company-admin/folders/delete` - Delete folder
- `POST /company-admin/roles` - Create role
- `GET /company-admin/roles` - List roles
- `POST /company-admin/roles/delete` - Delete role
- `POST /company-admin/roles/assign` - Assign role to user
- `POST /company-admin/roles/upload/{folder_name}` - Upload documents to role
- `GET /company-admin/stats` - Get company statistics
- `POST /company-admin/guest-access` - Create guest access
- `GET /company-admin/guest-workspaces` - List guest workspaces

## Authentication

The API uses Keycloak for authentication. All protected endpoints require a valid JWT token in the `Authorization` header:

```
Authorization: Bearer <jwt_token>
```

The token is validated using Keycloak's public key, and user information is extracted from the token claims.

### User Roles

- **Super Admin**: Full system access, can manage all companies
- **Company Admin**: Manages users, documents, and settings within their company
- **User**: Standard user with access to documents based on assigned roles and folders

## Database Schema

### Collections

- **companies**: Company information and configuration
- **users**: User accounts and profiles
- **documents**: Document metadata and references
- **folders**: Document folder organization
- **roles**: Role definitions and permissions

## Docker Deployment

### Development

```bash
docker-compose up -d
```

This starts:
- FastAPI application on port 8000
- MongoDB on port 27017 (internal)

### Production

```bash
docker build -t mijndavi-backend .
docker run -p 8000:8000 --env-file .env.local mijndavi-backend
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MONGO_URI` | `mongodb://localhost:27017/DAVI` | MongoDB connection string |
| `DB_NAME` | `DAVI` | MongoDB database name |
| `KEYCLOAK_PUBLIC_KEY` | - | Keycloak realm public key for JWT validation |
| `RAG_INDEX_URL` | `http://localhost:1416/davi_indexing/run` | RAG indexing service URL |
| `RAG_QUERY_URL` | `http://localhost:1416/davi_query/run` | RAG query service URL |
| `OPENAI_API_URL` | `https://api.openai.com/v1` | OpenAI API base URL |
| `MAX_TOKENS` | `1024` | Maximum tokens for LLM responses |

## Development Guidelines

### Code Style

- Follow PEP 8 conventions
- Use type hints for function parameters and return values
- Document complex functions with docstrings
- Use async/await for I/O operations

### Error Handling

- Use FastAPI's `HTTPException` for API errors
- Log errors with appropriate levels
- Return meaningful error messages to clients
- Handle database connection errors gracefully

### Testing

```bash
# Run tests (when implemented)
pytest tests/
```

### Logging

The application uses Rich for enhanced console logging. Log levels:
- `INFO`: Normal operations
- `WARNING`: Non-critical issues
- `ERROR`: Errors that need attention
- `DEBUG`: Detailed debugging information

## Production Considerations

1. **Security**
   - Use HTTPS/TLS for all connections
   - Implement rate limiting
   - Validate and sanitize all inputs
   - Use environment variables for secrets
   - Rotate JWT keys regularly

2. **Performance**
   - Enable connection pooling for MongoDB
   - Use async operations for I/O-bound tasks
   - Implement caching where appropriate
   - Monitor API response times

3. **Monitoring**
   - Set up application logging
   - Monitor database performance
   - Track API usage and errors
   - Set up health check endpoints

4. **Scalability**
   - Use load balancers for multiple instances
   - Implement horizontal scaling
   - Use message queues for async tasks
   - Consider database sharding for large datasets

## Troubleshooting

### Common Issues

1. **MongoDB Connection Failed**
   - Verify MongoDB is running: `docker-compose ps`
   - Check `MONGO_URI` configuration
   - Ensure network connectivity

2. **Keycloak Authentication Errors**
   - Verify `KEYCLOAK_PUBLIC_KEY` is correct
   - Check token expiration
   - Ensure Keycloak server is accessible

3. **RAG Service Unavailable**
   - Verify RAG service is running
   - Check `RAG_INDEX_URL` and `RAG_QUERY_URL`
   - Review network connectivity

4. **Document Upload Failures**
   - Check file permissions
   - Verify upload directory exists
   - Ensure sufficient disk space

5. **413 Payload Too Large Error (Production)**
   - **Root Cause**: Reverse proxy (Nginx) has a default `client_max_body_size` of 1MB
   - **Solution**: Update your Nginx configuration to allow larger uploads:
     ```nginx
     client_max_body_size 100M;  # or your desired limit
     proxy_request_buffering off;  # Stream large uploads directly
     ```
   - See `nginx.conf.example` for a complete configuration example
   - After updating Nginx config, reload: `sudo nginx -s reload` or `sudo systemctl reload nginx`
   - **Note**: This only affects production deployments with a reverse proxy. Local development typically doesn't have this limit.

## License

© 2025 by MijnDAVI. All rights reserved.

---

## Quick Reference

### Start Development Server
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### View API Documentation
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

### Check Health
```bash
curl http://localhost:8000/
```
