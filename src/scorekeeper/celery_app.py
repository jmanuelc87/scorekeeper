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
