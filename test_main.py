"""Contract tests with fake devices; never contacts or switches real hardware."""
import asyncio
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

from fastapi.testclient import TestClient
from tplinkcloud.api_response import TPLinkApiResponse
from tplinkcloud.client import TPLinkApi
from main import Cloud, TapoSwitch, create_app

TOKEN = "test-only-token-not-a-real-secret-1234"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


class Device:
    def __init__(self, alias, state=False, model="P110", device_id=None):
        self.alias, self.state = alias, state
        self.device_id = device_id or alias
        self.device_info = SimpleNamespace(device_model=model, alias=alias)
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
        cases = [
            ("-20677", 503, "MFA-Code"),
            ("-20601", 502, "E-Mail oder Passwort"),
            (-20675, 502, "Kontosperre"),
            ("-20661", 502, "Kontosperre"),
        ]
        for attempt, (code, status, expected) in enumerate(cases):
            with self.subTest(code=code):
                self.cloud.manager = None
                replies = [
                    TPLinkApiResponse({"error_code": 0, "result": {}}),
                    TPLinkApiResponse({"error_code": 0, "result": {
                        "errorCode": code, "errorMsg": "sensitive-upstream-text",
                    }}),
                ]
                env = {"TAPO_USERNAME": "test@example.invalid", "TAPO_PASSWORD": "fake-password"}
                with (
                    patch.dict(os.environ, env),
                    patch("main.monotonic", return_value=attempt * 7201),
                    patch.object(TPLinkApi, "_request_post_v2", side_effect=replies) as request,
                ):
                    response = self.client.get("/api/devices", headers=HEADERS)
                    repeated = self.client.get("/api/devices", headers=HEADERS)
                self.assertEqual(request.call_count, 2)
                self.assertEqual(repeated.json(), response.json())
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

    def test_bedroom_group_targets_only_named_plugs_and_leaves_hub_read_only(self):
        from base64 import b64encode

        ambient = Device(b64encode(b"Ambientebeleuchtung").decode(), True)
        ambient.cloud_type = "tapo"
        vitrine = Device("Vitrine ", False)
        hub = Device("Wohnzimmer", None, "H100")
        hub.device_info.status = 0
        self.cloud.manager.items.extend([ambient, vitrine, hub])

        listed = self.client.get("/api/devices", headers=HEADERS).json()
        self.assertEqual(listed[-3]["alias"], "Ambientebeleuchtung")
        self.assertEqual((listed[-1]["kind"], listed[-1]["online"], listed[-1]["controllable"]),
                         ("hub", False, False))
        path = "/api/groups/bedroom-lights/on"
        self.assertEqual(self.client.post(path).status_code, 401)
        response = self.client.post(path, headers=HEADERS)
        self.assertTrue(response.json()["success"])
        self.assertEqual(ambient.commands, ["on"])
        self.assertEqual(vitrine.commands, ["on"])
        self.assertEqual(self.a.commands, [])
        self.assertEqual(self.b.commands, [])
        self.assertEqual(self.toggle("Wohnzimmer").status_code, 404)
        self.client.post("/api/devices/off", headers=HEADERS)
        self.assertEqual(hub.commands, [])

    def test_bedroom_group_refuses_partial_or_ambiguous_matches(self):
        ambient = Device("Ambientebeleuchtung")
        self.cloud.manager.items.append(ambient)
        path = "/api/groups/bedroom-lights/off"
        self.assertEqual(self.client.post(path, headers=HEADERS).status_code, 409)
        self.cloud.manager.items.extend([Device("Vitrine", device_id="v1"), Device("Vitrine", device_id="v2")])
        self.assertEqual(self.client.post(path, headers=HEADERS).status_code, 409)
        self.assertEqual(ambient.commands, [])

    def test_native_tapo_state_and_writes_use_same_confirmed_transport(self):
        device = Device("Schreibtischlampe", model="L530")
        device.cloud_type = "tapo"
        device._client = SimpleNamespace(
            host="https://test.invalid", pass_through_request=AsyncMock(return_value=None),
        )
        v1 = SimpleNamespace(pass_through_request=AsyncMock(side_effect=[
            {"error_code": 0, "result": {"device_on": False}},
            {"error_code": 0},
            {"error_code": 0, "result": {"device_on": True}},
        ]))
        api = SimpleNamespace(access_key="test", secret_key="test", _app_name="Tapo")
        with patch("main.TPLinkDeviceClient", return_value=v1):
            self.cloud.manager.items = [TapoSwitch(device, api, "fake-token", "fake-terminal")]
        response = self.toggle(device.alias)
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["kind"], response.json()["on"]), ("light", True))
        self.assertEqual(device._client.pass_through_request.await_count, 1)
        self.assertEqual(v1.pass_through_request.await_args_list[1].args,
                         (device.device_id, {"method": "set_device_info", "params": {"device_on": True}}))

    def test_native_tapo_refuses_writes_without_confirmed_state(self):
        device = Device("Vitrine")
        device.cloud_type = "tapo"
        device._client = SimpleNamespace(
            host="https://test.invalid", pass_through_request=AsyncMock(return_value={"error_code": -1003}),
        )
        v1 = SimpleNamespace(pass_through_request=AsyncMock(return_value=None))
        api = SimpleNamespace(access_key="test", secret_key="test", _app_name="Tapo")
        with patch("main.TPLinkDeviceClient", return_value=v1):
            self.cloud.manager.items = [TapoSwitch(device, api, "fake-token", "fake-terminal")]
        response = self.client.post("/api/devices/off", headers=HEADERS)
        self.assertFalse(response.json()["success"])
        self.assertEqual(device._client.pass_through_request.await_count, 2)
        self.assertEqual(v1.pass_through_request.await_count, 1)

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
