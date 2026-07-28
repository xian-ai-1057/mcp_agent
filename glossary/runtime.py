"""The process-wide glossary loader.

Tools reach the glossary through here so that no tool module owns a file path or
a caching policy. Tests swap the loader with `set_loader`.
"""

import os
import threading
from importlib.resources import files
from pathlib import Path

from glossary.loader import Glossary, GlossaryLoader

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = Path(str(files("data").joinpath("glossary.csv")))

_lock = threading.Lock()
_loader: GlossaryLoader | None = None


def default_csv_path() -> Path:
    """`GLOSSARY_CSV` if set, otherwise the bundled asset."""
    configured = os.environ.get("GLOSSARY_CSV")
    if not configured:
        return DEFAULT_CSV
    path = Path(configured)
    return path if path.is_absolute() else REPO_ROOT / path


def get_loader() -> GlossaryLoader:
    global _loader
    with _lock:
        if _loader is None:
            _loader = GlossaryLoader(default_csv_path())
        return _loader


def set_loader(loader: GlossaryLoader | None) -> None:
    """Install a loader (or reset to lazy default). For tests and embedding."""
    global _loader
    with _lock:
        _loader = loader


def get_glossary() -> Glossary:
    """Current glossary, reloaded first if the CSV changed on disk."""
    return get_loader().get()
