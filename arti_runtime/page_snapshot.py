"""Owned data at the live runtime -> decision-page boundary (ADR 223).

Never call object copy/pickle hooks. Unsupported selected data fails with its
field path; runtime services and unconsumed DayPolicy state are not selected.
"""
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, fields, is_dataclass
import json
import math


class PageSnapshotTypeError(TypeError):
    pass


def plain_snapshot(value, *, path='page'):
    """Detach plain data, preserving tuple keys and list/tuple value semantics."""
    active=set()
    def walk(item, name, key=False):
        if item is None or type(item) in (str, bool, int):
            return item
        if type(item) is float:
            if not math.isfinite(item):
                raise PageSnapshotTypeError(name+': non-finite float')
            return item
        identity=id(item)
        if identity in active:
            raise PageSnapshotTypeError(name+': cyclic '+type(item).__qualname__)
        active.add(identity)
        try:
            if isinstance(item, Mapping):
                if key:raise PageSnapshotTypeError(name+': mapping key is not scalar/tuple')
                return {walk(k,name+'.<key>',True):walk(v,name+'['+repr(k)+']') for k,v in item.items()}
            if is_dataclass(item) and not isinstance(item,type):
                if key:raise PageSnapshotTypeError(name+': dataclass mapping key unsupported')
                return {f.name:walk(getattr(item,f.name),name+'.'+f.name) for f in fields(item)}
            if isinstance(item, Sequence) and not isinstance(item,(str,bytes,bytearray)):
                rows=[walk(v,name+'['+str(i)+']',key) for i,v in enumerate(item)]
                if key or isinstance(item,tuple):return tuple(rows)
                return rows
            if isinstance(item, Set):
                if key:raise PageSnapshotTypeError(name+': set mapping key unsupported')
                rows=[walk(v,name+'.<set>') for v in item]
                # No repr of an unknown object; every row is already plain data.
                return sorted(rows,key=lambda v:json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':')))
            raise PageSnapshotTypeError(name+': unsupported '+type(item).__module__+'.'+type(item).__qualname__)
        finally:
            active.remove(identity)
    return walk(value,path)


@dataclass(frozen=True)
class TravelPolicySnapshot:
    last_trips: dict
    walk_refusals: dict = None  # ADR 231 part B1: today's game words on a trip (fact_line._travel)
    place_visits: dict = None  # ADR 232 part C: her dated record of each place (fact_line._travel)


def fact_policy_snapshot(*, catalog=None, config=None, context=None, policy=None):
    """Only dependencies read by fact_line.present/visible_targets/_travel.

    Full catalog: receipt/text/name resolution. Context: viewport origin/bounds
    and current targets. Policy: measured trips only. No NavGrid, crop/chest
    managers, clocks/services, full runtime config or private unrelated state.
    """
    from arti_runtime.fact_line import SETTING
    for name,value in (('context',context),('config',config)):
        if value is not None and not isinstance(value,Mapping):
            raise PageSnapshotTypeError('policy.'+name+': expected Mapping, got '+type(value).__qualname__)
    context=context or {}
    world=context.get('world_knowledge') or {}
    if not isinstance(world,Mapping):raise PageSnapshotTypeError('policy.context.world_knowledge: expected Mapping')
    graph=world.get('world_graph') or {}
    if not isinstance(graph,Mapping):raise PageSnapshotTypeError('policy.context.world_knowledge.world_graph: expected Mapping')
    trips=(policy.get('last_trips',{}) if isinstance(policy,Mapping)
           else getattr(policy,'last_trips',{}) if policy is not None else {})
    trips=trips or {}
    if not isinstance(trips,Mapping):
        raise PageSnapshotTypeError('policy.last_trips: expected Mapping, got '+type(trips).__qualname__)
    refusals=(policy.get('walk_refusals',{}) if isinstance(policy,Mapping)
              else getattr(policy,'walk_refusals',{}) if policy is not None else {}) or {}
    if not isinstance(refusals,Mapping):
        raise PageSnapshotTypeError('policy.walk_refusals: expected Mapping, got '+type(refusals).__qualname__)
    visits=(policy.get('place_visits') if isinstance(policy,Mapping)
            else getattr(policy,'place_visits',None) if policy is not None else None) or {}
    if not isinstance(visits,Mapping):
        raise PageSnapshotTypeError('policy.place_visits: expected Mapping, got '+type(visits).__qualname__)
    return dict(catalog=plain_snapshot(catalog,path='policy.catalog'),
        config={SETTING:(config or {}).get(SETTING) is True},
        context=plain_snapshot(dict(location_id=context.get('location_id'),
            targets=context.get('targets') or {}, world_knowledge=dict(
                visible_tiles=world.get('visible_tiles'),world_graph=dict(current=graph.get('current')))),
            path='policy.context'),
        policy=TravelPolicySnapshot(plain_snapshot(trips,path='policy.last_trips'),
                                    plain_snapshot(refusals,path='policy.walk_refusals'),
                                    plain_snapshot(visits,path='policy.place_visits') if visits else None))
