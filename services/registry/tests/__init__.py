"""Tests for the security core.

Written against stdlib ``unittest`` rather than pytest, on purpose: these are the
tests most likely to be run on a machine nobody prepared — a teammate's laptop,
a judge's laptop, a fresh container minutes before a deadline. Zero install:

    cd services/registry && python -m unittest discover -s tests -v

The interesting cases here are the negative ones. A round-trip test proves the
happy path works; it says nothing about whether the thing is *secure*. So most of
what follows tries to break the primitives in the specific ways an attacker
would.
"""
