# Nextcloud Keycloak SSO Configuration Guide

This guide explains how to configure Nextcloud to use Keycloak for authentication, enabling SSO between DAVI and Nextcloud.

## Overview

DAVI and Nextcloud now share the same Keycloak realm for authentication. This means:
- Company admins log into Nextcloud using their Keycloak credentials (same as DAVI)
- No Nextcloud admin username/password is needed
- All authentication goes through Keycloak OIDC

## Prerequisites

1. Keycloak server running with the "DAVI" realm configured
2. Nextcloud server accessible
3. Both services on the same network (davi_network)

## Step 1: Install Nextcloud OIDC App

1. Log into Nextcloud as admin (temporary, for setup only)
2. Go to Apps → Search for "OpenID Connect user backend"
3. Install the app

## Step 2: Configure Keycloak Client for Nextcloud

1. Log into Keycloak Admin Console (http://localhost:8080)
2. Select the "DAVI" realm
3. Go to Clients → Create Client
4. Configure:
   - **Client ID**: `nextcloud`
   - **Client Protocol**: `openid-connect`
   - **Access Type**: `confidential`
   - **Valid Redirect URIs**: 
     - `http://localhost:8081/apps/user_oidc/code`
     - `https://your-nextcloud-domain.com/apps/user_oidc/code` (for production)
   - **Web Origins**: `+` (allows all origins)
5. Save and note the **Client Secret**

## Step 3: Configure Nextcloud OIDC

1. In Nextcloud, go to Settings → Administration → OpenID Connect
2. Configure:
   - **Provider URL**: `http://keycloak:8080/realms/DAVI` (internal) or `http://localhost:8080/realms/DAVI` (external)
   - **Client ID**: `nextcloud`
   - **Client Secret**: (from Step 2)
   - **Auto-provisioning**: Enabled (creates users automatically)
   - **Update user info on login**: Enabled
3. Save configuration

## Step 4: Map Keycloak Attributes

In Nextcloud OIDC settings, configure attribute mapping:
- **User ID**: `preferred_username` or `email`
- **Display Name**: `name` or `preferred_username`
- **Email**: `email`

## Step 5: Test Authentication

1. Log out of Nextcloud admin
2. Click "Login with OpenID Connect" or use the Keycloak login
3. Enter your Keycloak credentials (same as DAVI)
4. You should be logged into Nextcloud

## Step 6: Configure WebDAV with Bearer Token

Nextcloud WebDAV now accepts Bearer tokens from Keycloak. DAVI automatically:
- Uses the logged-in user's Keycloak access token
- Sends it as `Authorization: Bearer <token>` header
- Nextcloud validates the token with Keycloak

## Troubleshooting

### Users can't log in
- Check Keycloak client configuration
- Verify redirect URIs match exactly
- Check Nextcloud logs: `docker logs davi_nextcloud`

### WebDAV requests fail
- Ensure Nextcloud OIDC app is installed and configured
- Verify Bearer token is being sent (check DAVI logs)
- Check Nextcloud accepts Bearer tokens for WebDAV

### Token validation fails
- Verify Keycloak realm is accessible from Nextcloud
- Check network connectivity between Nextcloud and Keycloak
- Ensure Keycloak client secret is correct

## Environment Variables

DAVI no longer needs:
- `NEXTCLOUD_USERNAME` (removed)
- `NEXTCLOUD_PASSWORD` (removed)

DAVI still needs:
- `NEXTCLOUD_URL`: Nextcloud server URL (e.g., `http://nextcloud:80` for internal, `http://localhost:8081` for external)
- `NEXTCLOUD_ROOT_PATH`: Root path in Nextcloud (default: `/DAVI`)

## Security Notes

- All authentication is handled by Keycloak
- No passwords are stored in DAVI
- Each user authenticates with their own Keycloak token
- Tokens are short-lived and refreshed automatically
- Users can only access their own data (enforced by Nextcloud + Keycloak)

