"""The Celery application that runs evaluations off the request path.

The HTTP API ingests an upload synchronously (parse + persist the run tree) and
then enqueues a single ``scorekeeper.score_run`` task carrying only the ``run_id``;
this worker consumes the queue and does the slow LLM-as-a-judge scoring. Clients
poll ``BenchmarkRun.status`` instead of a Celery result, so no result backend is
configured.

Broker: the app's own Postgres via kombu's SQLAlchemy transport (see
``Settings.broker_url``); kombu auto-creates its ``kombu_message`` / ``kombu_queue``
tables on first connect. Constructing this app opens no broker connection, so it is
safe to import from the API process and from tests.
"""

from __future__ import annotations

import logging

from celery import Celery
from celery.signals import after_setup_logger, after_setup_task_logger

from scorekeeper.config import get_settings

celery_app = Celery(
    "scorekeeper",
    broker=get_settings().broker_url,
    # Import the tasks module lazily (only when the worker boots), which avoids a
    # celery_app <-> tasks circular import at module load.
    include=["scorekeeper.tasks"],
)
celery_app.conf.update(
    task_ignore_result=True,  # status lives in BenchmarkRun.status; no result backend
    task_serializer="json",
    accept_content=["json"],
    task_acks_late=True,  # re-deliver the job if a worker dies mid-scoring
    worker_prefetch_multiplier=1,  # long tasks -> fair, one-at-a-time dispatch
    task_track_started=True,
)


def _quiet_http_request_logging() -> None:
    """Silence the per-request HTTP INFO logs from the judge's HTTP client.

    Each judge call makes the ``httpx`` client log ``HTTP Request: POST
    https://api.anthropic.com/v1/messages`` at INFO, which floods the worker output
    (one line per metric per turn). Our own evaluation logs are the signal we want, so
    keep httpx/httpcore at WARNING.
    """
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# Celery owns logging in the worker and configures it on boot, so set the levels from
# these signals (which fire *after* Celery's setup) rather than at import time — an
# import-time level would be overwritten when Celery hijacks the root logger.
@after_setup_logger.connect
def _on_after_setup_logger(**_kwargs: object) -> None:
    _quiet_http_request_logging()


@after_setup_task_logger.connect
def _on_after_setup_task_logger(**_kwargs: object) -> None:
    _quiet_http_request_logging()
