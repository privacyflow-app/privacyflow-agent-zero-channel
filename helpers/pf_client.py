"""
PrivacyFlow Public API Client

Wraps the 4 endpoints from privacyflow-public-backend-api:
- GET  /api/v1/health
- GET  /api/v1/auth/verify
- GET  /api/v1/messages/poll
- POST /api/v1/messages/send

Reads credentials from A0 plugin config (config.json) with env var fallback.
"""

import os
import requests
from typing import Optional

from helpers import plugins


PLUGIN_NAME = "privacyflow_channel"


def _get_config() -> dict:
    """Read plugin config, falling back to env vars."""
    config = plugins.get_plugin_config(PLUGIN_NAME) or {}
    return {
        "pf_api_base": config.get("pf_api_base") or os.getenv("PF_API_BASE", ""),
        "pf_api_key": config.get("pf_api_key") or os.getenv("PF_API_KEY", ""),
        "pf_app_id": config.get("pf_app_id") or os.getenv("PF_APP_ID", ""),
    }


def get_base_url() -> str:
    return _get_config()["pf_api_base"].rstrip("/")


def _get_api_key() -> str:
    return _get_config()["pf_api_key"]


def get_app_id() -> str:
    return _get_config()["pf_app_id"]


def _get_headers() -> dict:
    return {"Authorization": f"Bearer {_get_api_key()}"}


def resolve(
    api_base: Optional[str] = None,
    api_key: Optional[str] = None,
    app_id: Optional[str] = None,
) -> dict:
    """Resolve credentials with explicit args taking precedence over saved config / env.

    The WebUI "Test Connection" button passes the values typed into the form
    before they are saved, so the test validates what the user entered rather
    than the last-saved config.
    """
    cfg = _get_config()
    return {
        "pf_api_base": (api_base or cfg["pf_api_base"]).rstrip("/"),
        "pf_api_key": api_key or cfg["pf_api_key"],
        "pf_app_id": app_id or cfg["pf_app_id"],
    }


def is_configured(
    api_base: Optional[str] = None,
    api_key: Optional[str] = None,
) -> bool:
    """Check if all required credentials are present."""
    cfg = resolve(api_base, api_key)
    return bool(cfg["pf_api_base"] and cfg["pf_api_key"])


def health_check(
    api_base: Optional[str] = None,
    api_key: Optional[str] = None,
) -> bool:
    """GET /api/v1/health"""
    cfg = resolve(api_base, api_key)
    try:
        resp = requests.get(f"{cfg['pf_api_base']}/api/v1/health", timeout=10)
        return resp.ok
    except Exception:
        return False


def verify_auth(
    api_base: Optional[str] = None,
    api_key: Optional[str] = None,
) -> dict:
    """GET /api/v1/auth/verify"""
    cfg = resolve(api_base, api_key)
    resp = requests.get(
        f"{cfg['pf_api_base']}/api/v1/auth/verify",
        headers={"Authorization": f"Bearer {cfg['pf_api_key']}"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def poll_messages(limit: int = 10) -> dict:
    """GET /api/v1/messages/poll?limit=N"""
    resp = requests.get(
        f"{get_base_url()}/api/v1/messages/poll?limit={limit}",
        headers=_get_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def send_message(
    contact_id: Optional[str] = None,
    message: str = "",
    messenger: str = "",
    group_id: Optional[str] = None,
) -> dict:
    """POST /api/v1/messages/send

    contactId is OPTIONAL — group delivery is the default target for an app
    (resolved by the messenger from the app's active group, or supplied as
    groupId). contactId is only required for direct (1:1) delivery.
    """
    msg = {
        "appId": get_app_id(),
        "message": message,
        "messenger": messenger,
    }
    if contact_id:
        msg["contactId"] = contact_id
    if group_id:
        msg["groupId"] = group_id

    resp = requests.post(
        f"{get_base_url()}/api/v1/messages/send",
        headers={**_get_headers(), "Content-Type": "application/json"},
        json={"messages": [msg]},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()
