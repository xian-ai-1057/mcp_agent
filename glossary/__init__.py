"""Knowledge layer: load the glossary, find terms, judge translations.

This package does not know that MCP, a gateway or a prompt exist. See
`specs/001-glossary-core/spec.md`.
"""

from glossary.loader import (
    Glossary,
    GlossaryConflict,
    GlossaryConflictError,
    GlossaryError,
    GlossaryLoader,
    load_glossary,
)
from glossary.matcher import hit_rate, match_terms
from glossary.runtime import get_glossary, get_loader, set_loader
from glossary.scanner import scan

__all__ = [
    "Glossary",
    "GlossaryConflict",
    "GlossaryConflictError",
    "GlossaryError",
    "GlossaryLoader",
    "load_glossary",
    "scan",
    "match_terms",
    "hit_rate",
    "get_glossary",
    "get_loader",
    "set_loader",
]
