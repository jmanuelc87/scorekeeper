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

from celery import Celery
from celery.signals import after_setup_logger, after_setup_task_logger

from scorekeeper.config.settings import get_settings
from scorekeeper.utils.logging_config import configure_logging

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


# Celery owns logging in the worker and configures (and hijacks) it on boot, so
# install our structlog dual output from these signals — which fire *after* Celery's
# setup — with force=True to override it. Configuring at import time would be
# overwritten when Celery reconfigures the root logger. This also quiets the httpx
# per-request INFO lines that would otherwise flood the worker output.
@after_setup_logger.connect
def _on_after_setup_logger(**_kwargs: object) -> None:
    configure_logging(get_settings().log_level, force=True)


@after_setup_task_logger.connect
def _on_after_setup_task_logger(**_kwargs: object) -> None:
    configure_logging(get_settings().log_level, force=True)
