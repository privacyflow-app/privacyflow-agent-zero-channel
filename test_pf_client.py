"""
Tests for pf_client.send_message() payload construction.

Covers the group-delivery contract: contactId is optional and omitted when
not provided, groupId is included when supplied, and contactId is included
for direct (1:1) delivery.
"""

import sys
import types
import unittest
from unittest.mock import MagicMock, patch


# --- Mock A0 helpers before importing pf_client ---
_helpers_mock = types.ModuleType("helpers")
_helpers_mock.plugins = MagicMock()
sys.modules["helpers"] = _helpers_mock
sys.modules["helpers.plugins"] = MagicMock()

try:
    import requests  # noqa: F401
except ImportError:
    sys.modules["requests"] = MagicMock()

sys.path.insert(0, "helpers")
from pf_client import send_message  # noqa: E402


class TestSendMessagePayload(unittest.TestCase):
    def setUp(self):
        _helpers_mock.plugins.get_plugin_config.return_value = {
            "pf_api_base": "https://api.privacyflow.tech",
            "pf_api_key": "test-key",
            "pf_app_id": "app-123",
        }

    def _mock_post_ok(self):
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {}
        return patch("pf_client.requests.post", return_value=resp)

    def test_direct_delivery_includes_contact_id(self):
        with self._mock_post_ok() as mock_post:
            send_message(message="hi", messenger="signal", contact_id="c1")
        payload = mock_post.call_args.kwargs["json"]["messages"][0]
        self.assertEqual(payload["contactId"], "c1")
        self.assertEqual(payload["appId"], "app-123")
        self.assertNotIn("groupId", payload)

    def test_group_only_reply_omits_contact_id_sends_group_id(self):
        with self._mock_post_ok() as mock_post:
            send_message(message="hi", messenger="signal", group_id="g1")
        payload = mock_post.call_args.kwargs["json"]["messages"][0]
        self.assertNotIn("contactId", payload)
        self.assertEqual(payload["groupId"], "g1")

    def test_group_reply_with_both_sends_both(self):
        with self._mock_post_ok() as mock_post:
            send_message(message="hi", messenger="signal", contact_id="c1", group_id="g1")
        payload = mock_post.call_args.kwargs["json"]["messages"][0]
        self.assertEqual(payload["contactId"], "c1")
        self.assertEqual(payload["groupId"], "g1")


if __name__ == "__main__":
    unittest.main()
