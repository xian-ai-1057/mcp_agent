"""Tests for `.env` loading (`agent/config.py`).

This exists because the README told users to fill in `.env` while nothing ever
read it. A configuration promise that isn't tested is a promise that quietly
stops being true — so the promise is tested here.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent.config import DEFAULT_ENV_FILE, describe_env_source, load_env_file

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def restore_environ():
    """Snapshot and restore `os.environ` around every test in this module.

    These tests deliberately mutate the real environment — that is the thing
    under test — and `monkeypatch` only undoes what `monkeypatch` did, not what
    `load_env_file` did. Without this, a leaked `GLOSSARY_CSV` follows the
    process into later tests and their MCP subprocesses.
    """
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


@pytest.fixture
def env_file(tmp_path):
    def _write(body: str) -> Path:
        path = tmp_path / ".env"
        path.write_text(body, encoding="utf-8")
        return path

    return _write


class TestLoadEnvFile:
    def test_values_land_in_the_environment(self, env_file, monkeypatch):
        monkeypatch.delenv("GATEWAY_BASE_URL", raising=False)
        path = env_file("GATEWAY_BASE_URL=https://gw.example/v1\nGATEWAY_MODEL=fedgpt-medium\n")

        assert load_env_file(path) == path
        assert os.environ["GATEWAY_BASE_URL"] == "https://gw.example/v1"
        assert os.environ["GATEWAY_MODEL"] == "fedgpt-medium"

    def test_comments_and_blank_lines_are_ignored(self, env_file, monkeypatch):
        monkeypatch.delenv("GATEWAY_MODEL", raising=False)
        load_env_file(env_file("# a comment\n\nGATEWAY_MODEL=m1\n\n# trailing\n"))
        assert os.environ["GATEWAY_MODEL"] == "m1"

    def test_an_exported_variable_wins_over_the_file(self, env_file, monkeypatch):
        """Explicit beats ambient.

        `GATEWAY_MODEL=other python -m agent.cli ...` must do what it says even
        when `.env` names something else.
        """
        monkeypatch.setenv("GATEWAY_MODEL", "exported-wins")
        load_env_file(env_file("GATEWAY_MODEL=from-file\n"))
        assert os.environ["GATEWAY_MODEL"] == "exported-wins"

    def test_override_flag_reverses_that(self, env_file, monkeypatch):
        monkeypatch.setenv("GATEWAY_MODEL", "exported")
        load_env_file(env_file("GATEWAY_MODEL=from-file\n"), override=True)
        assert os.environ["GATEWAY_MODEL"] == "from-file"

    def test_an_exported_but_empty_variable_does_not_win(self, env_file, monkeypatch):
        """Empty means unset, consistently with `HTTPGateway.configured()`.

        A shell that exports `GATEWAY_BASE_URL=` would otherwise shadow a
        perfectly good `.env` and reproduce the original confusion — set in the
        file, yet the program insists it is not set.
        """
        monkeypatch.setenv("GATEWAY_BASE_URL", "")
        load_env_file(env_file("GATEWAY_BASE_URL=https://gw.example/v1\n"))
        assert os.environ["GATEWAY_BASE_URL"] == "https://gw.example/v1"

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        """Running purely off exported variables is a legitimate setup."""
        assert load_env_file(tmp_path / "nope.env") is None

    def test_a_directory_is_not_an_error_either(self, tmp_path):
        assert load_env_file(tmp_path) is None

    def test_quoted_values_are_unquoted(self, env_file, monkeypatch):
        monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
        load_env_file(env_file('GATEWAY_API_KEY="secret with spaces"\n'))
        assert os.environ["GATEWAY_API_KEY"] == "secret with spaces"

    def test_the_default_path_is_the_repository_root(self):
        assert DEFAULT_ENV_FILE == REPO_ROOT / ".env"


class TestDescribeEnvSource:
    def test_names_the_expected_path_when_no_file_was_found(self):
        message = describe_env_source(None)
        assert ".env" in message
        assert str(DEFAULT_ENV_FILE) in message

    def test_says_the_file_was_read_when_it_was(self, tmp_path):
        path = tmp_path / ".env"
        assert str(path) in describe_env_source(path)


class TestImportingDoesNotTouchTheEnvironment:
    def test_importing_agent_modules_leaves_os_environ_alone(self):
        """The constraint that keeps the test suite hermetic.

        If any module loaded `.env` at import time, a developer's local gateway
        config would leak into every test run — and the `requires_gateway` tests
        would stop skipping and start making real network calls.
        """
        script = (
            "import os, json, sys; "
            "before = dict(os.environ); "
            "import agent.cli, agent.gateway, agent.loop, agent.config, evals.run_eval; "
            "after = dict(os.environ); "
            "print(json.dumps(sorted(set(after) - set(before))))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip().endswith("[]"), (
            f"importing agent modules added env vars: {result.stdout.strip()}"
        )


class TestCliReportsWhyConfigurationIsMissing:
    def _run_cli(self, args, env):
        return subprocess.run(
            [sys.executable, "-m", "agent.cli", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env={**os.environ, **env, "GATEWAY_BASE_URL": ""},
        )

    def test_missing_file_is_named_in_the_error(self, tmp_path):
        result = self._run_cli(["--env-file", str(tmp_path / "absent.env"), "hi"], {})
        assert result.returncode == 2
        assert "找不到" in result.stderr
        assert ".env" in result.stderr

    def test_a_file_without_the_key_says_so(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("GATEWAY_MODEL=m1\n", encoding="utf-8")

        result = self._run_cli(["--env-file", str(path), "hi"], {})
        assert result.returncode == 2
        assert "已載入" in result.stderr
        assert str(path) in result.stderr

    def test_a_complete_file_gets_past_configuration(self, tmp_path):
        """The actual bug: a filled-in `.env` must reach `HTTPGateway`.

        The run then fails at the network, not at configuration — that is the
        distinction that was broken.
        """
        path = tmp_path / ".env"
        path.write_text(
            "GATEWAY_BASE_URL=http://127.0.0.1:9/v1\nGATEWAY_API_KEY=k\nGATEWAY_MODEL=m\n",
            encoding="utf-8",
        )

        result = self._run_cli(["--env-file", str(path), "--max-turns", "1", "hi"], {})
        assert "GATEWAY_BASE_URL is not set" not in result.stderr
        assert "gateway error" in result.stderr or "gateway request failed" in result.stderr


class TestEnvFileReachesTheToolSubprocess:
    async def test_glossary_csv_from_the_env_file_is_inherited_by_the_server(
        self, tmp_path, monkeypatch
    ):
        """The MCP server is a child process; it inherits the loaded environment."""
        from tests.mcp_session import mcp_session

        csv = tmp_path / "custom.csv"
        csv.write_text(
            "zh,en,aliases,category\n環境變數術語,env var term,,測試\n", encoding="utf-8"
        )
        env_file = tmp_path / ".env"
        env_file.write_text(f"GLOSSARY_CSV={csv}\n", encoding="utf-8")

        monkeypatch.delenv("GLOSSARY_CSV", raising=False)
        assert load_env_file(env_file) == env_file

        import json

        async with mcp_session() as server:
            payload = json.loads(await server.call("lookup_terms", {"text": "環境變數術語"}))
        assert payload["matches"][0]["en"] == "env var term"
