"""The local add-on is a complete build context with its own baked version."""
from pathlib import Path
import shlex
import subprocess

import yaml


def test_local_addon_build_context_and_version(tmp_path):
    source = Path(__file__).resolve().parents[1]
    output = tmp_path / "echomuse-dev"
    subprocess.run(["sh", str(source / "tools/make_dev_addon.sh"), str(output)],
        check=True, capture_output=True, text=True)
    config = yaml.safe_load((output / "config.yaml").read_text())
    assert config["name"] == "EchoMuse (dev)"
    assert config["slug"] == "controller-dev"
    assert "image" not in config
    assert config["ingress"] and config["host_network"]
    assert config["options"]["esphome_port_base"] == 16201
    dockerfile = (output / "Dockerfile").read_text()
    assert "ARG EM_CONTROLLER_VERSION=" + config["version"] in dockerfile
    for line in dockerfile.splitlines():
        if not line.startswith("COPY ") or "--from=" in line:
            continue
        for name in shlex.split(line)[1:-1]:
            assert (output / name).exists(), f"Missing Docker build input: {name}"
    assert (output / "em_schedule.py").read_bytes() == (source / "em_schedule.py").read_bytes()
    assert (output / "static/dashboard.jsx").read_bytes() == (source / "static/dashboard.jsx").read_bytes()
    assert not (output / ".env").exists()
    assert not (output / "tls").exists()
    assert not list(output.rglob("*.db"))
    assert not list(output.rglob("*.pyc"))
