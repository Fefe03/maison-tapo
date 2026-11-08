"""Cloud-only Tapo kiosk. Python 3.12+, one Uvicorn worker.

The library's login/discovery are synchronous, its device methods asynchronous.
All library work runs on one dedicated thread, with a fresh event loop per job.
No credentials, cloud exception bodies or cloud tokens are sent to the browser.
"""

import asyncio
import os
import re
import secrets
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from tplinkcloud import TPLinkDeviceManager, TPLinkAuthError, TPLinkMFARequiredError


class Cloud:
    def __init__(self):
        self.manager = None

    async def devices(self):
        if self.manager is None:
            username, password = os.getenv("TAPO_USERNAME"), os.getenv("TAPO_PASSWORD")
            if not username or not password:
                raise HTTPException(503, "TAPO_USERNAME und TAPO_PASSWORD fehlen im Backend.")
            self.manager = TPLinkDeviceManager(
                username, password, prefetch=False, cache_devices=False, include_tapo=True
            )
        # v5.2.2 swallows Tapo-login errors. Do not report a false empty success.
        if not self.manager.get_tapo_token():
            self.manager = None
            raise HTTPException(502, "Tapo-Cloud-Anmeldung fehlgeschlagen. Zugang und gegebenenfalls MFA prüfen.")
        devices = await self.manager.get_devices()
        # Only P-series sockets: exclude cameras, hubs and other account devices.
        # Model matching also retains sockets deduplicated into the Kasa cloud.
        return [d for d in devices if re.match(r"^P\d", d.device_info.device_model or "")]

    async def status(self, device):
        try:
            state = await asyncio.wait_for(device.is_on(), timeout=12)
            if type(state) is not bool:
                raise ValueError("Unknown state")
            return state, None
        except Exception:
            return None, "Status nicht verfügbar"

    async def row(self, device, duplicates=()):
        state, error = await self.status(device)
        alias = device.get_alias() or device.device_id
        duplicate = alias in duplicates
        return {
            "id": device.device_id,
            "alias": alias,
            "model": device.device_info.device_model,
            "on": state,
            "online": state is not None,
            "controllable": state is not None and not duplicate,
            "error": "Alias mehrfach vergeben – in der Tapo-App umbenennen" if duplicate else error,
        }

    async def perform(self, action, alias=None):
        devices = await self.devices()
        aliases = Counter(d.get_alias() or d.device_id for d in devices)
        duplicates = {name for name, count in aliases.items() if count > 1}
        if action == "list":
            return await asyncio.gather(*(self.row(d, duplicates) for d in devices))
        if action == "off":
            # Explicit power_off, never toggle; include unavailable devices in failures.
            async def turn_off(d):
                name = d.get_alias() or d.device_id
                try:
                    await asyncio.wait_for(d.power_off(), timeout=12)
                    row = await self.row(d, duplicates)
                    if row["on"] is not False:
                        raise ValueError("Off not confirmed")
                    return {"id": d.device_id, "alias": name, "success": True, "device": row}
                except Exception:
                    return {"id": d.device_id, "alias": name, "success": False,
                            "error": "Ausschalten nicht bestätigt. Status erneut abrufen."}
            results = await asyncio.gather(*(turn_off(d) for d in devices))
            return {"success": all(r["success"] for r in results), "results": results}
        matches = [d for d in devices if (d.get_alias() or d.device_id) == alias]
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
            except TPLinkMFARequiredError:
                controller.manager = None
                raise HTTPException(503, "TP-Link verlangt einen MFA-Code. Dieser minimale Server unterstützt keinen MFA-Dialog.") from None
            except TPLinkAuthError:
                controller.manager = None
                raise HTTPException(502, "TP-Link-Anmeldung fehlgeschlagen. Backend-Zugangsdaten prüfen.") from None
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

    @app.post("/api/device/{alias:path}/toggle", dependencies=[Depends(authorize)])
    async def toggle(alias: str):
        return await run("toggle", alias)

    return app


app = create_app()
