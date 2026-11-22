"""Read-only W13 Haley staging guard, evaluated before publishing stage_event.

The input is a fresh native inspect_context receipt; a debug command spelling
Beach does not establish that the game resolved the plain Beach instance.
"""
from __future__ import annotations
import json
import time
from pathlib import Path

def plain_beach(receipt, *, now_ms=None):
    now_ms=int(time.time()*1000) if now_ms is None else now_ms
    if not isinstance(receipt,dict):
        raise ValueError('Haley needs a native inspect_context receipt')
    context=receipt.get('context')
    captured=receipt.get('completed_unix_ms')
    if (receipt.get('op')!='inspect_context' or receipt.get('success') is not True
            or receipt.get('status')!='completed' or not isinstance(context,dict)
            or type(captured) is not int or not 0<=now_ms-captured<=2000):
        raise ValueError('Haley needs a fresh completed native inspect_context (<=2s)')
    if receipt.get('location_id')!='Beach' or context.get('location_id')!='Beach':
        raise ValueError('Haley needs plain Beach; BeachNightMarket/festival fixtures are incompatible')
    for key in ('session_id','runtime_instance_id','player_id','screen_id','context_generation'):
        if receipt.get(key) is None or receipt[key]!=context.get(key):
            raise ValueError('Haley native identity mismatch: '+key)
    if context.get('mode')!='gameplay' or context.get('is_player_free') is not True or context.get('can_player_move') is not True:
        raise ValueError('Haley needs free gameplay before staging')
    date=(context.get('animal_routine') or {}).get('date')
    if (not isinstance(date,list) or len(date)!=3 or type(date[0]) is not int
            or date[1] not in ('spring','summer','fall','winter') or type(date[2]) is not int or not 1<=date[2]<=28):
        raise ValueError('Haley native calendar unavailable')
    if date[1]=='winter' and 15<=date[2]<=17 or date[1]=='summer' and date[2]==28:
        raise ValueError('Haley date conflicts with Night Market or Moonlight Jellies; use offline Summer 26 staging')
    return tuple(date)

def check_file(path):
    return plain_beach(json.loads(Path(path).read_text(encoding='utf-8-sig')))
