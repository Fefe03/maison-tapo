"""Cloud-only Tapo kiosk. Python 3.12+, one Uvicorn worker.

The library's login/discovery are synchronous, its device methods asynchronous.
All library work runs on one dedicated thread, with a fresh event loop per job.
No credentials, cloud exception bodies or cloud tokens are sent to the browser.
"""

import asyncio
import json
import logging
from base64 import b64decode
from binascii import Error as Base64Error
import os
import re
import secrets
from uuid import uuid4
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from tplinkcloud import TPLinkDeviceManager, TPLinkAuthError, TPLinkMFARequiredError, TPLinkCloudError
from tplinkcloud.api_response import TPLinkApiResponse
from tplinkcloud.client import TPLinkApi
from tplinkcloud.device import TPLinkDevice
from tplinkcloud.device_client import TPLinkDeviceClient

BEDROOM_LIGHTS = ("Ambientebeleuchtung", "Vitrine")
LOGGER = logging.getLogger("uvicorn.error")


def device_alias(device):
    alias = (device.get_alias() or "").strip() or device.device_id
    if getattr(device, "cloud_type", None) == "tapo":
        try:
            decoded = b64decode(alias, validate=True).decode("utf-8")
            if decoded.isprintable() and decoded.strip():
                return decoded.strip()
        except (Base64Error, UnicodeError, ValueError):
            pass
    return alias


def device_kind(device):
    model = device.device_info.device_model or ""
    if re.match(r"^P\d", model):
        return "plug"
    if re.match(r"^H\d", model):
        return "hub"
    if re.match(r"^L\d", model):
        return "light"
    return None


def diagnostic_model(device):
    return re.sub(r"[^A-Za-z0-9() ._-]", "?", device.device_info.device_model or "?")[:32]


class TapoApi(TPLinkApi):
    def _request_post_v2(self, base_url, url_path, body, token=None):
        response = super()._request_post_v2(base_url, url_path, body, token)
        if url_path not in ("/api/v2/account/login", "/api/v2/account/checkMFACodeAndLogin"):
            return response
        if response.successful and isinstance(response.result, dict):
            # The Tapo login may wrap a rejected login in an outer success.
            code = response.result.get("errorCode")
            if type(code) is str and re.fullmatch(r"-?\d{1,8}", code):
                code = int(code)
            if type(code) is int and code != 0:
                return TPLinkApiResponse({"error_code": code, "result": response.result})
        return response


class TapoGateway(TPLinkDeviceClient):
    last_response = "no response"

    async def _request_post(self, body, url_path="/"):
        self.last_response = "no response"
        try:
            response = await super()._request_post(body, url_path)
        except Exception as error:
            http_code = re.match(r"^(\d{3}):", str(error)) if isinstance(error, TPLinkCloudError) else None
            self.last_response = f"HTTP {http_code[1]}" if http_code else type(error).__name__
            raise
        code = response.error_code
        self.last_response = f"cloud={code}" if type(code) is int else "unknown cloud response"
        if response.successful and isinstance(response.result, dict):
            inner = response.result.get("responseData")
            if isinstance(inner, str):
                try:
                    inner = json.loads(inner)
                except ValueError:
                    inner = None
            if isinstance(inner, dict) and type(inner.get("error_code")) is int:
                self.last_response += f", device={inner['error_code']}"
        return response


