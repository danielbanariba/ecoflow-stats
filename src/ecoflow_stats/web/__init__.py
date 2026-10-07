"""The server-rendered web layer: the FastAPI app, its routes, and (from
Phase 13 onward) the Jinja2/HTMX pages. Depends on the pure core and the
storage/acquisition adapters; nothing in the pure core ever depends back
on this package (contract-tested in ``tests/contract/test_pure_core_imports.py``).
"""
