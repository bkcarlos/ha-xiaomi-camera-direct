"""Private camera worker, executed by the isolated glibc Python runtime.

There is no RTSP server, go2rtc process, web UI, or Docker dependency here.
The installed bridge modules supply OAuth, reconnect and bitstream handling.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import io
import json
import logging
import os
import secrets
import signal
import socket
import time
from pathlib import Path

import av

# Set these before importing modules that capture their default paths.
import bridge.const as constants
from aiohttp import web

DATA = Path(os.environ["XIAOMI_CAMERA_DATA"])
constants.DATA_DIR = str(DATA)
constants.CREDENTIALS_FILE = str(DATA / "credentials.json")
constants.CACHE_DIR = str(DATA / "cache")
constants.SETTINGS_FILE = str(DATA / "settings.json")

from bridge.account import AccountManager
from bridge.cameras import CameraRegistry
from bridge.config import VideoQuality
from bridge.framing import MediaKind
from bridge.mux import StreamMuxer
from bridge.redact import install as install_redaction
from bridge.settings import SettingsStore
from bridge.store import CredentialStore
from bridge.streaming import SessionManager

LOGGER = logging.getLogger("xiaomi_camera_direct.worker")


class Worker:
    def __init__(self):
        self.account = AccountManager(CredentialStore())
        self.settings = SettingsStore(constants.SETTINGS_FILE)
        self.registry = None
        self.sessions = None
        self.bound_client = None
        self.inventory = []
        self.last_refresh = 0
        self.lock = asyncio.Lock()
        self.token = os.environ["XIAOMI_CAMERA_SECRET"]
        self.allowed = set(json.loads(os.environ.get("XIAOMI_CAMERA_DIDS", "[]")))

    @web.middleware
    async def authorize(self, request, handler):
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not supplied:
            supplied = request.query.get("token", "")
        if not secrets.compare_digest(supplied, self.token):
            raise web.HTTPUnauthorized()
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception:
            LOGGER.exception("Camera operation failed")
            raise web.HTTPServiceUnavailable(text="Camera operation failed") from None

    async def refresh(self, force=False):
        async with self.lock:
            client = self.account.client if self.account.is_linked else None
            if client is not self.bound_client:
                if self.sessions:
                    await self.sessions.async_shutdown()
                self.bound_client = client
                self.registry = CameraRegistry(client, self.settings) if client else None
                self.sessions = (
                    SessionManager(
                        client,
                        resolver=lambda did: self.settings.resolved_for(
                            did, support="full", compat_ready=False
                        ),
                    )
                    if client
                    else None
                )
                self.last_refresh = 0
            if self.registry and (force or time.monotonic() - self.last_refresh > 60):
                descriptions = await self.registry.async_refresh()
                self.inventory = [d.as_dict() for d in descriptions if d.support == "full"]
                await self.sessions.async_prune({d["did"] for d in self.inventory})
                self.last_refresh = time.monotonic()

    async def health(self, request):
        return web.json_response({"linked": self.account.is_linked, "mode": "direct"})

    async def cameras(self, request):
        await self.refresh()
        if not self.registry:
            raise web.HTTPUnauthorized(text="Xiaomi account is not linked")
        cameras = self.inventory
        if self.allowed and request.query.get("all") != "1":
            cameras = [d for d in cameras if d["did"] in self.allowed]
        return web.json_response({"cameras": cameras})

    async def link_begin(self, request):
        return web.json_response({"url": await self.account.async_begin_link()})

    async def link_complete(self, request):
        payload = await request.json()
        # Finish active native sessions before exchanging the account client.
        if self.sessions:
            await self.sessions.async_shutdown()
        await self.account.async_complete_link(payload["code"], payload["state"])
        await self.refresh(force=True)
        return web.json_response({"linked": True})

    def session(self, did):
        if self.allowed and did not in self.allowed:
            raise web.HTTPNotFound()
        info = self.registry.get(did) if self.registry else None
        if info is None or self.sessions is None:
            raise web.HTTPNotFound()
        if self.registry.power_state(did) is False:
            raise web.HTTPConflict(text="Camera is off")
        return self.sessions.session_for(info)

    @staticmethod
    async def next_unit(consumer):
        unit_task = asyncio.create_task(consumer.queue.get())
        close_task = asyncio.create_task(consumer.closed.wait())
        try:
            done, _ = await asyncio.wait(
                (unit_task, close_task), timeout=20, return_when=asyncio.FIRST_COMPLETED
            )
            if close_task in done or not done:
                raise ConnectionResetError("Camera stream ended or stalled")
            return unit_task.result()
        finally:
            for task in (unit_task, close_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(unit_task, close_task, return_exceptions=True)

    async def stream(self, request):
        session = self.session(request.match_info["did"])
        # Subscribe before sending 200 so an offline camera gets a real error.
        async with session.subscribe() as consumer:
            muxer = StreamMuxer(session.codec, session.audio_codec)
            response = web.StreamResponse(headers={"Content-Type": "video/mp2t"})
            await response.prepare(request)
            try:
                while True:
                    unit = await self.next_unit(consumer)
                    if unit.kind is MediaKind.AUDIO and not muxer.has_audio:
                        break
                    chunk = muxer.write(unit)
                    if chunk:
                        await response.write(chunk)
            except (ConnectionResetError, BrokenPipeError):
                pass
            finally:
                with contextlib.suppress(Exception):
                    muxer.close()
                with contextlib.suppress(Exception):
                    await response.write_eof()
            return response

    async def snapshot(self, request):
        session = self.session(request.match_info["did"])
        async with asyncio.timeout(30), session.subscribe() as consumer:
            codec = "hevc" if str(session.codec) == "h265" else "h264"
            decoder = av.CodecContext.create(codec, "r")

            def decode(payload):
                for packet in decoder.parse(payload):
                    for frame in decoder.decode(packet):
                        buf = io.BytesIO()
                        frame.to_image().save(buf, format="JPEG", quality=85)
                        return buf.getvalue()
                return None

            while True:
                unit = await self.next_unit(consumer)
                if unit.kind is MediaKind.VIDEO:
                    picture = await asyncio.to_thread(decode, unit.payload)
                    if picture:
                        return web.Response(body=picture, content_type="image/jpeg")

    async def power(self, request):
        did = request.match_info["did"]
        if self.allowed and did not in self.allowed:
            raise web.HTTPNotFound()
        payload = await request.json()
        if not isinstance(payload.get("value"), bool):
            raise web.HTTPBadRequest()
        if self.registry is None or self.registry.get(did) is None:
            raise web.HTTPNotFound()
        await self.registry.async_set_power(did, payload["value"])
        self.last_refresh = 0
        return web.json_response({"ok": True})

    async def close(self):
        if self.sessions:
            await self.sessions.async_shutdown()
        await self.account.async_shutdown()


async def main():
    # An unexpected HA exit must not leave native sessions orphaned.
    parent = os.getppid()
    ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0)
    if os.getppid() != parent:
        return
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    (DATA / "cache").mkdir(exist_ok=True, mode=0o700)
    install_redaction()
    logging.basicConfig(level=logging.WARNING)
    worker = Worker()
    quality = os.environ.get("XIAOMI_CAMERA_QUALITY")
    if quality:
        worker.settings.set_defaults(quality=VideoQuality(quality))
        for did in worker.allowed:
            worker.settings.set_override(did, quality=VideoQuality(quality))
    app = web.Application(middlewares=[worker.authorize])
    app.add_routes(
        [
            web.get("/health", worker.health),
            web.get("/cameras", worker.cameras),
            web.post("/link/begin", worker.link_begin),
            web.post("/link/complete", worker.link_complete),
            web.get("/stream/{did}", worker.stream),
            web.get("/snapshot/{did}", worker.snapshot),
            web.post("/power/{did}", worker.power),
        ]
    )
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=3)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await worker.account.async_setup()
        await worker.refresh()
        await runner.setup()
        sock = socket.socket(fileno=int(os.environ["XIAOMI_CAMERA_FD"]))
        await web.SockSite(runner, sock).start()
        await stop.wait()
    finally:
        await runner.cleanup()
        await worker.close()


if __name__ == "__main__":
    asyncio.run(main())