class TapoSwitch(TPLinkDevice):
    def __init__(self, device, account_api, token, term_id, diagnostics=None):
        super().__init__(device._client, device.device_id, device.device_info)
        self.cloud_type = device.cloud_type
        self._diagnostics = diagnostics
        # Older gateways also accept native messages in the v1 cloud envelope.
        self._v1_client = TapoGateway(
            device._client.host, token, term_id=term_id, access_key=account_api.access_key,
            secret_key=account_api.secret_key, app_name=account_api._app_name,
            cloud_type="kasa",
        )
        self._native_client = None
        self._legacy_confirmed = False

    async def _legacy_state(self):
        info = await self.get_sys_info()
        if isinstance(info, dict) and info.get("err_code", 0) == 0:
            state = info.get("relay_state")
            if type(state) in (int, bool) and state in (0, 1):
                return bool(state)
        return None

    async def is_on(self):
        if self._legacy_confirmed:
            return await self._legacy_state()
        clients = [self._native_client] if self._native_client else [self._client, self._v1_client]
        for client in clients:
            try:
                response = await asyncio.wait_for(
                    client.pass_through_request(self.device_id, {"method": "get_device_info"}), timeout=4
                )
            except Exception:
                continue
            if isinstance(response, dict) and response.get("error_code") == 0:
                result = response.get("result")
                if isinstance(result, dict) and type(result.get("device_on")) is bool:
                    self._native_client = client
                    return result["device_on"]
        if self._native_client:
            return None
        native_v2 = getattr(self._client, "last_response", "unknown")
        try:
            state = await asyncio.wait_for(self._legacy_state(), timeout=3)
        except Exception:
            state = None
        self._legacy_confirmed = type(state) is bool
        if state is None and self._diagnostics is not None and self.device_id not in self._diagnostics:
            self._diagnostics.add(self.device_id)
            LOGGER.info("Tapo state unavailable: model=%s, native v2=%s, native v1=%s, legacy=%s",
                        diagnostic_model(self), native_v2, self._v1_client.last_response, self._client.last_response)
        return state

    async def _set_power(self, desired):
        if self._native_client:
            response = await self._native_client.pass_through_request(self.device_id, {
                "method": "set_device_info", "params": {"device_on": desired},
            })
            if isinstance(response, dict) and response.get("error_code") not in (0, None):
                raise TPLinkCloudError("Device rejected the power request")
            return response
        if self._legacy_confirmed:
            return await (super().power_on() if desired else super().power_off())
        raise TPLinkCloudError("No verified power protocol")

    async def power_on(self):
        return await self._set_power(True)

    async def power_off(self):
        return await self._set_power(False)


class TapoDeviceManager(TPLinkDeviceManager):
    def __init__(self, username, password):
        # Reuse discovery and token refresh from tplink-cloud-api 5.2.2.
        # Log in directly so the dual-cloud manager cannot swallow Tapo errors.
        term_id = str(uuid4())
        super().__init__(prefetch=False, cache_devices=False, term_id=term_id)
        self._tapo_api = TapoApi(cloud_type="tapo", term_id=term_id)
        self._diagnosed = set()
        self._inventory = None
        result = self._tapo_api.login(username, password) or {}
        self._tapo_token = result.get("token")
        self._tapo_refresh_token = result.get("refreshToken")
        if not self._tapo_token:
            raise TPLinkCloudError("Tapo login returned no token")

    async def get_devices(self):
        devices = await self._get_cloud_devices(
            self._tapo_api, self._tapo_token, self._tapo_refresh_token, "tapo"
        )
        inventory = Counter(diagnostic_model(device) for device in devices)
        if inventory != self._inventory:
            LOGGER.info("Tapo cloud inventory by model: %s", dict(inventory))
            self._inventory = inventory
        return devices

    def _construct_device(self, device_info, api, token, cloud_type):
        device = super()._construct_device(device_info, api, token, cloud_type)
        if device_kind(device) in ("plug", "light"):
            device._client = TapoGateway(
                device._client.host, token, term_id=self._term_id, access_key=api.access_key,
                secret_key=api.secret_key, app_name=api._app_name, cloud_type=cloud_type,
            )
            return TapoSwitch(device, api, token, self._term_id, self._diagnosed)
        return device


