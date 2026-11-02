"""Lifecycle and credential isolation; does not mock the Xiaomi SDK."""

import asyncio
import importlib.util
import os
import signal
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import aiohttp
import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components/xiaomi_camera_direct"
package = ModuleType("camera_runtime_test")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location("camera_runtime_test.runtime", ROOT / "runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class Hass:
    def __init__(self, root):
        self.config = SimpleNamespace(path=lambda *args: str(root.joinpath(*args)))

    async def async_add_executor_job(self, fn, *args):
        return await asyncio.to_thread(fn, *args)


def test_runtime_isolated_from_host_environment(tmp_path, monkeypatch):
    libraries = tmp_path / "usr/lib/x86_64-linux-gnu"
    libraries.mkdir(parents=True)
    (libraries / "ld-linux-x86-64.so.2").touch()
    (tmp_path / "usr/bin").mkdir()
    (tmp_path / "usr/bin/python3.11").touch()
    monkeypatch.setenv("LD_PRELOAD", "/bad.so")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/musl")
    monkeypatch.setenv("PYTHONMALLOC", "mimalloc")
    command, env = runtime.runtime_command(tmp_path, tmp_path / "worker.py")
    assert "LD_PRELOAD" not in env
    assert "LD_LIBRARY_PATH" not in env
    assert "PYTHONMALLOC" not in env
    assert command[0] == str(libraries / "ld-linux-x86-64.so.2")
    assert env["PYTHONHOME"] == str(tmp_path / "usr")


def test_missing_runtime_is_actionable(tmp_path):
    with pytest.raises(runtime.RuntimeUnavailable):
        runtime.runtime_command(tmp_path, tmp_path / "worker.py")


FAKE_WORKER = """
import asyncio, os, socket
from aiohttp import web
async def health(request):
    if request.headers.get("Authorization") != "Bearer " + os.environ["XIAOMI_CAMERA_SECRET"]:
        raise web.HTTPUnauthorized()
    return web.json_response({"linked": True})
app=web.Application()
app.router.add_get('/health',health)
web.run_app(app, sock=socket.socket(fileno=int(os.environ['XIAOMI_CAMERA_FD'])), print=None, access_log=None)
"""


@pytest.mark.asyncio
async def test_restart_after_crash_reuses_address_and_unload_stops_process(tmp_path, monkeypatch):
    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER)
    monkeypatch.setattr(
        runtime,
        "runtime_command",
        lambda root, worker: ([sys.executable, str(script)], os.environ.copy()),
    )
    async with aiohttp.ClientSession() as session:
        managed = runtime.CameraRuntime(Hass(tmp_path), session)
        await managed.start()
        first_pid = managed.process.pid
        first_url = managed.url("/stream/test", stream=True)
        assert (await managed.request("/health"))["linked"]
        async with session.get(managed.url("/health")) as response:
            assert response.status == 401
        os.kill(first_pid, signal.SIGKILL)
        await managed.process.wait()
        assert (await managed.request("/health"))["linked"]
        assert managed.process.pid != first_pid
        assert managed.url("/stream/test", stream=True) == first_url
        process = managed.process
        await managed.stop()
        assert process.returncode is not None
        with pytest.raises(runtime.RuntimeUnavailable):
            await managed.start()


@pytest.mark.asyncio
async def test_start_failure_does_not_leave_a_process(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runtime,
        "runtime_command",
        lambda root, worker: ([sys.executable, "-c", "raise SystemExit(2)"], os.environ.copy()),
    )
    async with aiohttp.ClientSession() as session:
        managed = runtime.CameraRuntime(Hass(tmp_path), session)
        with pytest.raises(runtime.RuntimeUnavailable):
            await managed.start()
        assert managed.process is None
        await managed.stop()
