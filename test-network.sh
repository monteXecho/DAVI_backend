#!/bin/bash

# Test script to verify all containers are running and can communicate
# Run this after starting the containers with: docker-compose up -d

echo "=========================================="
echo "Testing DAVI Network Connectivity"
echo "=========================================="
echo ""

# Check if containers are running
echo "1. Checking container status..."
docker ps --filter "name=davi_\|fastapi_app" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
echo ""

# Test network connectivity
echo "2. Testing network connectivity..."
echo ""

# Test from app to mongodb
echo "Testing: app -> mongodb"
docker exec fastapi_app ping -c 2 mongodb 2>/dev/null && echo "✓ app can reach mongodb" || echo "✗ app cannot reach mongodb"
echo ""

# Test from app to nextcloud
echo "Testing: app -> nextcloud"
docker exec fastapi_app ping -c 2 nextcloud 2>/dev/null && echo "✓ app can reach nextcloud" || echo "✗ app cannot reach nextcloud"
echo ""

# Test from app to keycloak
echo "Testing: app -> keycloak"
docker exec fastapi_app ping -c 2 keycloak 2>/dev/null && echo "✓ app can reach keycloak" || echo "✗ app cannot reach keycloak"
echo ""

# Test HTTP connectivity
echo "3. Testing HTTP endpoints..."
echo ""

# Test Nextcloud
echo "Testing Nextcloud HTTP (http://nextcloud:80)..."
docker exec fastapi_app curl -s -o /dev/null -w "HTTP Status: %{http_code}\n" http://nextcloud:80 2>/dev/null || echo "✗ Cannot reach Nextcloud (curl may not be installed)"
echo ""

# Test Keycloak
echo "Testing Keycloak HTTP (http://keycloak:8080)..."
docker exec fastapi_app curl -s -o /dev/null -w "HTTP Status: %{http_code}\n" http://keycloak:8080 2>/dev/null || echo "✗ Cannot reach Keycloak (curl may not be installed)"
echo ""

# Show network details
echo "4. Network information..."
docker network inspect davi_network --format '{{range .Containers}}{{.Name}}: {{.IPv4Address}}{{"\n"}}{{end}}' 2>/dev/null || echo "Network not found"
echo ""

# Test DNS resolution
echo "5. Testing DNS resolution from app container..."
docker exec fastapi_app nslookup mongodb 2>/dev/null | grep -q "Name:" && echo "✓ mongodb DNS resolution works" || echo "✗ mongodb DNS resolution failed"
docker exec fastapi_app nslookup nextcloud 2>/dev/null | grep -q "Name:" && echo "✓ nextcloud DNS resolution works" || echo "✗ nextcloud DNS resolution failed"
docker exec fastapi_app nslookup keycloak 2>/dev/null | grep -q "Name:" && echo "✓ keycloak DNS resolution works" || echo "✗ keycloak DNS resolution failed"
echo ""

echo "=========================================="
echo "Test completed!"
echo "=========================================="
echo ""
echo "To test manually:"
echo "  docker exec -it fastapi_app ping nextcloud"
echo "  docker exec -it fastapi_app ping keycloak"
echo "  docker exec -it fastapi_app ping mongodb"
