"""ADR 220/223: the actor's current HUD is its own display witness.

Captured at live boundaries before either projection. Never infer a maximum.
"""
from collections.abc import Mapping

KEYS = frozenset(('hud', 'location', 'time', 'time_text', 'clock', 'date', 'season',
    'weekday', 'weather', 'energy', 'max_energy', 'hp', 'health', 'max_hp', 'max_health',
    'money', 'equipped', 'held_item', 'menu_open', 'bag', 'inventory',
    'inventory_highlights', 'tools', 'watering_can'))


def current(runtime, session, snapshot, context, weather):
    from arti_stardew_skills import _g
    motor = snapshot.motor
    native = runtime._state.latest(snapshot.binding.player_id)
    valid = (native is not None and not runtime._state.is_stale(snapshot.binding.player_id)
             and _g(native, 'revisi') == _g(motor, 'revisi')
             and _g(native, 'location') == _g(motor, 'location'))
    own = (_g(native, 'own_state') or {}) if valid else {}
    energy, health = own.get('energy') or {}, own.get('health') or {}
    combat = context.get('combat') or {}
    date = [_g(motor, k) for k in ('year','season','day')]
    return dict(location=_g(motor, 'location'), energy=_g(motor, 'energy'), max_energy=energy.get('max', _g(motor, 'max_energy')),
        hp=_g(native, 'health') if valid else None, max_hp=health.get('max', combat.get('max_hp')),
        money=own.get('money', context.get('money')), clock=_g(motor, 'time_of_day'),
        date=date, season=date[1],
        weather=weather or 'unknown', equipped=runtime._skill_facts(snapshot).equipped)


def protect(offer, hud):
    """One small canonical floor, including on menu and emergency pages."""
    facts = offer.setdefault('facts', {})
    hud = dict(hud)
    # The normal offer already resolves the held tool/item from the native slot.
    if hud.get('equipped') is None:
        hud['equipped'] = facts.get('equipped')
    ui = offer.get('unknown_ui') or {}
    if isinstance(ui.get('held_item'), Mapping):
        hud['held_item'] = dict(ui['held_item'])
    facts['hud'] = hud
    return offer
