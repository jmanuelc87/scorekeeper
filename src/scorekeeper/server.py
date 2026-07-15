import argparse
import os

from mcp.server.fastmcp import FastMCP

from scorekeeper.config import get_settings
from scorekeeper.database import add_score, create_schema, get_scores

settings = get_settings()
mcp = FastMCP(
    "Scorekeeper",
    instructions="Record scores and retrieve the latest results.",
    host=settings.mcp_host,
    port=settings.mcp_port,
    stateless_http=True,
    json_response=True,
)


@mcp.tool()
def record_score(player: str, points: int) -> dict[str, int | str]:
    """Record a player's score and return the saved result."""
    return add_score(player=player.strip(), points=points)


@mcp.tool()
def list_scores(limit: int = 50) -> list[dict[str, int | str]]:
    """Return the most recent scores, newest first."""
    return get_scores(max(1, min(limit, 200)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Scorekeeper MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "http", "streamable-http"),
        default=os.getenv("MCP_TRANSPORT", settings.mcp_transport),
    )
    args = parser.parse_args()
    create_schema()
    transport = "streamable-http" if args.transport == "http" else args.transport
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()

