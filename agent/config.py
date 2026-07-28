"""Process bootstrap: load `.env` into the environment.

**Call this from entry points only** — `agent/cli.py` and `evals/run_eval.py`.
Never at module import time. Importing a module must not mutate `os.environ`, or
every test that sets a variable with `monkeypatch` would be silently fighting a
developer's local `.env`, and the test suite would stop being hermetic.

That constraint is also why `requires_gateway` tests still skip when a `.env`
exists: nothing under `tests/` calls `load_env_file`, so a local gateway config
never leaks into a test run.
"""

import logging
import os
from pathlib import Path

from dotenv import dotenv_values

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = REPO_ROOT / ".env"


def load_env_file(path: str | Path | None = None, override: bool = False) -> Path | None:
    """Load `KEY=value` pairs from an env file into `os.environ`.

    Returns the path that was read, or `None` if there was no file to read —
    running purely off exported variables is a legitimate setup, not an error.

    `override=False` means an already-exported variable wins over the file.
    Explicit beats ambient: `GATEWAY_MODEL=other python -m agent.cli ...` must
    do what it says even when `.env` names a different model.

    An exported-but-**empty** variable does not win, because the rest of the
    application already treats empty as unset — `HTTPGateway.present()` is
    `bool(os.environ.get(...))`. A shell that exports `GATEWAY_BASE_URL=` would
    otherwise shadow a perfectly good `.env` and produce exactly the "it is set
    but the program says it isn't" confusion this module exists to remove.
    """
    target = Path(path) if path is not None else DEFAULT_ENV_FILE
    if not target.is_file():
        logger.debug("no env file at %s", target)
        return None

    applied = 0
    for key, value in dotenv_values(target).items():
        if value is None:
            continue
        if os.environ.get(key) and not override:
            continue
        os.environ[key] = value
        applied += 1

    logger.info("loaded %d variable(s) from %s", applied, target)
    return target


def describe_env_source(path: Path | None) -> str:
    """One clause explaining where configuration did or didn't come from.

    Exists so that a missing `GATEWAY_BASE_URL` reports *why* it is missing.
    The failure this replaces — "not set" while a filled-in `.env` sat right
    there — gave the user no way to tell that the file was never read at all.
    """
    if path is None:
        return f"找不到 {DEFAULT_ENV_FILE.name}（預期路徑：{DEFAULT_ENV_FILE}）"
    return f"已載入 {path}，但其中沒有這個變數"
