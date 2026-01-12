# DAVI Production Deployment - Quick Start Guide
## BIT.nl Server - Step by Step

This is a condensed quick-start guide. For detailed information, see `DEPLOYMENT_GUIDE.md`.

---

## Prerequisites Check

```bash
# Verify Docker
docker --version  # Should be 20.10+
docker-compose --version  # Should be 1.29+

# Verify directories exist
ls -la /var/opt/DAVI_backend
ls -la /var/opt/nextcloud
```

---

## Step 1: Create Docker Network

```bash
docker network create davi_network
docker network ls | grep davi_network
```

**Expected output**: `davi_network` should appear in the list.

---

## Step 2: Deploy Nextcloud

### Option A: Using Provided docker-compose (Recommended)

```bash
# Navigate to Nextcloud directory
cd /var/opt/nextcloud

# Create docker-compose.yml (or use provided nextcloud-docker-compose.yml)
# Copy nextcloud-docker-compose.yml to docker-compose.yml
cp /var/opt/DAVI_backend/nextcloud-docker-compose.yml docker-compose.yml

# Edit environment variables
nano docker-compose.yml
# Set NEXTCLOUD_ADMIN_PASSWORD

# Start Nextcloud
docker-compose up -d

# Wait for Nextcloud to initialize (30-60 seconds)
docker logs davi_nextcloud -f
# Press Ctrl+C when you see "Apache is running"
```

### Option B: Nextcloud Already Installed

If Nextcloud is already running on the host:

1. Note the port (e.g., `8081`)
2. Skip to Step 3

---

## Step 3: Configure Nextcloud

### 3.1 Access Nextcloud Web UI

```bash
# Get server IP
hostname -I | awk '{print $1}'

# Access in browser
# http://your-server-ip:8081
```

### 3.2 Complete Initial Setup

1. Create admin account
2. Choose database (SQLite for simple setup, or MySQL if configured)
3. Complete setup wizard

### 3.3 Create DAVI User

1. Go to **Settings → Users**
2. Click **Add user**
3. Username: `davi`
4. Password: Set a strong password (save this!)
5. Click **Create user**

### 3.4 Generate App Password (Recommended)

1. Log in as `davi` user
2. Go to **Settings → Security**
3. Scroll to **Devices & sessions**
4. Click **Create new app password**
5. Name: `DAVI Integration`
6. Copy the generated password (save this!)

### 3.5 Create DAVI Root Folder

1. Log in as `davi` user
2. Click **Files** in top menu
3. Click **New folder**
4. Name: `DAVI`
5. Click **Create**

### 3.6 Configure Trusted Domains

```bash
# Add container name
docker exec -u 33 davi_nextcloud php occ config:system:set trusted_domains 1 --value=davi_nextcloud

# Add server IP (replace with your actual IP)
SERVER_IP=$(hostname -I | awk '{print $1}')
docker exec -u 33 davi_nextcloud php occ config:system:set trusted_domains 2 --value=$SERVER_IP

# Add localhost
docker exec -u 33 davi_nextcloud php occ config:system:set trusted_domains 3 --value=localhost

# Verify
docker exec -u 33 davi_nextcloud php occ config:system:get trusted_domains
```

**Expected output:**
```
localhost
davi_nextcloud
your-server-ip
```

---

## Step 4: Configure DAVI Backend

### 4.1 Create Environment File

```bash
cd /var/opt/DAVI_backend

# Create .env.local
cat > .env.local << 'EOF'
# MongoDB Configuration
MONGO_ROOT_USERNAME=admin
MONGO_ROOT_PASSWORD=CHANGE_THIS_STRONG_PASSWORD
MONGO_PASSWORD=CHANGE_THIS_STRONG_PASSWORD

# Keycloak Configuration
KEYCLOAK_PUBLIC_KEY=YOUR_KEYCLOAK_PUBLIC_KEY_HERE

# Nextcloud Configuration
# IMPORTANT: Choose ONE based on your setup
# If Nextcloud is in Docker on davi_network:
NEXTCLOUD_URL=http://davi_nextcloud:80

# If Nextcloud is on host (not in Docker):
# NEXTCLOUD_URL=http://localhost:8081

# If Nextcloud is on different server:
# NEXTCLOUD_URL=http://nextcloud-server-ip:8081

NEXTCLOUD_USERNAME=davi
NEXTCLOUD_PASSWORD=YOUR_DAVI_USER_PASSWORD_OR_APP_PASSWORD
NEXTCLOUD_ROOT_PATH=/DAVI

# Application Settings
MAX_TOKENS=1024
EOF

# Edit with your actual values
nano .env.local

# Secure the file
chmod 600 .env.local
```

### 4.2 Verify Network Connection

```bash
# Test DNS resolution
docker exec davi_nextcloud getent hosts davi_nextcloud
# Should return an IP address

# Test HTTP connection
docker exec davi_nextcloud curl -I http://localhost:80
# Should return HTTP 200 or 302
```

---

## Step 5: Deploy DAVI Backend

### 5.1 Build and Start

```bash
cd /var/opt/DAVI_backend

# Build image
docker-compose -f docker-compose-production.yml build

# Start services
docker-compose -f docker-compose-production.yml up -d

# Check status
docker-compose -f docker-compose-production.yml ps
```

### 5.2 Verify Connectivity

