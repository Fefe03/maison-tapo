"""Cloud-only Tapo kiosk. Python 3.12+, one Uvicorn worker.

The library's login/discovery are synchronous, its device methods asynchronous.
All library work runs on one dedicated thread, with a fresh event loop per job.
No credentials, cloud exception bodies or cloud tokens are sent to the browser.
"""

import asyncio
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

BEDROOM_LIGHTS = ("Ambientebeleuchtung", "Vitrine")


def device_alias(device):
    alias = device.get_alias() or device.device_id
    if getattr(device, "cloud_type", None) == "tapo":
        try:
            decoded = b64decode(alias, validate=True).decode("utf-8")
            if decoded.isprintable() and decoded.strip():
                return decoded
        except (Base64Error, UnicodeError, ValueError):
            pass
    return alias


def device_kind(device):
    model = device.device_info.device_model or ""
    if re.match(r"^P\d", model):
        return "plug"
    if re.match(r"^H\d", model):
        return "hub"
    return None


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


class TapoDeviceManager(TPLinkDeviceManager):
    def __init__(self, username, password):
        # Reuse discovery and token refresh from tplink-cloud-api 5.2.2.
        # Log in directly so the dual-cloud manager cannot swallow Tapo errors.
        term_id = str(uuid4())
        super().__init__(prefetch=False, cache_devices=False, term_id=term_id)
        self._tapo_api = TapoApi(cloud_type="tapo", term_id=term_id)
        result = self._tapo_api.login(username, password) or {}
        self._tapo_token = result.get("token")
        self._tapo_refresh_token = result.get("refreshToken")
        if not self._tapo_token:
            raise TPLinkCloudError("Tapo login returned no token")

    async def get_devices(self):
        return await self._get_cloud_devices(
            self._tapo_api, self._tapo_token, self._tapo_refresh_token, "tapo"
        )


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
            "online": state is not None,
            "controllable": state is not None and not duplicate,
            "error": "Alias mehrfach vergeben – in der Tapo-App umbenennen" if duplicate else error,
        }

    async def set_power(self, devices, desired, duplicates):
        async def set_one(device):
            name = device_alias(device)
            result = {"id": device.device_id, "alias": name}
            try:
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
        plugs = [device for device in devices if device_kind(device) == "plug"]
        aliases = Counter(device_alias(device) for device in plugs)
        duplicates = {name for name, count in aliases.items() if count > 1}
        if action == "list":
            return await asyncio.gather(*(self.row(d, duplicates) for d in devices))
        if action == "off":
            return await self.set_power(plugs, False, duplicates)
        if action in ("bedroom-on", "bedroom-off"):
            if any(aliases[name] != 1 for name in BEDROOM_LIGHTS):
                raise HTTPException(409, "Beide Schlafzimmerlichter müssen genau einmal gefunden werden.")
            lights = [device for device in plugs if device_alias(device) in BEDROOM_LIGHTS]
            return await self.set_power(lights, action == "bedroom-on", duplicates)
        matches = [device for device in plugs if device_alias(device) == alias]
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
