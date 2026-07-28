"""Tool layer: one tool per module, each carrying its own name, description and schema.

A tool takes a dict and returns a dict. It has no opinion about who called it,
what model is in play, or what happens to its result. See
`specs/002-mcp-tools/spec.md`.

Adding a tool is adding a file here — nothing in `server.py` or the system
prompt needs to change.
"""
