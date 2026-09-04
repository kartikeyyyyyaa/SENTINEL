"""Core security primitives.

Everything in this package is deliberately free of database and framework
dependencies, so it can be unit-tested in isolation and reasoned about on its
own. Nothing here reads configuration or global state; keys and secrets are
always passed in.
"""
