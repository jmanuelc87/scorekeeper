"""Broker bootstrap — declare Celery's queue once, before any worker connects.

Celery's broker defaults to the app's own Postgres through kombu's SQLAlchemy
transport (see ``Settings.broker_url``). That transport creates its schema lazily, on
the first queue operation::

    # kombu/transport/sqlalchemy/__init__.py
    with _MUTEX:
        engine = self._engine_from_config()
        metadata.create_all(engine)

``_MUTEX`` is a *thread* lock, so it serializes threads inside one process and nothing
else. ``create_all`` then checks-then-creates, which is not atomic across connections.
Boot several workers at once — ``compose.yaml`` runs three replicas — and each one sees
``queue_id_sequence`` missing and issues ``CREATE SEQUENCE``; one wins and the rest die
on PostgreSQL's ``pg_class_relname_nsp_index``::

    duplicate key value violates unique constraint "pg_class_relname_nsp_index"
    DETAIL:  Key (relname, relnamespace)=(queue_id_sequence, 2200) already exists.

Declaring the queue from a single process that finishes *before* the workers start
removes the race: by the time they connect there is nothing left to create. It also
settles the smaller race behind it, the ``INSERT`` into ``kombu_queue`` that would
otherwise collide on the queue name's unique index.

``compose.yaml`` runs this in the ``migrate`` service, which the workers already wait
on via ``service_completed_successfully``.

This is a workaround for a broker that was never meant to be shared by many workers.
The real fix is a purpose-built broker — set ``CELERY_BROKER_URL`` to Redis and this
module turns into a no-op.
"""

from __future__ import annotations

import logging

from scorekeeper.config.settings import get_settings

logger = logging.getLogger(__name__)

# The prefix kombu's SQLAlchemy transport is addressed by. Any other broker (Redis,
# RabbitMQ) owns its own storage and has nothing for us to pre-create.
_SQLA_PREFIX = "sqla+"


def init_broker_schema() -> bool:
    """Declare Celery's queue if the broker is the SQLAlchemy transport.

    Idempotent, and safe to lose the race it exists to prevent: a concurrent creator
    surfaces as a duplicate-key error, which means the schema is already there and
    there is nothing left to do. Returns ``True`` when the queue was ensured, ``False``
    when the configured broker is not the SQLAlchemy transport.
    """
    if not get_settings().broker_url.startswith(_SQLA_PREFIX):
        logger.info("El broker no usa SQLAlchemy; no hay esquema que crear.")
        return False

    # Imported here rather than at module scope: this pulls in the Celery app, and the
    # module is loaded only by its console script.
    from sqlalchemy.exc import IntegrityError, ProgrammingError

    from scorekeeper.celery_app import celery_app

    queue = celery_app.conf.task_default_queue
    try:
        with celery_app.connection_for_write() as connection:
            # The transport creates its tables on the first queue operation, not on
            # connect — so declaring the queue is what actually materializes them.
            connection.default_channel.queue_declare(queue)
    except (IntegrityError, ProgrammingError):
        # Another process got there between the check and the create — the end state
        # we wanted anyway.
        logger.info("El esquema del broker ya existía (creado en paralelo).")
    else:
        logger.info("Broker listo: cola '%s' declarada.", queue)
    return True


def main() -> None:
    """Console-script entry point (``scorekeeper-init-broker``)."""
    from scorekeeper.utils.logging_config import configure_logging

    configure_logging(get_settings().log_level)
    init_broker_schema()
