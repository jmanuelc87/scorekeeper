import argparse
import os
import sys

import structlog
from mcp.server.fastmcp import FastMCP

from scorekeeper import evaluation
from scorekeeper.config import get_settings
from scorekeeper.logging_config import configure_logging

settings = get_settings()
logger = structlog.get_logger("scorekeeper.server")
mcp = FastMCP(
    "Scorekeeper",
    instructions="Record scores and retrieve the latest results.",
    host=settings.mcp_host,
    port=settings.mcp_port,
    stateless_http=True,
    json_response=True,
)


@mcp.tool()
async def retrieve(
    run_id: str | None = None,
    platform: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    granularity: str = "scenario_results",
) -> list[dict]:
    """Recupera los detalles completos de las evaluaciones (benchmark runs).

    Todos los filtros son opcionales y se combinan con AND:

    * ``run_id`` — limita a una sola evaluación. Un id desconocido/ inválido
      devuelve una lista vacía.
    * ``platform`` — coincidencia exacta (sensible a mayúsculas), p. ej.
      ``"claude"``, ``"copilot"`` o ``"gemini"``.
    * ``start_date`` / ``end_date`` — rango ISO-8601 (``YYYY-MM-DD`` o marca de
      tiempo completa) sobre la ventana de evaluación (``started_at`` /
      ``finished_at``). Esas columnas son nulas hasta que un worker puntúa la
      evaluación, por lo que dar un límite excluye las que están en cola/en proceso.

    ``granularity`` controla la profundidad del detalle: ``platform_executions``
    (solo la ejecución por plataforma), ``scenario_results`` (añade cada escenario)
    o ``metric_scores`` (añade cada turno y sus puntajes por métrica). Devuelve una
    lista ordenada por fecha de creación.
    """
    return await evaluation.retrieve_runs(
        run_id=run_id,
        platform=platform,
        start_date=start_date,
        end_date=end_date,
        granularity=granularity,
    )


@mcp.tool()
async def retrieve_turn_traces(turn_id: str, provenance: bool = True) -> list[dict]:
    """Recupera las trazas estructuradas de las métricas de un turno.

    Devuelve una entrada por métrica evaluada en el turno: su ``metric_name`` y su
    ``trace`` (``{"steps": [...]}`` o ``null``). Con ``provenance=true``
    (predeterminado) cada entrada añade también ``judge_model`` y ``rubric_version``;
    con ``provenance=false`` devuelve la forma mínima. El ``turn_id`` (UUID del turno)
    se obtiene de ``retrieve`` con ``granularity="metric_scores"``. Un ``turn_id``
    desconocido o inválido devuelve una lista vacía.
    """
    return await evaluation.retrieve_turn_traces(turn_id, include_provenance=provenance) or []


@mcp.tool()
async def retrieve_turn_token_usage(turn_id: str) -> dict:
    """Recupera el uso de tokens del LLM al puntuar un turno (sin agregación).

    Devuelve el ``TurnTokenUsage`` 1:1 del turno: ``input_tokens``, ``output_tokens``
    y el ``total_tokens`` derivado (``input + output``). Un turno que nunca se puntuó
    informa ceros. El ``turn_id`` (UUID del turno) se obtiene de ``retrieve`` con
    ``granularity="metric_scores"``. Un ``turn_id`` desconocido o inválido devuelve un
    objeto vacío.
    """
    return await evaluation.retrieve_turn_token_usage(turn_id) or {}


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
    # Plain-text logging like the API/worker. stdio transport speaks JSON-RPC over
    # stdout, so the logs go to stderr in that mode to avoid corrupting the protocol;
    # other transports log to stdout.
    log_stream = sys.stderr if transport == "stdio" else None
    configure_logging(settings.log_level, stream=log_stream)
    logger.info("mcp_server_starting", transport=transport)
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
