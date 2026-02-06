#!/usr/bin/env python3
"""
Helper script to exchange a DAVI token for a Nextcloud token.

Usage:
    python exchange_token.py YOUR_DAVI_TOKEN

Or set environment variables:
    export DAVI_TOKEN="your-token-here"
    python exchange_token.py
"""

import sys
import os
import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv(".env.local")

# Configuration
KEYCLOAK_HOST = os.getenv("KEYCLOAK_HOST", "https://kc.daviapp.nl")
KEYCLOAK_REALM = os.getenv("KEYCLOAK_REALM", "DAVI")
NEXTCLOUD_KEYCLOAK_CLIENT_ID = os.getenv("NEXTCLOUD_KEYCLOAK_CLIENT_ID", "nextcloud_dev")
NEXTCLOUD_KEYCLOAK_CLIENT_SECRET = os.getenv("NEXTCLOUD_KEYCLOAK_CLIENT_SECRET", "")

def exchange_token(davi_token: str) -> dict:
    """
    Exchange a DAVI token (from DAVI_frontend_demo) for a Nextcloud token (from nextcloud_dev).
    
    Args:
        davi_token: Access token from DAVI_frontend_demo client
        
    Returns:
        Dictionary with exchanged token information
    """
    token_exchange_url = f"{KEYCLOAK_HOST}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token"
    
    if not NEXTCLOUD_KEYCLOAK_CLIENT_SECRET:
        raise ValueError("NEXTCLOUD_KEYCLOAK_CLIENT_SECRET not set in environment variables")
    
    response = requests.post(
        token_exchange_url,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "client_id": NEXTCLOUD_KEYCLOAK_CLIENT_ID,
            "client_secret": NEXTCLOUD_KEYCLOAK_CLIENT_SECRET,
            "subject_token": davi_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token"
        }
    )
    
    if response.status_code == 200:
        data = response.json()
        return {
            "success": True,
            "access_token": data.get("access_token"),
            "expires_in": data.get("expires_in"),
            "token_type": data.get("token_type"),
            "scope": data.get("scope"),
            "full_response": data
        }
    else:
        error_data = response.json() if response.text else {}
        return {
            "success": False,
            "status_code": response.status_code,
            "error": error_data.get("error", "Unknown error"),
            "error_description": error_data.get("error_description", response.text[:200])
        }


def main():
    # Get token from command line or environment
    if len(sys.argv) > 1:
        davi_token = sys.argv[1]
    else:
        davi_token = os.getenv("DAVI_TOKEN")
        if not davi_token:
            print("Usage: python exchange_token.py YOUR_DAVI_TOKEN")
            print("Or set DAVI_TOKEN environment variable")
            sys.exit(1)
    
    print(f"Exchanging token from DAVI_frontend_demo to nextcloud_dev...")
    print(f"Token preview: {davi_token[:50]}...")
    print()
    
    result = exchange_token(davi_token)
    
    if result["success"]:
        print("✅ Token exchange successful!")
        print()
        print(f"Exchanged Token: {result['access_token']}")
        print(f"Expires in: {result['expires_in']} seconds")
        print(f"Token type: {result['token_type']}")
        print(f"Scope: {result['scope']}")
        print()
        print("You can now use this token for Nextcloud API calls:")
        print(f"curl -H 'Authorization: Bearer {result['access_token']}' \\")
        print("     -H 'OCS-APIRequest: true' \\")
        print("     https://clouddavi.nl/ocs/v2.php/cloud/user?format=json")
    else:
        print("❌ Token exchange failed!")
        print(f"Status code: {result['status_code']}")
        print(f"Error: {result['error']}")
        print(f"Description: {result['error_description']}")
        sys.exit(1)


if __name__ == "__main__":
    main()

