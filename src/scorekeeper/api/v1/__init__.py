"""Version 1 of the HTTP API — one module per resource.

Route handlers call services **module-qualified** (``ingestion.ingest_evaluation``,
not a direct function import) so the attribute resolves at call time. Tests patch
the service module's attribute, which only works if the route looks it up then.
"""
