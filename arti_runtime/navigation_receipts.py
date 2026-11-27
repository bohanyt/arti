"""Delayed, independently observed travel effects (ADR 121). No input here."""
from dataclasses import replace


def remember_transition(goal, pin, action, destination, date):
    """ADR 210: retain the settled command's pin before recovery revokes play."""
    if goal is None or pin is None:
        return
    if (action.lifecycle == 'completed' and action.cleanup_confirmed is True and action.request_id
            and action.after is None and action.reason in ('TransitionObserved','TransitionObservedEventUp')):
        identity = tuple(getattr(pin,k,None) for k in
                         ('runtime_instance_id','command_session_id','observation_session_id','player_id','screen_id'))
        generation = getattr(pin, 'context_generation', None)
        if (None not in identity and getattr(goal, 'play_identity', None) == identity
                and type(generation) is int and generation > 0
                and isinstance(date, tuple) and len(date) == 3 and None not in date):
            goal.navigation_readback_pin = (action.request_id, identity, destination, generation, date)


def reconcile_menu_transition(runtime, goal, pin, before, current):
    action = getattr(goal, 'last_action', None)
    objective = getattr(goal, 'interact_objective', None)
    session = getattr(runtime, '_skill_session', None)
    run = getattr(session, 'current', None)
    identity = tuple(getattr(pin,k,None) for k in
                     ('runtime_instance_id','command_session_id','observation_session_id','player_id','screen_id'))
    menu_transition = action is not None and action.reason == 'UNCERTAIN(MENU_OPENED_MID_MOVE)'
    from arti_stardew_interact import move_location_change_expected
    delayed_transition = (action is not None and action.reason in ('TransitionObserved','TransitionObservedEventUp')
        and action.lifecycle == 'completed' and action.after is None
        and move_location_change_expected(objective, before, current))
    festival_leave = (action is not None and action.lifecycle == 'completed'
        and action.world_effect_status == 'observed_pad_button'
        and ((action.after or {}).get('pad_button') or {}).get('effect') == 'festival_leave_asked'
        and before == 'Temp')
    receipt = getattr(goal, 'navigation_readback_pin', None)
    settled_pin = (delayed_transition and isinstance(receipt, tuple) and len(receipt) == 5
                   and receipt[:3] == (action.request_id, identity, current)
                   and type(getattr(pin, 'context_generation', None)) is int
                   and pin.context_generation > receipt[3])
    if (action is None or not (menu_transition or delayed_transition or festival_leave)
            or action.cleanup_confirmed is not True or not action.request_id
            or objective is None or not objective.navigation or objective.blocker
            or not before or before == current
            or (not festival_leave and (action.before or {}).get('location') != before)
            or run is None or run.goal_id != goal.goal_id or run.call.name != 'pergi_ke'
            or None in identity or not (getattr(goal,'play_identity',None) == identity
                or getattr(goal,'play_identity',None) is None and settled_pin)
            or run.pending not in (None, ('fail', action.reason), ('fail', 'action_unreconciled'))
            or runtime._gather_mutation_unresolved or getattr(goal, 'gather_uncertain', False)):
        return False
    if delayed_transition and not settled_pin:
        return False
    motor = runtime._state.latest(pin.player_id)
    # The fresh, bound gameplay pin cannot exist while a menu/transition owns
    # control. Require its independent motor reading as well as native release.
    if (motor is None or runtime._state.is_stale(pin.player_id) or motor.location != current
            or motor.active_menu is not None or motor.is_player_free is not True
            or motor.can_player_move is not True
            or (not festival_leave and ((action.before or {}).get('player_id') != pin.player_id
                                        or (action.before or {}).get('screen_id') != pin.screen_id))):
        return False
    if delayed_transition and tuple(getattr(motor,k,None) for k in ('year','season','day')) != receipt[4]:
        return False
    evidence = replace(action, lifecycle='completed', reason=(
        'observed_transition_after_menu' if menu_transition
        else 'observed_transition_after_festival_leave' if festival_leave
        else 'observed_transition_after_readback'),
        outcome='LocationChanged', after=dict(location=current, location_id=current),
        world_effect_status='observed_location_change', observed_changes=('location',))
    goal.last_action = evidence
    goal.navigation_readback_pin = None
    if delayed_transition:
        runtime._expected_location = current
    run.pending = None
    if run.state.get('ui_uncertain_cause') == action.reason:
        run.state.pop('ui_uncertain_cause')
    runtime._interaction_effect(goal, 'transition', evidence, before, current)
    runtime._journal(dict(ev='navigation_transition_reconciled', request_id=action.request_id,
                          original_reason=action.reason, **{'from':before,'to':current}))
    return True
