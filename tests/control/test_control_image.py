"""The control plane's packaging (docker/): Chrome without its own sandbox when the config says so (the image runs as a
non-root user), and the files Dokploy and systemd read, checked for what deploying them relies on.
Building and running the image is by hand."""
import os
import stat
from types import SimpleNamespace

import yaml

from valet.control.browser.service import BrowserService
from valet.shared.config import ROOT, ControlConfig

DOCKER = ROOT / "docker"


def test_chrome_no_sandbox_setting(tmp_path):
    base = {"VALET_DATA": str(tmp_path)}
    assert ControlConfig.from_env(base).chrome_no_sandbox is None
    assert ControlConfig.from_env({**base, "VALET_CHROME_NO_SANDBOX": "1"}).chrome_no_sandbox is True
    assert ControlConfig.from_env({**base, "VALET_CHROME_NO_SANDBOX": "0"}).chrome_no_sandbox is False
    for value, expected in ((None, os.geteuid() == 0), (True, True), (False, False)):
        cfg = ControlConfig(data=tmp_path, definitions=tmp_path / "d", chrome_no_sandbox=value)
        assert BrowserService(SimpleNamespace(cfg=cfg)).backend.no_sandbox is expected


def test_the_image_runs_headed_chrome_as_a_user():
    text = (DOCKER / "control" / "Dockerfile").read_text()
    for setting in ("VALET_DATA=/data", "VALET_HOST=0.0.0.0", "VALET_PORT=8780", "DISPLAY=:99", "VALET_HEADLESS=0",
                    "VALET_CHROME_NO_SANDBOX=1", "PLAYWRIGHT_BROWSERS_PATH="):
        assert setting in text, setting
    assert "USER valet" in text and "playwright install --with-deps chromium" in text
    assert 'ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]' in text and "/api/me" in text
    assert "valet/agent-base" not in text                         # the agents' image isn't built or tagged here
    entry = DOCKER / "control" / "entrypoint.sh"
    assert entry.stat().st_mode & stat.S_IXUSR and 'exec "$@"' in entry.read_text()
    ignored = (ROOT / ".dockerignore").read_text().split()
    assert {"data", ".venv", ".git"} <= set(ignored)


def test_compose_for_dokploy():
    c = yaml.safe_load((DOCKER / "compose.yml").read_text())
    svc = c["services"]["valet-control"]
    assert svc["image"] == "valet/control:latest" and svc["pull_policy"] == "never"
    assert svc["ports"][0] == "127.0.0.1:8780:8780"                   # never on every interface
    assert all(not p.startswith(("8780", "0.0.0.0")) for p in svc["ports"])
    env = dict(e.split("=", 1) for e in svc["environment"])
    assert env["VALET_MASTER_KEY"] == "${VALET_MASTER_KEY}" and env["VALET_PUBLIC_URL"] == "${VALET_PUBLIC_URL}"
    assert env["VALET_GATEWAY_URL"].endswith(":8780}") and "VALET_APNS_KEY_ID" in env
    assert c["name"] == "valet"
    assert svc["volumes"] == ["valet-data:/data"] and c["volumes"]["valet-data"]["external"] is True


def test_runner_unit():
    unit = (DOCKER / "valet-runner.service").read_text()
    assert "EnvironmentFile=/home/rafa/.config/valet/runner.env" in unit and "valet-runner" in unit
    assert "Restart=always" in unit and "WantedBy=multi-user.target" in unit
    example = dict(line.split("=", 1) for line in (DOCKER / "runner.env.example").read_text().splitlines()
                   if line and not line.startswith("#"))
    assert example["VALET_CONTROL_URL"] == "ws://127.0.0.1:8780/ws/runner"
    assert example["VALET_RUNNER_TOKEN"] == "" and example["CLAUDE_CODE_OAUTH_TOKEN"] == ""     # no value committed
