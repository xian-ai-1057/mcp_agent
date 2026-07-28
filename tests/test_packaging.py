"""Build/install smoke test for runtime assets and new capability packages."""

import os
import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_wheel_contains_assets_and_runs_from_an_install(tmp_path):
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            ".",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_dir),
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(wheel_dir.glob("mcp_agent-*.whl"))

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert "data/glossary.csv" in names
    assert "fixtures/routing_cases.json" in names
    assert "mcp_servers/rag_upload/server.py" in names
    assert "capabilities/translation/policy.py" in names
    assert "agent/web.py" in names
    assert "agent/static/index.html" in names
    assert "agent/static/flow.html" in names
    assert "contracts/api.py" in names

    install_dir = tmp_path / "installed"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(install_dir),
            str(wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    env = {**os.environ, "PYTHONPATH": str(install_dir)}
    smoke = subprocess.run(
        [
            sys.executable,
            "-c",
            "from glossary.runtime import get_glossary; "
            "from mcp_servers.rag_upload.server import SERVER_NAME; "
            "print(len(get_glossary()), SERVER_NAME)",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    count, server_name = smoke.stdout.strip().split()
    assert int(count) > 0
    assert server_name == "rag-upload"
