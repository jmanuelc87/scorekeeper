"""The business logic, one module per phase of a run's life.

``ingestion`` -> ``retrieval`` -> ``scoring`` follow a run from uploaded files to
scored results; ``runs`` covers the lifecycle transitions in between;
``read_models`` and ``serializers`` are the query side.

Import from the submodules directly — this package re-exports nothing.
"""
