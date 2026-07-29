"""
PrivacyFlow Channel — Test Connection API Endpoint

Called from the WebUI config page "Test Connection" button.
Runs health check, auth verify, and validates the configured app_id.
Returns structured results for each check.
"""

from helpers.api import ApiHandler, Request


class PfTestConnection(ApiHandler):
    async def process(self, input: dict, request: Request):
        import importlib.util
        import os

        plugin_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..")
        )

        def _load_helper(name: str):
            path = os.path.join(plugin_dir, "helpers", f"{name}.py")
            spec = importlib.util.spec_from_file_location(
                f"privacyflow_channel.helpers.{name}", path
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

        pf_client = _load_helper("pf_client")

        # The WebUI test button sends the form values so the connection can be
        # validated before saving. Fall back to saved config / env for any field
        # the form left blank.
        api_base = (input.get("pf_api_base") or "").strip()
        api_key = (input.get("pf_api_key") or "").strip()
        app_id = (input.get("pf_app_id") or "").strip()

        checks = {
            "configured": {"ok": False, "error": ""},
            "health": {"ok": False, "error": ""},
            "auth": {"ok": False, "error": ""},
            "app_id": {"ok": False, "error": ""},
        }

        # Step 1: Check if configured
        try:
            if not pf_client.is_configured(api_base, api_key):
                checks["configured"] = {
                    "ok": False,
                    "error": "Missing required configuration. Set API Base URL, API Key, and App ID.",
                }
                return {"ok": False, "error": "Plugin not configured", "checks": checks}
            checks["configured"] = {"ok": True, "error": ""}
        except Exception as e:
            checks["configured"] = {"ok": False, "error": str(e)}
            return {"ok": False, "error": str(e), "checks": checks}

        resolved = pf_client.resolve(api_base, api_key, app_id)
        base_url = resolved["pf_api_base"]
        configured_app_id = resolved["pf_app_id"]

        # Step 2: Health check
        try:
            healthy = pf_client.health_check(api_base, api_key)
            if healthy:
                checks["health"] = {"ok": True, "error": ""}
            else:
                checks["health"] = {
                    "ok": False,
                    "error": f"Server not reachable at {base_url}/api/v1/health",
                }
                return {"ok": False, "error": "Health check failed", "checks": checks}
        except Exception as e:
            checks["health"] = {"ok": False, "error": str(e)}
            return {"ok": False, "error": str(e), "checks": checks}

        # Step 3: Auth verify
        try:
            auth_result = pf_client.verify_auth(api_base, api_key)
            if auth_result.get("valid"):
                checks["auth"] = {"ok": True, "error": ""}
            else:
                checks["auth"] = {"ok": False, "error": "API key returned invalid=false"}
                return {"ok": False, "error": "Auth verification failed", "checks": checks}
        except Exception as e:
            error_msg = str(e)
            if "401" in error_msg or "403" in error_msg:
                error_msg = "Invalid API key — server rejected credentials"
            elif "ConnectionError" in error_msg or "Connection refused" in error_msg:
                error_msg = f"Cannot connect to {base_url}"
            checks["auth"] = {"ok": False, "error": error_msg}
            return {"ok": False, "error": error_msg, "checks": checks}

        # Step 4: Validate app_id is in returned appIds
        try:
            app_ids = auth_result.get("appIds", [])
            if configured_app_id in app_ids:
                checks["app_id"] = {
                    "ok": True,
                    "error": "",
                    "detail": f"App ID '{configured_app_id}' is authorized",
                }
            else:
                checks["app_id"] = {
                    "ok": False,
                    "error": f"App ID '{configured_app_id}' is not associated with this API key. Authorized app IDs: {', '.join(app_ids) if app_ids else '(none)'}",
                }
                return {"ok": False, "error": "App ID not authorized", "checks": checks}
        except Exception as e:
            checks["app_id"] = {"ok": False, "error": str(e)}
            return {"ok": False, "error": str(e), "checks": checks}

        return {
            "ok": True,
            "error": "",
            "checks": checks,
            "detail": f"All checks passed. App ID '{configured_app_id}' is authorized.",
        }
