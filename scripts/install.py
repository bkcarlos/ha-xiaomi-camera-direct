"""One-time migration on the Docker host. Runtime operation needs only HA.

Uses the already installed, locally licensed camera runtime; downloads nothing.
Does not restart HA or stop the old camera service until the new integration
has been verified by the operator.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ha", default="homeassistant")
    parser.add_argument("--source", default="xiaomi-camera-bridge")
    args = parser.parse_args()
    target = json.loads(docker("inspect", args.ha))[0]
    image = json.loads(docker("image", "inspect", target["Image"]))[0]
    if image.get("Architecture") != "amd64":
        raise SystemExit("This migration currently supports Linux x86_64 only")
    mount = next(m for m in target["Mounts"] if m["Destination"] == "/config")
    config = Path(mount["Source"])
    destination = config / "custom_components/xiaomi_camera_direct"
    private = config / ".xiaomi_camera"
    private.mkdir(mode=0o700, exist_ok=True)
    private.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = private / ("backup-" + stamp + ".tar.gz")
    with tarfile.open(backup, "w:gz") as archive:
        for path in (
            destination,
            config / ".storage/core.config_entries",
            config / ".storage/core.entity_registry",
            config / ".storage/core.device_registry",
        ):
            if path.exists():
                archive.add(path, arcname=str(path.relative_to(config)))
    backup.chmod(0o600)
    runtime = private / "runtime"
    runtime.mkdir(exist_ok=True)
    artifacts = (
        ("/usr/bin/python3.11", "usr/bin/python3.11"),
        ("/usr/lib/python3.11", "usr/lib/python3.11"),
        ("/usr/lib/x86_64-linux-gnu", "usr/lib/x86_64-linux-gnu"),
        ("/opt/venv/lib/python3.11/site-packages", "site-packages"),
        ("/app/bridge", "bridge"),
    )
    data = private / "data"
    data.mkdir(exist_ok=True, mode=0o700)
    needs_source = any(not (runtime / relative).exists() for _, relative in artifacts)
    needs_source |= any(
        not (data / name).exists() for name in ("credentials.json", "settings.json")
    )
    source = json.loads(docker("inspect", args.source))[0] if needs_source else None
    for original, relative in artifacts:
        path = runtime / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            docker("cp", args.source + ":" + original, str(path))
    for name in ("credentials.json", "settings.json"):
        path = data / name
        if not path.exists():
            docker("cp", args.source + ":/data/" + name, str(path))
        path.chmod(0o600)
    component = Path(__file__).resolve().parents[1] / "custom_components/xiaomi_camera_direct"
    shutil.copytree(
        component,
        destination,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    # Keep provenance without publishing or bundling vendor binaries in source.
    if source is not None:
        (runtime / "provenance.json").write_text(
            json.dumps(
                {
                    "source_image_id": source["Image"],
                    "source_container": args.source,
                    "prepared_at_utc": stamp,
                },
                indent=2,
            )
            + "\n"
        )
    code = (
        "from pathlib import Path; "
        "from custom_components.xiaomi_camera_direct.runtime import runtime_command; "
        "import subprocess; "
        "c,e=runtime_command(Path('/config/.xiaomi_camera/runtime'),Path('/dev/null')); "
        "c[-1:]=['-c','from miot.camera import _load_dynamic_lib; "
        "print(_load_dynamic_lib().miot_camera_version().decode())']; "
        "subprocess.run(c,env=e,check=True)"
    )
    print(docker("exec", "-e", "PYTHONPATH=/config", args.ha, "python", "-c", code).strip())
    print("Installed. Restart Home Assistant, then add Xiaomi Camera Direct.")
    print("No other service was started or stopped. Backup:", backup)


if __name__ == "__main__":
    os.umask(0o077)
    main()
