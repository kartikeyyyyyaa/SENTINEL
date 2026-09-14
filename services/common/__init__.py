"""Code shared between the registry service, the edge workers and the state core.

Kept dependency-free on purpose. Anything imported here is imported by processes
running on very different machines — a GPU box at a district control room, a
container in the state data centre, a laptop during the live test — so a dependency
added here is a dependency that must install correctly in all three places.
"""
