"""Synthetic native graph for legacy boundary tests; never imported by production.

Locks are supplied by each test, not inferred by the Python controller's clock,
bag or calendar. Native gate computation is covered by Protocol --fix11a.
"""
from collections import deque

LINKS = (("Cabin_fixture", "Farm"), ("Cabin_other", "Farm"), ("Farm", "Forest"),
         ("Farm", "BusStop"), ("Farm", "Backwoods"), ("BusStop", "Town"),
         ("Backwoods", "Mountain"), ("Town", "Forest"), ("Town", "Mountain"),
         ("Town", "SeedShop"), ("Town", "Beach"), ("Mountain", "Mine"))


def catalog(current="Farm", *, locked=None, home="Cabin_fixture"):
    locked = locked or {}
    order = ["Farm", "Forest", "Town", "SeedShop", "Mountain", "Mine", "Beach", "BusStop",
             "Backwoods", "Cabin_fixture", "Cabin_other"]
    links = LINKS
    if current.startswith(("Cabin", "FarmHouse")) and current not in order:
        order.append(current)
        links = (*links,(current,"Farm"))
    paths = {current: []}
    queue = deque([current])
    while queue:
        here = queue.popleft()
        for left, right in links:
            other = right if here == left else left if here == right else None
            if other is not None and other not in paths:
                paths[other] = paths[here] + [other]
                queue.append(other)
    places = []
    for identity in order:
        reason = locked.get(identity)
        if identity not in paths:
            reason = reason or "unknown: no fixture connection"
        places.append(dict(id=identity, label=identity, reachable_now=reason is None,
            go_to_available=reason is None, locked=reason, route=paths.get(identity, []),
            motor_route=paths.get(identity, []), facts=["fishing: game-data catches; rod required"]
            if identity in ("Forest", "Beach") else []))
    return dict(scope="synthetic-world:actor", date="1:spring:1", game_facts=[],
                world_graph=dict(current=current, home=home, complete=True, places=places, connections=[]))
