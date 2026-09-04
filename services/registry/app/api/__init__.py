"""HTTP API: routers, request/response schemas and auth dependencies.

Nothing in here talks to the database directly except through ``..db``, and
nothing decides authorisation except by asking the database (``deps.py``
resolves a caller to a ``SecurityContext`` and nothing more — see ``..db`` for
why permissions are never trusted from a token).
"""