```bash
# Test DNS from DAVI container
docker exec fastapi_app getent hosts davi_nextcloud
# Should return: 172.x.x.x davi_nextcloud

# Test HTTP connection
docker exec fastapi_app python3 -c "
import httpx
import asyncio
async def test():
    async with httpx.AsyncClient() as client:
        r = await client.get('http://davi_nextcloud:80', timeout=5.0)
        print('Status:', r.status_code)
asyncio.run(test())
"
# Should return: Status: 200 or 302

# Test storage provider initialization
docker exec fastapi_app python3 -c "
from app.storage.providers import get_storage_provider
p = get_storage_provider()
print('✅ Storage provider initialized')
print('URL:', p.base_url)
print('User:', p.username)
"
# Should print configuration without errors
```

### 5.3 Check Logs

```bash
# DAVI Backend logs
docker logs fastapi_app --tail 50

# Nextcloud logs
docker logs davi_nextcloud --tail 50

# Look for any errors
docker logs fastapi_app | grep -i error
docker logs davi_nextcloud | grep -i error
```

---

## Step 6: Test Integration

### 6.1 Test Folder Creation

1. Access DAVI frontend
2. Go to **Mappen** (Folders)
3. Create a new folder
4. Verify it appears in Nextcloud:
   - Log in to Nextcloud as `davi` user
   - Check `/DAVI` folder
   - New folder should be visible

### 6.2 Test Document Upload

1. Upload a document to a folder in DAVI
2. Verify in Nextcloud:
   - Check the folder in Nextcloud
   - Document should be visible

### 6.3 Test Folder Import

1. Create a folder in Nextcloud (as `davi` user)
2. In DAVI, go to **Mappen → Importeren**
3. Click **Vernieuwen** (Refresh)
4. Folder should appear in the list
5. Select and import
6. Verify folder appears in DAVI

### 6.4 Test Sync from Nextcloud

1. Upload a document directly to Nextcloud folder
2. In DAVI, go to **Mappen → Importeren**
3. Click **Synchroniseer van Nextcloud**
4. Document should appear in DAVI

---

## Troubleshooting

### Issue: "Temporary failure in name resolution"

**Fix:**
```bash
# Verify network
docker network inspect davi_network

# Reconnect containers
docker network connect davi_network fastapi_app
docker network connect davi_network davi_nextcloud

# Restart containers
docker restart fastapi_app
docker restart davi_nextcloud
```

### Issue: "Access through untrusted domain"

**Fix:**
```bash
# Add missing trusted domain
docker exec -u 33 davi_nextcloud php occ config:system:set trusted_domains 4 --value=davi_nextcloud
```

### Issue: "401 Unauthorized" or "403 Forbidden"

**Fix:**
1. Verify credentials in `.env.local`
2. Test credentials manually:
```bash
docker exec fastapi_app python3 -c "
import httpx
import asyncio
async def test():
    async with httpx.AsyncClient() as client:
        r = await client.get(
            'http://davi_nextcloud:80/remote.php/dav/files/davi/',
            auth=('davi', 'YOUR_PASSWORD')
        )
        print('Status:', r.status_code)
asyncio.run(test())
"
```

### Issue: Containers can't communicate

**Fix:**
```bash
# Verify both are on same network
docker inspect fastapi_app | grep -A 5 Networks
docker inspect davi_nextcloud | grep -A 5 Networks

# Both should show davi_network

# If not, connect them
docker network connect davi_network fastapi_app
docker network connect davi_network davi_nextcloud
```

---

## Production Checklist

Before going live, verify:

- [ ] Docker network `davi_network` exists
- [ ] Nextcloud container running and healthy
- [ ] DAVI backend container running and healthy
- [ ] DNS resolution works (`davi_nextcloud` resolves)
- [ ] HTTP connectivity works
- [ ] Trusted domains configured in Nextcloud
- [ ] DAVI user exists in Nextcloud
- [ ] `/DAVI` folder exists in Nextcloud
- [ ] `.env.local` has correct credentials
- [ ] Test folder creation works
- [ ] Test document upload works
- [ ] Test folder import works
- [ ] Test sync from Nextcloud works
- [ ] Backups configured
- [ ] Logs monitored
- [ ] SSL/TLS configured (if using HTTPS)

---

## Maintenance Commands

```bash
# View logs
docker logs fastapi_app -f
docker logs davi_nextcloud -f

# Restart services
docker-compose -f docker-compose-production.yml restart

# Update and rebuild
cd /var/opt/DAVI_backend
git pull  # or upload new files
docker-compose -f docker-compose-production.yml build
docker-compose -f docker-compose-production.yml up -d

# Backup MongoDB
docker exec davi_mongodb mongodump --out /data/backups/$(date +%Y%m%d)

# Check network
docker network inspect davi_network
```

---

## Support

If you encounter issues:

1. Check logs: `docker logs fastapi_app` and `docker logs davi_nextcloud`
2. Verify network: `docker network inspect davi_network`
3. Test connectivity: Use the test commands in Step 5.2
4. Review DEPLOYMENT_GUIDE.md for detailed troubleshooting

---

**Quick Reference:**

- **Network**: `davi_network` (must be created first)
- **Nextcloud URL**: `http://davi_nextcloud:80` (Docker network) or `http://localhost:8081` (host)
- **Nextcloud User**: `davi`
- **Root Path**: `/DAVI`
- **Trusted Domains**: Must include `davi_nextcloud`, server IP, and `localhost`
