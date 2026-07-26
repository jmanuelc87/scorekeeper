"""Persistence: the engine and session convention, the ORM models, the queries.

Import from the submodules directly — this package deliberately re-exports
nothing so that importing it stays side-effect free (in particular, importing
``scorekeeper.db.models`` must not construct an engine).
"""
