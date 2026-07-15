import argparse
import os

from mcp.server.fastmcp import FastMCP

from scorekeeper.config import get_settings

settings = get_settings()
mcp = FastMCP(
    "Scorekeeper",
    instructions="Record scores and retrieve the latest results.",
    host=settings.mcp_host,
    port=settings.mcp_port,
    stateless_http=True,
    json_response=True,
)

def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Scorekeeper MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "http", "streamable-http"),
        default=os.getenv("MCP_TRANSPORT", settings.mcp_transport),
    )
    args = parser.parse_args()
    # Schema is managed by Alembic; run `alembic upgrade head` before starting.
    transport = "streamable-http" if args.transport == "http" else args.transport
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
