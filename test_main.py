"""Contract tests with fake devices; never contacts or switches real hardware."""
import asyncio
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

from fastapi.testclient import TestClient
from tplinkcloud.api_response import TPLinkApiResponse
from tplinkcloud.client import TPLinkApi
from main import Cloud, create_app

TOKEN = "test-only-token-not-a-real-secret-1234"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


class Device:
    def __init__(self, alias, state=False, model="P110", device_id=None):
        self.alias, self.state = alias, state
        self.device_id = device_id or alias
        self.device_info = SimpleNamespace(device_model=model)
        self.commands = []
        self.fail_write = False

    def get_alias(self):
        return self.alias

    async def is_on(self):
        return self.state

    async def power_on(self):
        self.commands.append("on")
        if not self.fail_write:
            self.state = True

    async def power_off(self):
        self.commands.append("off")
        if not self.fail_write:
            self.state = False


class Manager:
    def __init__(self, devices):
        self.items = devices

    def get_tapo_token(self):
        return "fake-tapo-token"

    async def get_devices(self):
        return self.items


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"DASHBOARD_TOKEN": TOKEN, "CORS_ORIGINS": "*"})
        self.env.start()
        self.a, self.b = Device("Wohnzimmer / gemütlich", True), Device("Nachttisch", False)
        self.cloud = Cloud()
        self.cloud.manager = Manager([self.a, self.b])
        self.client = TestClient(create_app(self.cloud))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.env.stop()

    def toggle(self, alias):
        return self.client.post("/api/device/" + quote(alias, safe="") + "/toggle", headers=HEADERS)

    def test_auth_required_and_cors_preflight(self):
        self.assertEqual(self.client.get("/api/devices").status_code, 401)
        self.assertEqual(self.client.post("/api/devices/off").status_code, 401)
        self.assertEqual(self.client.post("/api/device/test/toggle").status_code, 401)
        r = self.client.options("/api/devices", headers={"Origin":"https://example.org", "Access-Control-Request-Method":"GET", "Access-Control-Request-Headers":"authorization"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["access-control-allow-origin"], "*")
        self.assertEqual(self.a.commands, [])

    def test_missing_token_fails_closed(self):
        with patch.dict(os.environ, {"DASHBOARD_TOKEN": ""}):
            self.assertEqual(self.client.get("/api/devices", headers=HEADERS).status_code, 503)

    def test_nested_tapo_login_errors_are_reported_without_secrets(self):
        for code, status, expected in [
            ("-20677", 503, "MFA-Code"),
            ("-20601", 502, "E-Mail oder Passwort"),
            (-20675, 502, "Kontosperre"),
        ]:
            with self.subTest(code=code):
                self.cloud.manager = None
                replies = [
                    TPLinkApiResponse({"error_code": 0, "result": {}}),
                    TPLinkApiResponse({"error_code": 0, "result": {
                        "errorCode": code, "errorMsg": "sensitive-upstream-text",
                    }}),
                ]
                env = {"TAPO_USERNAME": "test@example.invalid", "TAPO_PASSWORD": "fake-password"}
                with patch.dict(os.environ, env), patch.object(
                    TPLinkApi, "_request_post_v2", side_effect=replies
                ):
                    response = self.client.get("/api/devices", headers=HEADERS)
                self.assertEqual(response.status_code, status)
                self.assertIn(expected, response.json()["detail"])
                self.assertNotIn("sensitive-upstream-text", response.text)

    def test_list_filters_cameras_and_preserves_unknown(self):
        self.b.state = None
        self.cloud.manager.items.append(Device("Kamera", True, "C200"))
        r = self.client.get("/api/devices", headers=HEADERS)
        self.assertEqual(len(r.json()), 2)
        self.assertIsNone(r.json()[1]["on"])
        self.assertFalse(r.json()[1]["controllable"])
        self.assertEqual(r.headers["cache-control"], "no-store")

    def test_toggle_with_slash_and_unicode_alias(self):
        r = self.toggle(self.a.alias)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIs(r.json()["on"], False)
        self.assertEqual(self.a.commands, ["off"])

    def test_unknown_state_does_not_switch(self):
        self.a.state = None
        self.assertEqual(self.toggle(self.a.alias).status_code, 503)
        self.assertEqual(self.a.commands, [])

    def test_missing_and_duplicate_alias(self):
        self.assertEqual(self.toggle("missing").status_code, 404)
        self.cloud.manager.items.append(Device(self.a.alias, device_id="duplicate"))
        self.assertEqual(self.toggle(self.a.alias).status_code, 409)
        rows = self.client.get("/api/devices", headers=HEADERS).json()
        self.assertFalse(rows[0]["controllable"])
        self.assertEqual(self.a.commands, [])

    def test_master_off_is_idempotent(self):
        for _ in range(2):
            r = self.client.post("/api/devices/off", headers=HEADERS)
            self.assertTrue(r.json()["success"])
            self.assertIs(self.a.state, False)
            self.assertIs(self.b.state, False)
        self.assertEqual(self.b.commands, ["off", "off"])

    def test_partial_failure_does_not_claim_all_off(self):
        self.a.fail_write = True
        r = self.client.post("/api/devices/off", headers=HEADERS).json()
        self.assertFalse(r["success"])
        self.assertFalse(r["results"][0]["success"])
        self.assertTrue(r["results"][1]["success"])

    def test_unconfirmed_toggle_is_not_retried(self):
        self.a.fail_write = True
        self.assertEqual(self.toggle(self.a.alias).status_code, 502)
        self.assertEqual(self.a.commands, ["off"])

    def test_health_works_during_cloud_request_and_second_write_rejected(self):
        entered, release = threading.Event(), threading.Event()
        original = self.cloud.perform
        async def slow(action, alias=None):
            entered.set()
            release.wait(timeout=3)
            return await original(action, alias)
        self.cloud.perform = slow
        results = []
        thread = threading.Thread(target=lambda: results.append(self.toggle(self.a.alias)))
        thread.start()
        try:
            self.assertTrue(entered.wait(timeout=2))
            self.assertEqual(self.client.get("/health").status_code, 200)
            self.assertEqual(self.toggle(self.b.alias).status_code, 409)
        finally:
            release.set()
            thread.join(timeout=3)
        self.assertEqual(results[0].status_code, 200)
        self.assertEqual(self.b.commands, [])

    def test_cloud_exception_is_sanitized(self):
        async def broken(*args):
            raise RuntimeError("secret-password-do-not-return")
        self.cloud.perform = broken
        r = self.client.get("/api/devices", headers=HEADERS)
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("secret-password", r.text)


if __name__ == "__main__":
    unittest.main()
