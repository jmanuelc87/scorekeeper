"""The domain: ingestion, the metric taxonomy, the retrieval pipeline, scoring.

Import from the submodules directly — this package deliberately re-exports
nothing. Re-exporting ``metrics`` or ``retrieval`` here would make *any* import
of ``scorekeeper.core.*`` pull in both catalogs and their optional LLM SDK
import paths, which the layering is meant to keep out of, say, the API layer.
"""
