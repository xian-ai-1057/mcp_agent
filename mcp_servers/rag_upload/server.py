"""stdio entry point for the isolated RAG upload MCP server."""

from mcp_servers.common import run_server
from mcp_servers.rag_upload.config import RagUploadSettings
from mcp_servers.rag_upload.tools import build_specs

SERVER_NAME = "rag-upload"
SERVER_VERSION = "0.1.0"


def main() -> None:
    settings = RagUploadSettings.from_env()
    run_server(build_specs(settings), SERVER_NAME, SERVER_VERSION)


if __name__ == "__main__":
    main()
