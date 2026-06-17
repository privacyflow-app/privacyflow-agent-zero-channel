"""
PrivacyFlow Public API Client

Wraps the 4 endpoints from privacyflow-public-backend-api:
- GET  /api/v1/health
- GET  /api/v1/auth/verify
- GET  /api/v1/messages/poll
- POST /api/v1/messages/send
"""

import os
import requests
from typing import Optional


def _get_base_url() -> str:
    return os.getenv("PF_API_BASE", "").rstrip("/")


def _get_api_key() -> str:
    return os.getenv("PF_API_KEY", "")


def _get_app_id() -> str:
    return os.getenv("PF_APP_ID", "")


def _get_headers() -> dict:
    return {"Authorization": f"Bearer {_get_api_key()}"}


def health_check() -> bool:
    """GET /api/v1/health"""
    try:
        resp = requests.get(f"{_get_base_url()}/api/v1/health", timeout=10)
        return resp.ok
    except Exception:
        return False


def verify_auth() -> dict:
    """GET /api/v1/auth/verify"""
    resp = requests.get(
        f"{_get_base_url()}/api/v1/auth/verify",
        headers=_get_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def poll_messages(limit: int = 10) -> dict:
    """GET /api/v1/messages/poll?limit=N"""
    resp = requests.get(
        f"{_get_base_url()}/api/v1/messages/poll?limit={limit}",
        headers=_get_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def send_message(
    contact_id: str,
    message: str,
    messenger: str,
    group_id: Optional[str] = None,
) -> dict:
    """POST /api/v1/messages/send"""
    msg = {
        "appId": _get_app_id(),
        "contactId": contact_id,
        "message": message,
        "messenger": messenger,
    }
    if group_id:
        msg["groupId"] = group_id

    resp = requests.post(
        f"{_get_base_url()}/api/v1/messages/send",
        headers={**_get_headers(), "Content-Type": "application/json"},
        json={"messages": [msg]},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()
