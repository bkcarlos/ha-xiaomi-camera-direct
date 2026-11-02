"""Lifecycle of the private native worker. No Docker socket or daemon needed."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import signal
import socket
from pathlib import Path

import aiohttp

from .const import CONF_CAMERAS, CONF_QUALITY, RUNTIME_DIR

LOGGER = logging.getLogger(__name__)


class RuntimeUnavailable(Exception):
    """The native runtime is missing, incompatible or failed to start."""


class AccountRequired(Exception):
    """The saved account needs authentication."""


def runtime_command(root: Path, worker: Path) -> tuple[list[str], dict[str, str]]:
    """Keep glibc isolated from HA's musl interpreter and its environment."""
    libraries = root / "usr/lib/x86_64-linux-gnu"
    loader = libraries / "ld-linux-x86-64.so.2"
    python = root / "usr/bin/python3.11"
    if not loader.is_file() or not python.is_file():
        raise RuntimeUnavailable("Install the private Linux x86_64 runtime first")
    env = os.environ.copy()
    # HA 3.14 sets PYTHONMALLOC=mimalloc, which terminates Python 3.11 before
    # it can execute any code. Do not inherit interpreter-specific flags.
    for key in tuple(env):
        if key.startswith("PYTHON") or key in ("LD_PRELOAD", "LD_LIBRARY_PATH"):
            env.pop(key)
    env.update(
        {
            "PYTHONHOME": str(root / "usr"),
            "PYTHONPATH": os.pathsep.join((str(root / "site-packages"), str(root))),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
            "SSL_CERT_DIR": "/etc/ssl/certs",
        }
    )
    return [str(loader), "--library-path", str(libraries), str(python), str(worker)], env


class CameraRuntime:
    def __init__(self, hass, session: aiohttp.ClientSession, options=None):
        self.hass = hass
        self.session = session
        self.options = options or {}
        self.directory = Path(hass.config.path(RUNTIME_DIR))
        self.process = None
        self.reader = None
        self.port = None
        self.token = secrets.token_urlsafe(32)
        self.lock = asyncio.Lock()
        self.closed = False
        self.generation = 0

    def url(self, path: str, *, stream=False) -> str:
        base = f"http://127.0.0.1:{self.port}{path}"
        return base + "?token=" + self.token if stream else base

    async def start(self):
        async with self.lock:
            if self.closed:
                raise RuntimeUnavailable("Camera integration is unloaded")
            if self.process is not None and self.process.returncode is None:
                return
            if self.reader:
                await self.reader
                self.reader = None
            root = self.directory / "runtime"
            worker = Path(__file__).with_name("worker.py")
            command, env = await self.hass.async_add_executor_job(runtime_command, root, worker)
            data = self.directory / "data"
            await self.hass.async_add_executor_job(
                lambda: data.mkdir(parents=True, exist_ok=True, mode=0o700)
            )
            sock = socket.socket()
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                # Reuse the same address after a crash so HA's stream reconnects.
                sock.bind(("127.0.0.1", self.port or 0))
                sock.listen(128)
                self.port = sock.getsockname()[1]
                env.update(
                    {
                        "XIAOMI_CAMERA_FD": str(sock.fileno()),
                        "XIAOMI_CAMERA_DATA": str(data),
                        "XIAOMI_CAMERA_SECRET": self.token,
                        "XIAOMI_CAMERA_DIDS": json.dumps(self.options.get(CONF_CAMERAS, [])),
                        "XIAOMI_CAMERA_QUALITY": self.options.get(CONF_QUALITY, "low"),
                    }
                )
                self.process = await asyncio.create_subprocess_exec(
                    *command,
                    env=env,
                    pass_fds=(sock.fileno(),),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
            except (OSError, ValueError) as err:
                raise RuntimeUnavailable("Could not start the camera runtime") from err
            finally:
                sock.close()
            self.reader = asyncio.create_task(self._read_log(self.process))
            try:
                async with asyncio.timeout(75):
                    while self.process.returncode is None:
                        try:
                            async with self.session.get(
                                self.url("/health"),
                                headers=self.headers,
                                timeout=aiohttp.ClientTimeout(total=2),
                            ) as response:
                                if response.status == 200:
                                    self.generation += 1
                                    return
                        except (aiohttp.ClientError, TimeoutError):
                            pass
                        await asyncio.sleep(0.4)
                raise RuntimeUnavailable("Camera runtime exited during startup")
            except BaseException:
                await self._terminate()
                raise

    @property
    def headers(self):
        return {"Authorization": "Bearer " + self.token}

    async def request(self, path, *, method="GET", data=None, binary=False):
        await self.start()
        try:
            async with self.session.request(
                method,
                self.url(path),
                json=data,
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=45),
            ) as response:
                if response.status == 401:
                    raise AccountRequired("Sign in to Xiaomi again")
                response.raise_for_status()
                return await response.read() if binary else await response.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            # Do not propagate authenticated stream URLs or cloud payloads.
            raise RuntimeUnavailable("Camera worker request failed") from err

    async def _read_log(self, process):
        # Vendor SDK logs can contain credentials. Consume the pipe to avoid
        # blocking the worker; expose exit status, never arbitrary vendor output.
        tail = b""
        while chunk := await process.stdout.read(8192):
            tail = (tail + chunk)[-16384:]
        await process.wait()
        if not self.closed and process.returncode:
            # A bounded private diagnostic, never included in HA logs or UI.
            path = self.directory / "data" / "worker-error.log"
            output = tail.replace(self.token.encode(), b"[redacted]")

            def save():
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(output)

            await self.hass.async_add_executor_job(save)
            LOGGER.warning("Camera worker exited (%s); HA will restart it", process.returncode)

    async def _terminate(self):
        process = self.process
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=12)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        if self.reader:
            await self.reader
            self.reader = None
        self.process = None

    async def stop(self):
        async with self.lock:
            self.closed = True
            await self._terminate()