class Cloud:
    def __init__(self):
        self.manager = None
        self.login_failure = None
        self.login_retry_at = 0.0

    async def devices(self):
        if self.manager is None:
            if self.login_failure and monotonic() < self.login_retry_at:
                status, detail = self.login_failure
                raise HTTPException(status, detail)
            username, password = os.getenv("TAPO_USERNAME"), os.getenv("TAPO_PASSWORD")
            if not username or not password:
                raise HTTPException(503, "TAPO_USERNAME und TAPO_PASSWORD fehlen im Backend.")
            try:
                self.manager = TapoDeviceManager(username.strip(), password)
            except TPLinkCloudError as error:
                cooldown = 300
                if error.error_code in (-20661, -20675):
                    cooldown = 7200
                    failure = (502, "TP-Link meldet eine vorübergehende Kontosperre. Warte zwei Stunden vor dem nächsten Anmeldeversuch.")
                elif isinstance(error, TPLinkMFARequiredError):
                    failure = (503, "TP-Link verlangt einen MFA-Code. Dieser Server unterstützt noch keinen MFA-Dialog.")
                elif isinstance(error, TPLinkAuthError):
                    failure = (502, "Tapo lehnt E-Mail oder Passwort ab. Prüfe TAPO_USERNAME und TAPO_PASSWORD in Render.")
                else:
                    code = f" (Code {error.error_code})" if type(error.error_code) is int else ""
                    failure = (502, f"Tapo Cloud konnte die Anmeldung nicht abschließen{code}.")
                self.login_failure = failure
                self.login_retry_at = monotonic() + cooldown
                raise HTTPException(*failure) from None
            self.login_failure = None
        devices = await self.manager.get_devices()
        return [d for d in devices if device_kind(d)]

    async def status(self, device):
        try:
            state = await asyncio.wait_for(device.is_on(), timeout=12)
            if type(state) is not bool:
                raise ValueError("Unknown state")
            return state, None
        except Exception:
            return None, "Status nicht verfügbar"

    async def row(self, device, duplicates=()):
        kind = device_kind(device)
        alias = device_alias(device)
        if kind == "hub":
            cloud_status = getattr(device.device_info, "status", None)
            online = True if cloud_status in (1, "1") else False if cloud_status in (0, "0") else None
            return {"id": device.device_id, "alias": alias, "model": device.device_info.device_model,
                    "kind": kind, "on": None, "online": online, "controllable": False, "error": None}
        state, error = await self.status(device)
        duplicate = alias in duplicates
        return {
            "id": device.device_id,
            "alias": alias,
            "model": device.device_info.device_model,
            "kind": kind,
            "on": state,
            "online": True if state is not None else None,
            "controllable": state is not None and not duplicate,
            "error": "Alias mehrfach vergeben – in der Tapo-App umbenennen" if duplicate else error,
        }

    async def set_power(self, devices, desired, duplicates):
        async def set_one(device):
            name = device_alias(device)
            result = {"id": device.device_id, "alias": name}
            try:
                if isinstance(device, TapoSwitch):
                    state, _ = await self.status(device)
                    if state is None:
                        raise TPLinkCloudError("No confirmed power state")
                command = device.power_on if desired else device.power_off
                await asyncio.wait_for(command(), timeout=12)
                row = await self.row(device, duplicates)
                if row["on"] is desired:
                    return {**result, "success": True, "device": row}
            except Exception:
                pass
            return {**result, "success": False, "error": "Schaltstatus nicht bestätigt. Status erneut abrufen."}

        results = await asyncio.gather(*(set_one(device) for device in devices))
        return {"success": all(result["success"] for result in results), "results": results}

    async def perform(self, action, alias=None):
        devices = await self.devices()
        switches = [device for device in devices if device_kind(device) in ("plug", "light")]
        aliases = Counter(device_alias(device) for device in switches)
        duplicates = {name for name, count in aliases.items() if count > 1}
        if action == "list":
            return await asyncio.gather(*(self.row(d, duplicates) for d in devices))
        if action == "off":
            return await self.set_power(switches, False, duplicates)
        if action in ("bedroom-on", "bedroom-off"):
            if any(aliases[name] != 1 for name in BEDROOM_LIGHTS):
                raise HTTPException(409, "Beide Schlafzimmerlichter müssen genau einmal gefunden werden.")
            lights = [device for device in switches if device_alias(device) in BEDROOM_LIGHTS]
            return await self.set_power(lights, action == "bedroom-on", duplicates)
        matches = [device for device in switches if device_alias(device) == alias.strip()]
        if not matches:
            raise HTTPException(404, "Gerät nicht gefunden. Liste aktualisieren.")
        if len(matches) != 1:
            raise HTTPException(409, "Alias mehrfach vergeben. Geräte in der Tapo-App eindeutig benennen.")
        device = matches[0]
        state, _ = await self.status(device)
        if state is None:
            raise HTTPException(503, "Gerätestatus unbekannt. Es wurde kein Schaltbefehl gesendet.")
        # Set once, then verify. Never automatically retry an ambiguous write.
        try:
            command = device.power_off if state else device.power_on
            await asyncio.wait_for(command(), timeout=12)
        except Exception:
            raise HTTPException(502, "Schaltbefehl nicht bestätigt. Vor erneutem Schalten aktualisieren.") from None
        row = await self.row(device)
        if row["on"] is not (not state):
            raise HTTPException(502, "Neuer Status nicht bestätigt. Bitte aktualisieren.")
        return row


