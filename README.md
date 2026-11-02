# Xiaomi Camera Direct for Home Assistant

[简体中文](README.zh-CN.md)

A Home Assistant custom integration that manages Xiaomi camera streaming inside
the existing HA container. Camera video, account authorization, lens power and
source quality are configured in HA.

```text
Xiaomi camera → HA-managed native worker → MPEG-TS → HA stream → viewer
```

No separate camera container, standalone go2rtc, RTSP server or camera web UI is
required at runtime. The worker is necessary because the installed Xiaomi
library requires glibc while the standard HA container uses musl. HA owns the
worker lifecycle and restarts it after a crash on the next request or poll.

## Current scope

- Source version **0.1.0**; developed against HA Container 2026.10.4 on Linux x86_64.
- An existing camera-bridge installation is required for the initial migration.
  This repository does **not** include or download Xiaomi native binaries.
- The original video is remuxed, not re-encoded. Viewers must support its codec;
  cameras sending H.265 require an H.265-capable viewer.
- `chuangmi.camera.061a01` was tested through authenticated HA HLS and snapshots.
  Other vendor-supported models can be selected, but are not all hardware-tested.
- Main video channel only. No legacy go2rtc compatibility mode, talkback, PTZ or
  recording-history playback. Selecting a camera does not turn its lens on.

This is an initial source release, not a one-click HACS package or a general
replacement for Xiaomi Home device controls.

## Migrate an existing installation

On the Docker host that already runs Home Assistant and a working
[ha-xiaomi-camera bridge](https://github.com/willjayyyy/ha-xiaomi-camera):

```sh
git clone https://github.com/bkcarlos/ha-xiaomi-camera-direct.git
cd ha-xiaomi-camera-direct
python3 scripts/install.py --ha homeassistant --source xiaomi-camera-bridge
```

Replace both container names with your existing names. The installer backs up
HA registry/config files, copies the locally installed runtime and account
authorization into HA's persistent config directory, and validates the native
library. It does not start/stop other services or change lens power.

Restart HA, add **Xiaomi Camera Direct** under Settings → Devices & services,
and select cameras. Existing authorization is reused; reauthentication uses
the HA config flow. After verifying the new entities, the old camera service
can be stopped and removed. Keep its data/configuration if rollback is needed.

For subsequent source updates, the old container is no longer required:

```sh
python3 scripts/install.py --ha homeassistant
```

Restart HA after updating the integration code.

## Storage

- `/config/custom_components/xiaomi_camera_direct/`: integration source.
- `/config/.xiaomi_camera/runtime/`: isolated glibc, Python and camera runtime.
- `/config/.xiaomi_camera/data/`: private account state, cache and preferences.

These paths survive ordinary HA container updates. Include them in backups;
such backups contain account authorization. Do not commit them to Git.

Runtime modules are reused from the local `ha-xiaomi-camera` installation.
Third-party libraries retain their respective licenses. No vendor binaries,
personal deployment records, credentials or camera images are distributed here.
The initial migration records runtime image provenance in `runtime/provenance.json`.

## Development checks

```sh
python3 -m venv .venv
.venv/bin/pip install aiohttp pytest pytest-asyncio ruff
.venv/bin/pytest -q
.venv/bin/ruff check custom_components tests scripts
```

Tests cover lifecycle recovery, shutdown cleanup, authentication and interpreter
environment isolation. Real camera compatibility still requires hardware tests.

[栖间 / Qijian](https://github.com/bkcarlos/qijian) is a separate HomeKit client;
it does not consume this integration's private worker endpoint directly.
