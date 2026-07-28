"""Interface contracts shared by every layer.

Frozen in Phase 0. `glossary/`, `tools/` and `agent/` all import from here and
none of them import from each other's internals, so this package is the only
place where a change breaks more than one layer at a time.
"""

from contracts.agent import Initiator, RunMetrics, RunResult, StopReason, ToolCallRecord
from contracts.glossary import GlossaryEntry, TermMatch
from contracts.tools import LookupResult, TermVerdict, Verdict, VerifyResult

__all__ = [
    "GlossaryEntry",
    "TermMatch",
    "LookupResult",
    "TermVerdict",
    "Verdict",
    "VerifyResult",
    "Initiator",
    "RunMetrics",
    "RunResult",
    "StopReason",
    "ToolCallRecord",
]
