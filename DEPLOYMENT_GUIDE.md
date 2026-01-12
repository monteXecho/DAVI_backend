# DAVI Production Deployment Guide
## BIT.nl Server Deployment

This guide provides step-by-step instructions for deploying the DAVI project to a production server (BIT.nl) with Nextcloud integration.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Server Setup](#server-setup)
3. [Nextcloud Installation & Configuration](#nextcloud-installation--configuration)
4. [Docker Network Configuration](#docker-network-configuration)
5. [DAVI Backend Configuration](#davi-backend-configuration)
6. [Environment Variables](#environment-variables)
7. [Deployment Steps](#deployment-steps)
8. [Network Troubleshooting](#network-troubleshooting)
9. [Security Considerations](#security-considerations)
10. [Monitoring & Maintenance](#monitoring--maintenance)

---

## Prerequisites

### Server Requirements

- **OS**: Linux (Ubuntu 20.04+ or Debian 11+ recommended)
- **Docker**: Version 20.10+
- **Docker Compose**: Version 1.29+
- **RAM**: Minimum 4GB (8GB+ recommended)
- **Disk Space**: Minimum 50GB (100GB+ recommended for document storage)
- **CPU**: 2+ cores

### Required Services

- **MongoDB**: Included in Docker Compose
- **Nextcloud**: Separate container/service
- **Keycloak**: Authentication service (external or containerized)
- **RAG Service**: Running on port 1416 (external service)

---

## Server Setup

### 1. Install Docker and Docker Compose

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Install Docker Compose
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose

# Verify installation
docker --version
docker-compose --version
```

### 2. Create Required Directories

```bash
# Create data directories
sudo mkdir -p /var/opt/DAVI_backend/{uploads,mongo-data,output/highlighted}
sudo mkdir -p /var/backups/davi-mongo
sudo mkdir -p /var/opt/nextcloud/data

# Set permissions
sudo chown -R $USER:$USER /var/opt/DAVI_backend
sudo chown -R $USER:$USER /var/backups/davi-mongo
sudo chown -R $USER:$USER /var/opt/nextcloud
```

### 3. Create Docker Network

```bash
# Create a shared network for DAVI and Nextcloud
docker network create davi_network

# Verify network creation
docker network ls | grep davi_network
```

---

## Nextcloud Installation & Configuration

### Option A: Nextcloud as Docker Container (Recommended)

Create `/var/opt/nextcloud/docker-compose.yml`:

```yaml
version: "3.9"

networks:
  davi_network:
    external: true

services:
  nextcloud:
    image: nextcloud:29
    container_name: davi_nextcloud
    restart: unless-stopped
    ports:
      - "8081:80"  # Adjust port if needed
    environment:
      - NEXTCLOUD_ADMIN_USER=admin
      - NEXTCLOUD_ADMIN_PASSWORD=your_secure_password_here
      - MYSQL_HOST=nextcloud_db
      - MYSQL_DATABASE=nextcloud
      - MYSQL_USER=nextcloud
      - MYSQL_PASSWORD=your_db_password_here
    volumes:
      - /var/opt/nextcloud/data:/var/www/html
      - /var/opt/nextcloud/config:/var/www/html/config
    networks:
      - davi_network
    depends_on:
      - nextcloud_db

  nextcloud_db:
    image: mariadb:10.11
    container_name: davi_nextcloud_db
    restart: unless-stopped
    environment:
      - MYSQL_ROOT_PASSWORD=your_root_password_here
      - MYSQL_DATABASE=nextcloud
      - MYSQL_USER=nextcloud
      - MYSQL_PASSWORD=your_db_password_here
    volumes:
      - /var/opt/nextcloud/db:/var/lib/mysql
    networks:
      - davi_network
```

**Start Nextcloud:**

```bash
cd /var/opt/nextcloud
docker-compose up -d
```

### Option B: Nextcloud on Host (If Already Installed)

If Nextcloud is already installed on the host:

1. Ensure it's accessible on a specific port (e.g., `localhost:8081`)
2. Configure trusted domains (see below)
3. Create a DAVI user account

---

## Nextcloud Configuration

### 1. Initial Setup

1. **Access Nextcloud Web UI**: `http://your-server-ip:8081`
2. **Complete initial setup** (admin account, database, etc.)
3. **Create DAVI user account**:
   - Go to Settings → Users
   - Create user: `davi`
   - Set a strong password
   - Note: You'll need this password for DAVI configuration

### 2. Configure Trusted Domains

**Critical for WebDAV access!**

```bash
# Access Nextcloud container
docker exec -it davi_nextcloud bash

# Edit config.php
nano /var/www/html/config/config.php
```

Add your server IP/domain to `trusted_domains`:

```php
'trusted_domains' => 
  array (
    0 => 'localhost',
    1 => 'your-server-ip',
    2 => 'your-domain.com',
    3 => 'davi_nextcloud',  // Container name for Docker networking
  ),
```

**Or use occ command:**

```bash
docker exec -u 33 davi_nextcloud php occ config:system:set trusted_domains 1 --value=your-server-ip
docker exec -u 33 davi_nextcloud php occ config:system:set trusted_domains 2 --value=your-domain.com
docker exec -u 33 davi_nextcloud php occ config:system:set trusted_domains 3 --value=davi_nextcloud
```

### 3. Create DAVI Root Folder

1. **Log in to Nextcloud** as the `davi` user
2. **Create root folder**: `/DAVI` (or your preferred root path)
3. **Set permissions**: Ensure the `davi` user has read/write access

### 4. Generate App Password (Recommended)

For better security, use an App Password instead of the user password:

1. Go to Settings → Security
2. Generate App Password: `DAVI Integration`
3. Copy the generated password
4. Use this password in DAVI configuration

---

## Docker Network Configuration

### Critical: Network Setup for Nextcloud Connection

The connection issues you experienced were due to containers being on different networks. Here's the correct setup:

### 1. Create Shared Network

```bash
# Create network (if not already created)
docker network create davi_network
```

### 2. Connect Containers to Network

**If using docker-compose for Nextcloud:**

Ensure Nextcloud is on `davi_network` (see Nextcloud docker-compose.yml above).

**If Nextcloud is on host or different network:**

```bash
# Connect Nextcloud container to davi_network
docker network connect davi_network davi_nextcloud

# Verify connection
docker network inspect davi_network
```

### 3. Update DAVI Backend docker-compose

Use the production docker-compose file that includes the network configuration.

---

## DAVI Backend Configuration

### 1. Create Production Docker Compose File

Create `/var/opt/DAVI_backend/docker-compose-prod.yml`:

```yaml
version: "3.9"

networks:
  davi_network:
    external: true

services:
  app:
    build: .
    container_name: fastapi_app
    # Production command (no reload)
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4 --limit-max-requests 1000 --timeout-keep-alive 5
    ports:
      - "8000:8000"
    volumes:
      - ./app:/code/app
      - ./requirements.txt:/code/requirements.txt
      - /var/opt/DAVI_backend/uploads:/app/uploads
      - /var/opt/DAVI_backend/output/highlighted:/code/output/highlighted
    restart: unless-stopped
    depends_on:
      - mongodb
    environment:
      - MONGO_URI=mongodb://davi_user:${MONGO_PASSWORD}@mongodb:27017/davi_db?authSource=admin
      - DB_NAME=davi_db
      # Nextcloud configuration (from .env.local)
      - NEXTCLOUD_URL=${NEXTCLOUD_URL}
      - NEXTCLOUD_USERNAME=${NEXTCLOUD_USERNAME}
      - NEXTCLOUD_PASSWORD=${NEXTCLOUD_PASSWORD}
      - NEXTCLOUD_ROOT_PATH=${NEXTCLOUD_ROOT_PATH}
    networks:
      - default
      - davi_network
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3

  mongodb:
    image: mongo:6.0
    container_name: davi_mongodb
    restart: always
    ports:
      - "127.0.0.1:27017:27017"  # Only accessible from localhost
    environment:
      - MONGO_INITDB_ROOT_USERNAME=${MONGO_ROOT_USERNAME}
      - MONGO_INITDB_ROOT_PASSWORD=${MONGO_ROOT_PASSWORD}
      - MONGO_INITDB_DATABASE=davi_db
    volumes:
      - /var/opt/DAVI_backend/mongo-data:/data/db
      - /var/backups/davi-mongo:/data/backups
    networks:
      - default
    healthcheck:
      test: ["CMD", "mongosh", "--eval", "db.adminCommand('ping')"]
      interval: 30s
      timeout: 10s
      retries: 3
```

### 2. Create Environment File

Create `/var/opt/DAVI_backend/.env.local`:

```env
# MongoDB Configuration
MONGO_ROOT_USERNAME=admin
MONGO_ROOT_PASSWORD=your_secure_mongo_root_password
MONGO_PASSWORD=your_secure_davi_user_password

# Keycloak Configuration
KEYCLOAK_PUBLIC_KEY=your-keycloak-public-key-here

# Nextcloud Configuration (CRITICAL - Adjust based on your setup)
# Option 1: If Nextcloud is in Docker on same network
NEXTCLOUD_URL=http://davi_nextcloud:80

# Option 2: If Nextcloud is on host
# NEXTCLOUD_URL=http://localhost:8081

# Option 3: If Nextcloud is on different server
# NEXTCLOUD_URL=http://nextcloud-server-ip:8081

NEXTCLOUD_USERNAME=davi
NEXTCLOUD_PASSWORD=your_nextcloud_davi_user_password
NEXTCLOUD_ROOT_PATH=/DAVI

# RAG Service (if applicable)
# RAG_BASE_URL=http://rag-service-ip:1416

# Application Settings
MAX_TOKENS=1024
```

**Important Notes:**
- `NEXTCLOUD_URL` depends on your network setup:
  - **Docker network**: Use container name `http://davi_nextcloud:80`
  - **Host network**: Use `http://localhost:8081`
  - **External server**: Use full URL `http://server-ip:8081`
- Use App Password if you generated one in Nextcloud
- Keep `.env.local` secure (chmod 600)

---

## Deployment Steps

### 1. Clone/Upload DAVI Backend

```bash
cd /var/opt/DAVI_backend
# Upload your code or clone from repository
# Ensure all files are in place
```

### 2. Configure Environment

```bash
# Create .env.local (see above)
nano .env.local

# Set secure permissions
chmod 600 .env.local
```

### 3. Build and Start Services

```bash
# Build Docker image
docker-compose -f docker-compose-prod.yml build

# Start services
docker-compose -f docker-compose-prod.yml up -d

# Check logs
docker-compose -f docker-compose-prod.yml logs -f app
```

### 4. Verify Network Connectivity

```bash
# Test Nextcloud connection from DAVI container
docker exec fastapi_app python3 -c "
from app.storage.providers import get_storage_provider
provider = get_storage_provider()
print('Storage provider:', provider.base_url)
"

# Test DNS resolution
docker exec fastapi_app getent hosts davi_nextcloud

# Test HTTP connection
docker exec fastapi_app python3 -c "
import httpx
import asyncio
async def test():
    async with httpx.AsyncClient() as client:
        r = await client.get('http://davi_nextcloud:80')
        print('Status:', r.status_code)
asyncio.run(test())
"
```

### 5. Verify Nextcloud Trusted Domains

```bash
# Check trusted domains
docker exec -u 33 davi_nextcloud php occ config:system:get trusted_domains
```

---

## Network Troubleshooting

### Issue: "Temporary failure in name resolution"

**Cause**: Containers are on different networks or DNS not resolving.

**Solution**:

```bash
# 1. Verify network exists
docker network ls | grep davi_network

# 2. Check which network containers are on
docker inspect fastapi_app | grep -A 10 NetworkMode
docker inspect davi_nextcloud | grep -A 10 NetworkMode

# 3. Connect containers to same network
docker network connect davi_network fastapi_app
docker network connect davi_network davi_nextcloud

# 4. Verify DNS resolution
docker exec fastapi_app getent hosts davi_nextcloud
# Should return: 172.x.x.x davi_nextcloud
```

### Issue: "Connection refused" or "All connection attempts failed"

**Cause**: Nextcloud not accessible at the configured URL.

**Solution**:

```bash
# 1. Verify Nextcloud is running
docker ps | grep nextcloud

# 2. Test from host
curl http://localhost:8081

# 3. Test from DAVI container
docker exec fastapi_app curl http://davi_nextcloud:80

# 4. Check Nextcloud logs
docker logs davi_nextcloud --tail 50

# 5. Verify port mapping
docker port davi_nextcloud
```

### Issue: "Access through untrusted domain"

**Cause**: Nextcloud not configured to trust the request source.

**Solution**:

```bash
# Add container name to trusted domains
docker exec -u 33 davi_nextcloud php occ config:system:set trusted_domains 3 --value=davi_nextcloud

# Add server IP
docker exec -u 33 davi_nextcloud php occ config:system:set trusted_domains 4 --value=your-server-ip

# Verify
docker exec -u 33 davi_nextcloud php occ config:system:get trusted_domains
```

### Issue: "401 Unauthorized" or "403 Forbidden"

**Cause**: Incorrect credentials or user permissions.

**Solution**:

```bash
# 1. Verify credentials in .env.local
cat .env.local | grep NEXTCLOUD

# 2. Test credentials manually
docker exec fastapi_app python3 -c "
import httpx
import asyncio
async def test():
    async with httpx.AsyncClient() as client:
        r = await client.get(
            'http://davi_nextcloud:80/remote.php/dav/files/davi/',
            auth=('davi', 'your_password')
        )
        print('Status:', r.status_code)
asyncio.run(test())
"

# 3. Verify user exists in Nextcloud
docker exec -u 33 davi_nextcloud php occ user:list

# 4. Check user permissions
docker exec -u 33 davi_nextcloud php occ user:info davi
```

---

## Production Configuration Checklist

### ✅ Network Configuration

- [ ] `davi_network` Docker network created
- [ ] Nextcloud container on `davi_network`
- [ ] DAVI backend container on `davi_network`
- [ ] DNS resolution working (`davi_nextcloud` resolves)
- [ ] HTTP connectivity verified

### ✅ Nextcloud Configuration

- [ ] Nextcloud installed and accessible
- [ ] DAVI user account created
- [ ] App Password generated (recommended)
- [ ] `/DAVI` root folder created
- [ ] Trusted domains configured:
  - [ ] `localhost`
  - [ ] Server IP
  - [ ] Domain name (if applicable)
  - [ ] `davi_nextcloud` (container name)
- [ ] WebDAV enabled and accessible

### ✅ DAVI Backend Configuration

- [ ] `.env.local` file created with correct values
- [ ] `NEXTCLOUD_URL` matches network setup:
  - Docker network: `http://davi_nextcloud:80`
  - Host network: `http://localhost:8081`
  - External: `http://server-ip:8081`
- [ ] `NEXTCLOUD_USERNAME` matches Nextcloud user
- [ ] `NEXTCLOUD_PASSWORD` is correct (or App Password)
- [ ] `NEXTCLOUD_ROOT_PATH` matches created folder
- [ ] MongoDB credentials configured
- [ ] Keycloak public key configured

### ✅ Security

- [ ] `.env.local` has secure permissions (600)
- [ ] Strong passwords for all services
- [ ] MongoDB only accessible from localhost
- [ ] Firewall configured (if applicable)
- [ ] SSL/TLS configured (if using HTTPS)
- [ ] Regular backups scheduled

---

## Security Considerations

### 1. Environment Variables

```bash
# Secure .env.local file
chmod 600 /var/opt/DAVI_backend/.env.local
chown $USER:$USER /var/opt/DAVI_backend/.env.local
```

### 2. MongoDB Security

- Use strong passwords
- Bind only to localhost (already configured)
- Enable authentication
- Regular backups

### 3. Nextcloud Security

- Use App Passwords instead of user passwords
- Enable 2FA for admin account
- Regular security updates
- Configure trusted domains properly

### 4. Network Security

- Use Docker networks for internal communication
- Expose only necessary ports
- Use reverse proxy (Nginx/Apache) for HTTPS
- Configure firewall rules

### 5. SSL/TLS (Recommended)

Set up Nginx reverse proxy with SSL:

```nginx
# /etc/nginx/sites-available/davi
server {
    listen 80;
    server_name your-domain.com;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name your-domain.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

---

## Monitoring & Maintenance

### 1. Health Checks

```bash
# Check container status
docker ps

# Check logs
docker logs fastapi_app --tail 100
docker logs davi_nextcloud --tail 100

# Check network
docker network inspect davi_network
```

### 2. Backup Strategy

**MongoDB Backup:**

```bash
# Create backup script
cat > /var/opt/DAVI_backend/backup-mongo.sh << 'EOF'
#!/bin/bash
BACKUP_DIR="/var/backups/davi-mongo"
DATE=$(date +%Y%m%d_%H%M%S)
docker exec davi_mongodb mongodump --out /data/backups/$DATE
tar -czf $BACKUP_DIR/mongo_$DATE.tar.gz /var/opt/DAVI_backend/mongo-data
find $BACKUP_DIR -name "mongo_*.tar.gz" -mtime +30 -delete
EOF

chmod +x /var/opt/DAVI_backend/backup-mongo.sh

# Add to crontab (daily at 2 AM)
crontab -e
# Add: 0 2 * * * /var/opt/DAVI_backend/backup-mongo.sh
```

**Nextcloud Backup:**

```bash
# Backup Nextcloud data
tar -czf /var/backups/nextcloud_$(date +%Y%m%d).tar.gz /var/opt/nextcloud/data
```

### 3. Log Rotation

```bash
# Configure log rotation for Docker
cat > /etc/logrotate.d/docker-containers << 'EOF'
/var/lib/docker/containers/*/*.log {
    rotate 7
    daily
    compress
    size=1M
    missingok
    delaycompress
    copytruncate
}
EOF
```

### 4. Update Procedure

```bash
# 1. Backup data
./backup-mongo.sh

# 2. Pull latest code
cd /var/opt/DAVI_backend
git pull  # or upload new files

# 3. Rebuild containers
docker-compose -f docker-compose-prod.yml build

# 4. Restart services
docker-compose -f docker-compose-prod.yml up -d

# 5. Verify
docker logs fastapi_app --tail 50
```

---

## Common Issues & Solutions

### Issue: Files not syncing to Nextcloud

**Check:**
1. Storage provider initialized correctly
2. Folder has `storage_path` in database
3. Nextcloud credentials correct
4. Network connectivity

**Debug:**
```bash
# Check storage provider
docker exec fastapi_app python3 -c "
from app.storage.providers import get_storage_provider
p = get_storage_provider()
print('URL:', p.base_url)
print('User:', p.username)
"

# Test folder creation
docker exec fastapi_app python3 -c "
from app.storage.providers import get_storage_provider
import asyncio
async def test():
    p = get_storage_provider()
    result = await p.create_folder('test-folder')
    print('Created:', result)
asyncio.run(test())
"
```

### Issue: Sync from Nextcloud not working

**Check:**
1. Folders have `origin="imported"`
2. Folders have `storage_path` set
3. Nextcloud files accessible
4. Permissions correct

**Debug:**
```bash
# List files in Nextcloud
docker exec fastapi_app python3 -c "
from app.storage.providers import get_storage_provider
import asyncio
async def test():
    p = get_storage_provider()
    files = await p.list_files('your-folder-path')
    print('Files:', files)
asyncio.run(test())
"
```

---

## Production Environment Variables Reference

### Required Variables

```env
# MongoDB
MONGO_ROOT_USERNAME=admin
MONGO_ROOT_PASSWORD=<strong_password>
MONGO_PASSWORD=<strong_password>

# Nextcloud (Choose ONE based on your setup)
# Docker network:
NEXTCLOUD_URL=http://davi_nextcloud:80
# Host network:
# NEXTCLOUD_URL=http://localhost:8081
# External:
# NEXTCLOUD_URL=http://nextcloud-server:8081

NEXTCLOUD_USERNAME=davi
NEXTCLOUD_PASSWORD=<app_password_or_user_password>
NEXTCLOUD_ROOT_PATH=/DAVI

# Keycloak
KEYCLOAK_PUBLIC_KEY=<your-keycloak-public-key>
```

### Optional Variables

```env
MAX_TOKENS=1024
RAG_BASE_URL=http://rag-service:1416
```

---

## Quick Start Commands

```bash
# 1. Create network
docker network create davi_network

# 2. Start Nextcloud
cd /var/opt/nextcloud
docker-compose up -d

# 3. Configure Nextcloud trusted domains
docker exec -u 33 davi_nextcloud php occ config:system:set trusted_domains 3 --value=davi_nextcloud

# 4. Start DAVI backend
cd /var/opt/DAVI_backend
docker-compose -f docker-compose-prod.yml up -d

# 5. Verify connection
docker exec fastapi_app getent hosts davi_nextcloud
docker logs fastapi_app | grep -i nextcloud
```

---

## Support & Troubleshooting

### Log Locations

- **DAVI Backend**: `docker logs fastapi_app`
- **Nextcloud**: `docker logs davi_nextcloud`
- **MongoDB**: `docker logs davi_mongodb`

### Test Connectivity

```bash
# From DAVI container to Nextcloud
docker exec fastapi_app curl -u davi:password http://davi_nextcloud:80/remote.php/dav/files/davi/

# From host to Nextcloud
curl http://localhost:8081

# DNS resolution
docker exec fastapi_app getent hosts davi_nextcloud
```

### Verify Configuration

```bash
# Check environment variables
docker exec fastapi_app env | grep NEXTCLOUD

# Test storage provider
docker exec fastapi_app python3 -c "
from app.core.config import NEXTCLOUD_URL, NEXTCLOUD_USERNAME
print('URL:', NEXTCLOUD_URL)
print('User:', NEXTCLOUD_USERNAME)
"
```

---

## Next Steps After Deployment

1. **Test folder creation** in DAVI → Verify appears in Nextcloud
2. **Test document upload** → Verify syncs to Nextcloud
3. **Test folder import** → Import folders from Nextcloud
4. **Test sync** → Upload file to Nextcloud, sync to DAVI
5. **Configure backups** → Set up automated backups
6. **Set up monitoring** → Configure log monitoring and alerts
7. **SSL/TLS** → Configure HTTPS via reverse proxy

---

## Notes

- **Network Mode**: The production setup uses Docker networks (not `host` mode) for better isolation
- **Ports**: Adjust port mappings based on your server configuration
- **Security**: Always use strong passwords and App Passwords where possible
- **Updates**: Regularly update Docker images and Nextcloud
- **Backups**: Implement automated backup strategy before going live

---

**Last Updated**: 2026-01-06
**Version**: 1.0