def create_app(cloud=None):
    controller = cloud or Cloud()

    @asynccontextmanager
    async def lifespan(app):
        app.state.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tapo")
        app.state.pending = None
        yield
        app.state.pool.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="MAISON · Tapo Cloud", lifespan=lifespan)
    origins = [s.strip() for s in os.getenv("CORS_ORIGINS", "*").split(",") if s.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins,
                       allow_credentials=False, allow_methods=["GET", "POST"],
                       allow_headers=["Authorization", "Content-Type"])

    def authorize(authorization: str | None = Header(default=None)):
        token = os.getenv("DASHBOARD_TOKEN", "")
        if len(token) < 24:
            raise HTTPException(503, "DASHBOARD_TOKEN im Backend setzen (mindestens 24 Zeichen).")
        supplied = authorization or ""
        if not secrets.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
            raise HTTPException(401, "Zugangsschlüssel fehlt oder ist ungültig.",
                                headers={"WWW-Authenticate": "Bearer"})

    async def run(action, alias=None):
        # Reject rather than queue writes; a timed-out browser cannot enqueue toggles.
        if app.state.pending is not None and not app.state.pending.done():
            raise HTTPException(409, "Cloud-Anfrage läuft noch. Bitte kurz warten und aktualisieren.")

        def job():
            try:
                return asyncio.run(controller.perform(action, alias))
            except HTTPException:
                raise
            except TPLinkCloudError as error:
                controller.manager = None
                code = f" (Code {error.error_code})" if type(error.error_code) is int else ""
                raise HTTPException(502, f"Tapo Cloud konnte die Anfrage nicht abschließen{code}.") from None
            except Exception:
                controller.manager = None
                raise HTTPException(502, "TP-Link Cloud nicht erreichbar. Später erneut aktualisieren.") from None

        future = asyncio.get_running_loop().run_in_executor(app.state.pool, job)
        app.state.pending = future
        # Consume eventual errors even if a disconnected client no longer awaits us.
        future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=150)
        except TimeoutError:
            raise HTTPException(504, "Cloud-Anfrage dauert zu lange. Ausgang unbekannt; bitte aktualisieren.") from None

    @app.middleware("http")
    async def no_store(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(Path(__file__).with_name("index.html"))

    @app.get("/api/devices", dependencies=[Depends(authorize)])
    async def devices():
        return await run("list")

    @app.post("/api/devices/off", dependencies=[Depends(authorize)])
    async def all_off():
        return await run("off")

    @app.post("/api/groups/bedroom-lights/{state}", dependencies=[Depends(authorize)])
    async def bedroom_lights(state: str):
        if state not in ("on", "off"):
            raise HTTPException(422, "Schaltzustand muss on oder off sein.")
        return await run(f"bedroom-{state}")

    @app.post("/api/device/{alias:path}/toggle", dependencies=[Depends(authorize)])
    async def toggle(alias: str):
        return await run("toggle", alias)

    return app


app = create_app()
