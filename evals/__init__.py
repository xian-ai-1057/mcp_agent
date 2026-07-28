"""End-to-end evaluation harness.

Everything here needs a *model*. The deterministic doubles in `agent/testing.py`
can prove the loop works; only a real gateway can answer whether the model picks
the right tool and follows the glossary. When no gateway is configured these
suites are skipped, never simulated — a faked routing score is a number that
looks like evidence and isn't.
"""
