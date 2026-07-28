"""Secure MCP adapter for submitting documents to a RAG ingestion service.

This package intentionally contains only the integration contract.  Parsing,
chunking, embedding, indexing, and persistence remain the responsibility of the
configured RAG service.
"""

from mcp_servers.rag_upload.config import RagUploadSettings
from mcp_servers.rag_upload.tools import build_specs

__all__ = ["RagUploadSettings", "build_specs"]
