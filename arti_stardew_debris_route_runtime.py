"""Compose S17/S6 inside the same navigation run, budgets and owner session."""
from types import SimpleNamespace
from collections.abc import Mapping
import time
from arti_stardew_pause import game_now, wait_resume

import arti_stardew_skills as S
from arti_stardew_debris_route import DELTAS, MAX_PIECES, MAX_TREES, blocking_path, open_frontier, sweep_blockers

ROUTE_BLOCKER_SURVEYS = 4  # fix10e: survey legs one trip spends before an off-window-reachable blocker is final
from arti_stardew_tool_work import AXE_QIDS, scythe_area_safe, scythe_area_tiles


def small_tree_target(target):
    """W1 gap S15b: a growth-stage 3-4 wild tree is removed by tool_work cut_small_tree, not S6 tebang."""
    return target.get('kind') == 'tree' and type(target.get('growth_stage')) is int and target['growth_stage'] < 5


class DebrisRouteRuntimeMixin:
    def _debris_route_exit_objective(self, goal, pin, destination):
        """FINAL B J1268/1344: a clear-path approach can cross its parent S7 exit.

        Recover only the bound navigation intent. The tree was not removed; no
        clear receipt, input refund, or new route budget may be manufactured.
        """
        import arti_stardew_interact as interact
        run = self._debris_route_run()
        if (run is None or run.call.name != 'pergi_ke' or pin is None
                or not small_tree_target(run.state['route_clear']['target'])
                or run.state['route_clear']['identity'] != tuple(getattr(pin, k, None) for k in (
                    'runtime_instance_id', 'command_session_id', 'observation_session_id',
                    'player_id', 'screen_id', 'location'))):
            return None
        objective = interact.parse_interact_objective(S.objective_text(run.call))
        if (objective is None or interact.resolve_objective(objective, current_location=pin.location, home_location=None)
                or not interact.move_location_change_expected(objective, pin.location, destination)):
            return None
        return objective

    def _debris_route_exit_observed(self, goal):
        run = self._debris_route_run()
        if run is None:
            return
        run.state.pop('route_clear')
        # Reuse this goal's freshly verified navigation transition. Starting a
        # replacement goal here would discard the arrival we just witnessed.
        goal.tool_work = None
        run.state['nav_objective'] = goal.interact_objective
        run.pending = None
        self._journal(dict(ev='route_clear_abandoned_on_exit', goal_id=goal.goal_id,
                           inputs_refunded=0, removal_verified=False))

    def _debris_route_run(self):
        session = getattr(self, '_skill_session', None)
        run = session.current if session is not None and session.status == 'active' else None
        goal = getattr(self, '_active', None)
        if (run is None or goal is None or not run.state.get('route_clear')
                or not (run.goal_id == goal.goal_id or run.status == 'starting' and run.goal_id is None
                        and run.objective_text == goal.text)):
            return None
        return run

    def _debris_route_tool_reason(self, ref, args, window):
        """The existing rule/auto drivers also pass this exact-target fence."""
        run = self._debris_route_run()
        if run is None:
            return None
        target = run.state['route_clear']['target']
        if ref != target['ref']:
            return 'route_clear_target_changed'
        if target['kind'] == 'weeds':
            if 'stand' in target and (args.get('tile_x'), args.get('tile_y')) != tuple(target['stand']):
                return 'route_clear_stand_changed'
            if window is None or not scythe_area_safe(args, window):
                return 'route_clear_sweep_unobserved_or_unsafe'
            sweep = set(scythe_area_tiles(args.get('tile_x'), args.get('tile_y'), args.get('facing')))
            grid = None
            if hasattr(self, '_water_context'):
                pin = self._water_context(run.state['route_clear']['identity'][3])
                if pin is None:
                    return 'route_clear_sweep_unobserved_or_unsafe'
                grid = self._nav_grid(pin)
                if not grid:
                    return 'route_clear_sweep_unobserved_or_unsafe'
                grid = dict(grid)
                grid[args['tile_x'], args['tile_y']] = '.'  # the observed actor cell
            # ADR 128 (a54 d19 J1713-J2731, 5 trips home, same target): the local window holds only objects and terrain on
            # its 3x3 (arti_stardew_scene.gather_window), never a clump, so ADR 125's clump rule passed the plan and
            # refused the swing at the hollow log's cell 30,55. The fence reads the clumps the plan read.
            facts = dict(self._debris_route_scene_clumps(run), **window.get('targets', {}))
            blockers = sweep_blockers(sweep, facts, grid)
            if blockers:
                run.state['route_clear_sweep_refused'] = blockers[:6]
                return 'route_clear_sweep_protected'
        return None

    def _debris_route_scene_clumps(self, run):
        """Clump facts of the current scene, by their native refs; empty when the scene cannot be read."""
        rec = getattr(self, '_obs', None) and self._obs.get(run.state['route_clear']['identity'][3], 'scene.current_location')
        facts = getattr(rec, 'facts', None)
        if not isinstance(facts, Mapping) or getattr(rec, 'stale', True) or not getattr(rec, 'authoritative', False):
            return {}
        return {ref: fact for ref, fact in facts.items() if isinstance(fact, dict) and fact.get('kind') == 'clump'}

    def _debris_route_entries(self, entries):
        """Keep the planned safe scythe ingress in the existing approach contract."""
        run = self._debris_route_run()
        if run is None:
            return entries
        target = run.state['route_clear']['target']
        if target['kind'] != 'weeds' or 'stand' not in target:
            return entries
        return [dict(e, reach_tiles=[target['stand']], revision=str(e['revision']) + ':route:' + str(target['stand']))
                if e['ref'] == target['ref'] else e for e in entries]

    def _debris_route_publication_reason(self):
        session = getattr(self, '_skill_session', None)
        run = session.current if session is not None and session.status == 'active' else None
        if run is None or 'route_caps' not in run.state:
            return None
        _, cap, seconds = run.state['route_caps']
        if self._physical_action_publications - run.inputs_start >= cap:
            return 'route_clear_input_cap'
        if game_now(self) - run.started_at > seconds:
            return 'route_clear_seconds_cap'
        return None

    def route_clear_step(self, run, goal, request, caps, *, reaches, path=None):
        """ADR 176 entry for another walker, including fix99A's exit owner.

        Native reaches and optional cardinal path are repair intent, not input
        authority. Returns None (no repair), or the existing S.Step. A selected
        leg is kept on run.state.route_clear; the ordinary skill lifecycle owns
        staging, tool admission, removal proof and parent continuation. Caller
        owns its exit/contact policy. No exit, combat or arrival credit here.
        """
        caps = tuple(min(a, b) for a, b in zip(caps, run.state.get('route_caps', caps)))
        if run.decisions >= caps[0]:
            return S.Step('fail', reason='route_clear_decisions_cap')
        if self._physical_action_publications-run.inputs_start >= caps[1]:
            return S.Step('fail', reason='route_clear_input_cap')
        if game_now(self)-run.started_at > caps[2]:
            return S.Step('fail', reason='route_clear_seconds_cap')
        return self._debris_route_step(run, goal, request, caps, _route=dict(reaches=reaches, path=path))

    def _debris_route_step(self, run, goal, request, caps, *, _route=None):
        snap, state = request.snapshot, run.state
        clearing = state.get('route_clear')
        _route = _route if _route is not None else (clearing or {}).get('external_route')
        from arti_runtime import mines_exit
        mine_exit = _route is None and mines_exit.intent(run, S._g(S._g(snap, 'motor'), 'location'))
        mine_walk = (not mine_exit and str(S._g(S._g(snap, 'motor'), 'location') or '').startswith('UndergroundMine')
                     and (_route is not None or run.call.name in ('jalan_ke', 'pergi_ke')
                          or state.get('wave1_driver') == 'ladder' or clearing or getattr(goal, 'approach_clear_hint', None)))
        approach_reach = None
        # FIX73F: her forage walk is a hop: its plan's own pieces clear as on a trip, trees excepted (Review 40754232).
        # ADR 136: so is a walk to a sighting, and only its own plan clears: the object's tile is no destination.
        seen_walk = run.call.name == 'jalan_ke' and 'route_seen_walk' in state
        forage_hop = ((run.call.name == 'forage' and not state.get('wave1_driver') or seen_walk) and not clearing
                      and bool(getattr(goal, 'home_route_state', {}).get('repair_plan')))
        hop = _route is None and (run.call.name == 'pergi_ke' or forage_hop)
        native_mine_walk = mine_walk and state.get('wave1_driver') == 'ladder'
        if (_route is None and not clearing and not forage_hop and not mine_exit and not native_mine_walk
                and run.call.name not in ('pergi_ke', 'jalan_ke', 'rawat_hewan')):
            approach_reach = self._debris_route_approach_reach(run, goal, snap)
            if not approach_reach:
                return None
        last = S._g(S._g(snap, 'working_memory'), 'last_action')
        if last is not None and (S._g(last, 'cleanup_confirmed') is False
                                 or S._g(last, 'lifecycle') in ('uncertain', 'interrupted')):
            # A work skill's optional approach repair never fails the skill: its own reconciliation owns this.
            return None if approach_reach else S.Step('fail', reason='action_unreconciled')
        if S.menu_up(snap):
            return None if not clearing else S.Step('fail', reason='route_clear_menu_open')
        if S._g(S._g(snap, 'working_memory'), 'last_inspect_status') != 'current':
            return None if not clearing else S.Step('decision', S.INSPECT)
        p = mines_exit.policy(self) if mine_exit else None
        context = self._skill_wave1_context(snap)
        native_context = context  # recovery signatures describe native geometry, never a filtered planning view
        if mine_exit:
            from arti_stardew_skills_mines import eat_unsafe
            from arti_runtime.mines_survival import assessment
            if assessment(context, S._g(snap.motor, 'location')) == 'fight':
                if clearing:
                    state.pop('route_clear', None)
                    goal.tool_work = None
                return None
            combat = context.get('combat') or {}
            hp = min((n for n in (S._g(snap.motor, 'health'), combat.get('hp')) if type(n) is int), default=None)
            prior = state.get('exit_clear_hp')
            state['exit_clear_hp'] = hp
            if clearing and (eat_unsafe(combat) or hp is not None and prior is not None and hp < prior):
                return S.Step('fail', reason='mines_exit_clear_unsafe')
        if not clearing and p is not None and not mines_exit.chosen(run):
            # ADR 239: memory bounds the program's recovery; her chosen way out is always a real try.
            refusal = mines_exit.blocked(p, context, S._g(snap.motor, 'location'))
            if refusal:
                if (S._g(snap, 'world_interaction_target') or {}).get('kind') == 'ladder_up':
                    return None
                state[mines_exit.REPLAYED] = refusal
                return S.Step('fail', reason=refusal)
        if mine_walk:
            from arti_stardew_skills_mines import eat_unsafe
            # ADR 176: contact does not close a rock beyond the monster's
            # reach. Unknown work sites and a scythe sweep still yield to flee.
            in_contact = eat_unsafe(context.get('combat') or {})
            if clearing and in_contact:
                if not clearing.get('target', {}).get('tile') or clearing['target'].get('kind') == 'weeds':
                    state.pop('route_clear', None)
                    goal.tool_work = None
                    state['route_resuming'] = True
                    run.pending = None
                    return S.Step('next_leg', reason='route_clear_contact_reobserve')
            combat = context.get('combat') or {}
            if 'monsters' not in combat or not context.get('nav_grid'):
                return None if not clearing else S.Step('decision', S.INSPECT)
            from arti_runtime.route_clear import walking_context
            context = walking_context(context, snap.motor.tile)
            if context is None:
                return None if not clearing else S.Step('decision', S.INSPECT)
            from arti_stardew_skills_mines import DIG_STONE_CELLS
            context['route_clear_cost'] = DIG_STONE_CELLS
        if clearing:
            if context.get('identity') != clearing['identity']:
                return S.Step('fail', reason='route_clear_identity_changed')
            if not context.get('ui_complete') or context.get('local_window') is None:
                return S.Step('decision', S.INSPECT)
            if self._skill_finish_ready(goal, request):
                return S.Step('decision', S.FINISH)
            target = clearing['target']
            if mine_walk and tuple(target['tile']) not in context['nav_grid']:
                # A monster moved onto the planned work site. Re-observe the
                # parent walk without swinging or inventing removal credit.
                state.pop('route_clear')
                goal.tool_work = None
                state['route_resuming'] = True
                run.pending = None
                return S.Step('next_leg', reason='route_clear_threat_reobserve')
            equipped = S._g(snap, 'equipped_tool') or {}
            if target['kind'] == 'weeds' and equipped.get('qualified_item_id') == '(W)47':
                from arti_stardew_tool_work import faced_tile
                if (faced_tile(equipped) == target['tile'] and
                        ('stand' not in target or tuple(snap.motor.tile) == tuple(target['stand']))):
                    reason = self._debris_route_tool_reason(target['ref'], equipped, context['local_window'])
                    if reason == 'route_clear_sweep_protected':
                        # ADR 128: a piece that cannot be swung at from this stand is one blocked piece, not the trip.
                        return self._debris_route_blocked(run, S.Step('fail', reason=reason)) or S.Step('fail', reason=reason)
                    if reason:
                        return S.Step('fail', reason=reason)
            if target['kind'] != 'tree' or small_tree_target(target):
                if mine_walk:
                    # Use the fresh threat-filtered open grid for the approach,
                    # rather than an opaque catalogue route through a monster.
                    approach = S.clear_route_approach(snap, target['tile'], context, clearing['driver_state'])
                    if approach is not None:
                        return self._debris_route_blocked(run, approach) or approach
                step = S.wave1_step(S.SkillCall('bersihkan'), snap, clearing['driver_state'], context)
                return self._debris_route_blocked(run, step) or step
            # S6 owns removal + new wood + settled pickup. Narrow its catalog;
            # a drop remains offered only after the pinned tree has disappeared.
            view = SimpleNamespace(**{k: S._g(snap, k) for k in (
                'working_memory', 'motor', 'equipped_tool', 'selectable_items', 'select_item_available',
                'use_tool_available', 'gather_tool_target')})
            view.approach_targets = tuple(e for e in snap.approach_targets if e.get('ref') == target['ref']
                                         or str(e.get('ref', '')).startswith('drop:'))
            view.gather_tool_qids = AXE_QIDS
            if view.gather_tool_target != target['ref']:
                view.gather_tool_target = None
            if not view.gather_tool_target and not any(e.get('ref') == target['ref'] for e in view.approach_targets):
                approach = S.clear_route_approach(snap, target['tile'], context, clearing['driver_state'])
                if approach is not None:
                    return self._debris_route_blocked(run, approach) or approach
            step = S._gather(S.SkillCall('tebang'), view, clearing['driver_state'])
            return self._debris_route_blocked(run, step) or step
        if (S._g(snap.motor, 'is_player_free') is not True or S._g(snap.motor, 'can_player_move') is not True
                or not context.get('ui_complete') or not context.get('inventory_complete')
                or not context.get('nav_grid') or not context.get('identity')):
            return None
        destinations = (approach_reach if approach_reach else (run.call.tile,) if run.call.name == 'jalan_ke' and not seen_walk else
                        getattr(goal, 'home_route_state', {}).get('route_reaches', ()))
        if mine_walk and state.get('wave1_driver') == 'ladder':
            from arti_runtime.route_clear import clearing_reaches
            pin = state.get('wave1_pin') or {}
            destinations = clearing_reaches(context, [e for e in context.get('extras', ())
                if e.get('kind') == 'ladder_down' and e.get('ref') == pin.get('ref')])
        if mine_exit:
            # ADR 160: an up-ladder exit is a trip too. Re-read native stands on
            # every snapshot, including while the normal approach cannot see a path.
            destinations = mines_exit.reaches(context, S._g(snap.motor, 'location'))
        if _route is not None:
            destinations = _route['reaches']
            context['route_clear_path'] = _route.get('path')
        if _route is None and run.call.name == 'rawat_hewan':
            pin = snap.water_context or snap.movement_context
            if pin is None or pin.location != 'Farm' or state.get('animal_pet_phase'):
                return None
            # The next native house door is the animal routine's approach intent,
            # even when its whole-map route offer is absent because of debris.
            self._animal_prepare(goal, pin)
            raw = self._animal_raw(pin) or {}
            allowed = getattr(goal, 'animal_destinations', set())
            wanted = (getattr(goal, 'animal_executor', None) or {}).get('destination')
            houses = sorted((h for h in raw.get('houses', ()) if h['location'] in allowed
                             and h.get('enterable') is True and h.get('door')),
                            key=lambda h: (h['location'] != wanted, 'Coop' not in h['type'], h['location']))
            destinations = [(houses[0]['door'][0], houses[0]['door'][1]+1)] if houses else ()
        if not destinations:
            return None
        # Live7 Farm hops retain the native exit but may need to identify a blocked
        # corridor before that exit enters the rolling window (M5 D1 L267-L588).
        repair_reaches = (getattr(goal, 'home_route_state', {}).get('repair_reaches')
                          if hop else None)
        # ADR 125: a hop plan names its own next pieces; the boundary nearest the exit (a52 d13 J1423: 47,55, a wall) is not on it.
        planned = getattr(goal, 'home_route_state', {}).get('repair_plan') if hop else None
        if mine_exit:
            repair_reaches, planned = None, None  # ladder stands own this leg, never the old world hop
        if planned:
            repair_reaches = [tuple(t) for t in planned['reaches']]
        elif repair_reaches:
            from arti_stardew_goal_routes import local_route_reaches
            repair_reaches = local_route_reaches(context['nav_grid'], snap.motor.tile, destinations)
        boundary_repair = bool(repair_reaches) and not any(tuple(t) in context['nav_grid'] for t in destinations)
        if repair_reaches:
            destinations = repair_reaches
        if (_route is None and run.call.name == 'pergi_ke' and not mine_exit and not repair_reaches
                and self._debris_route_survey_first(goal, snap, context, destinations)):
            return None
        tree_cap = self._debris_route_tree_cap(run)
        if approach_reach:
            hint, goal.approach_clear_hint = goal.approach_clear_hint, None  # one plan per controller ending
            key = '%s@%s' % (hint['ref'], hint['location'])
            pieces = state.setdefault('approach_clear_pieces', {})
            pieces[key] = pieces.get(key, 0) + 1  # a selected piece keeps it; no plan exhausts it (below)
            state['approach_clear_last'] = hint['ref']
            if run.call.name != 'tebang':
                # Review 40754232: the owner farms this map too. Only tebang may fell a tree on its way;
                # tanam/siram/panen/forage/pungut_drop clear stone/weeds/twig only (a tree wall is skipped).
                tree_cap = 0
        plan = blocking_path(context['nav_grid'], snap.motor.tile, destinations, context['targets'],
                             [f.get('qualified_item_id') for f in context['inventory']],
                             remaining=MAX_PIECES-state.get('route_clear_count', 0),
                             tree_cap=tree_cap,
                             blocked=self._debris_route_blocked_refs(state, context),
                             repair_cost=context.get('route_clear_cost'), details=True,
                             route_path=context.get('route_clear_path'))
        explained = plan is not None
        if plan is not None:
            self._debris_route_fact(run, snap, plan)
            if plan.get('diagnostic_only'):
                plan = None
        if plan is None or str(plan.get('blocker')).startswith('route_blocked_unremovable:'):
            context['route_clear_tree_cap'] = tree_cap
            plan = self._debris_route_inspected_plan(run, request, snap, context, destinations) or plan
            if plan is not None and not plan.get('inspect_pending'):
                self._debris_route_fact(run, snap, plan)
                explained = True
        if approach_reach and (plan is None or plan.get('blocker')):
            pieces[key] = MAX_PIECES  # after identification: an open or unrepairable target is not retried
        if plan is None and not explained:
            from arti_stardew_goal_routes import open_paths
            here = S._wave1_xy(S._g(snap.motor, 'tile'))
            reached = open_paths(context['nav_grid'], here) if here is not None else {}
            if here is not None and any(tuple(t) in context['nav_grid'] for t in destinations) and not any(tuple(t) in reached for t in destinations):
                self._debris_route_fact(run, snap, None)
        if planned and (plan is None or plan.get('blocker')):
            blocker = plan.get('blocker') if plan else None
            if blocker == 'route_clear_piece_cap' or blocker == 'route_clear_tree_cap' and not forage_hop:
                return S.Step('fail', reason=blocker)
            # Not removable: the next snapshot plans around it. No input, no new read/piece/tree allowance.
            return self._debris_route_plan_refused(goal, run, planned, blocker)
        if plan is None or boundary_repair and plan.get('blocker'):
            # An optional window-edge shortcut cannot fail the native exit trip.
            # Keep the existing survey and every read/piece/tree/input/time cap.
            getattr(goal, 'home_route_state', {}).pop('repair_reaches', None)
            return None
        if plan.get('inspect_pending'):
            # Yield after this decision's read batch without turning incomplete
            # identification into exit_not_offered. The same visit/read cap and
            # remembered facts survive; no movement or clearing is authorized.
            return S.Step('decision', S.INSPECT)
        if plan.get('blocker'):
            if approach_reach:
                # A capped/unsupported repair never fails the work skill: its own approach/rotation goes on.
                self._journal(dict(ev='approach_clear_skipped', skill=run.call.name, blocker=plan['blocker']))
                return None
            if run.call.name == 'pergi_ke' and plan['blocker'].startswith(('route_blocked_unremovable:',
                                                                           'route_blocked_unsupported:')):
                # fix10e (a14 spring 12, 42x route_blocked_unremovable:boulder with an open way home off-window):
                # an unremovable blocker is final only for a complete read. While her open area still reaches the
                # window edge, the ordinary survey goes on (bounded per trip; its own read/input caps hold).
                frontier = open_frontier(context['nav_grid'], snap.motor.tile)
                deferred = state.get('route_blocker_unproven', 0)
                if frontier and deferred < ROUTE_BLOCKER_SURVEYS:
                    state['route_blocker_unproven'] = deferred + 1
                    self._journal(dict(ev='route_blocker_unproven', skill=run.call.name, blocker=plan['blocker'],
                                       frontier_cells=len(frontier), survey=deferred + 1))
                    getattr(goal, 'home_route_state', {}).pop('repair_reaches', None)
                    return None
            if mine_exit:
                mines_exit.remember(self, native_context, S._g(snap.motor, 'location'), plan['blocker'])
            return S.Step('fail', reason=plan['blocker'])
        if planned:
            getattr(goal, 'home_route_state', {}).pop('repair_plan', None)
        target = plan['target']
        if mine_exit:
            from math import hypot
            from arti_stardew_skills_mines import CONTACT_RADIUS_TILES
            monsters = [m for m in combat.get('monsters', ()) if not (type(m.get('hp')) is int and m['hp'] <= 0)]
            tiles = [S._wave1_xy(m.get('tile')) for m in monsters]
            if (any(t is None for t in tiles) or eat_unsafe(combat)
                    or any(hypot(cell[0]-t[0], cell[1]-t[1]) <= CONTACT_RADIUS_TILES
                           for cell in plan['path'] for t in tiles)):
                mines_exit.remember(self, context, S._g(snap.motor, 'location'), 'mines_exit_clear_unsafe')
                return S.Step('fail', reason='mines_exit_clear_unsafe')
        if mine_walk and in_contact and target['kind'] == 'weeds':
            return None  # the scythe sweeps the actor cell too; the normal exit/flee still owns this read
        from arti_stardew_tool_work_runtime import TOOL_WORK_MIN_ENERGY
        energy = S._g(snap.motor, 'energy')
        if (target['kind'] != 'weeds' and isinstance(energy, (int, float)) and not isinstance(energy, bool)
                and energy < TOOL_WORK_MIN_ENERGY):
            self._debris_route_fact(run, snap, dict(plan, blocker='route_clear_energy_low'))
            return S.Step('fail', reason='route_clear_energy_low')
        driver = 'small_tree' if small_tree_target(target) else {
            'stone': 'rock', 'weeds': 'weed', 'twig': 'twig', 'tree': 'tree'}[target['kind']]
        text = {'rock': 'pecah 1 batu', 'weed': 'potong 1 weed', 'twig': 'potong twig',
                'tree': 'tebang satu pohon', 'small_tree': 'potong 1 pohon kecil'}[driver]
        x, y = target['tile']
        driver_state = {}
        S.wave1_repin(driver_state, text, driver, 'observed_tool_effect',
                      ref=target['ref'], tile=target['tile'], bounds=(max(0, x-1), max(0, y-1), x+1, y+1))
        driver_state['route_path'] = [list(t) for t in plan['path']]  # PM-1 fix4 #6: followed past the window
        state.setdefault('route_caps', caps)
        if mine_exit and getattr(goal, 'mines_exit', None) is not None:
            state['mines_exit_steps_used'] = goal.mines_exit['steps']
        state['route_clear'] = dict(target=target, identity=context['identity'], text=text, driver_state=driver_state)
        if _route is not None:
            state['route_clear']['external_route'] = _route
        self._journal(dict(ev='route_clear_selected', target=target, path=[list(t) for t in plan['path']],
                           **({'for_skill': run.call.name, 'blocked_target': state['approach_clear_last']}
                              if approach_reach else {}),
                           piece_cap=MAX_PIECES, pieces_done=state.get('route_clear_count', 0), input_cap=caps[1],
                           tree_cap=MAX_TREES, trees_done=state.get('route_clear_trees', 0)))
        return S.Step('next_leg', reason='route_clear:' + target['kind'])

    def _debris_route_fact(self, run, snap, plan):
        from arti_runtime.route_clear import describe
        line = describe(plan, S._g(snap.motor, 'energy'))
        run.state['route_blockage'] = line
        self._route_blockage = dict(location=S._g(snap.motor, 'location'), line='Pemeriksaan jalan tadi: ' + line)

    def _debris_route_plan_refused(self, goal, run, planned, blocker):
        """ADR 125: the hop plan's first closed cell cannot be cleared, so plan again without it. Each refusal rules out
        at least that cell and the cells a read found closed, under a per-hop limit, so re-planning always ends."""
        hop = goal.home_route_state
        hop.pop('repair_plan', None)
        hop.pop('repair_reaches', None)
        memory = hop.setdefault('route_memory', {})
        cells = {tuple(t) for t in memory.get('closed', ())} | {tuple(planned['blocker'])}
        cells |= {tuple(t) for t in (run.state.get('route_inspect') or {}).get('closed', ())}
        memory['closed'] = sorted(cells)
        hop['route_plan_refusals'] = hop.get('route_plan_refusals', 0) + 1
        reads = (run.state.get('route_inspect') or {}).get('reads', 0)
        self._journal(dict(ev='route_plan_refused', blocker_cell=list(planned['blocker']), blocker=blocker,
                           closed=len(cells), refusals=hop['route_plan_refusals'], reads=reads))
        from arti_stardew_goal_routes import ROUTE_PLAN_REFUSALS_MAX
        if reads >= self.ROUTE_INSPECTS_MAX or hop['route_plan_refusals'] >= ROUTE_PLAN_REFUSALS_MAX:
            # The reads are spent and the farm's map is known: a true end, not another walk (d8 J1110 walked 137 inputs to say less).
            return S.Step('fail', reason='route_no_clearable_way')
        return S.Step('decision', S.INSPECT)

    ROUTE_BLOCKED_MAX = 3  # stalled clear targets per location visit before the named route_clear_blocked_cap
    def _debris_route_approach_reach(self, run, goal, snap):
        """Live7 (owner, smoke 2026-10-23: "[recorded remark omitted]"): a work
        skill's approach the controller just ended REPLAN_REQUIRED/BLOCKED names its target's stands; the SAME capped
        blocking_path + S17/S6 clear legs as pergi_ke then remove the first natural blocker (stone -> pickaxe, weeds ->
        scythe, twig -> axe, tree -> axe) and resume the work skill. One plan per
        controller ending (the hint is consumed), generically for every non-combat walk (ADR 176);
        up to MAX_PIECES pieces per blocked target, and the run's piece cap
        still applies. An open route (None) or a capped/unsupported repair leaves the skill's own approach unchanged
        and is not re-planned for that target. Only tebang may fell a tree (tree_cap 0 otherwise)."""
        if not run.state.get('live7') or run.state.get('wave1_driver') in ('combat', 'combat_floor'):
            return None
        hint = getattr(goal, 'approach_clear_hint', None)
        location = S._g(S._g(snap, 'motor'), 'location')
        if not isinstance(hint, dict) or hint.get('location') != location or not hint.get('reach'):
            return None
        if run.state.get('approach_clear_pieces', {}).get('%s@%s' % (hint['ref'], location), 0) >= MAX_PIECES:
            return None  # this target already had its capped repairs (a wall of up to MAX_PIECES pieces)
        return tuple(tuple(t) for t in hint['reach'])

    @staticmethod
    def _debris_route_tree_cap(run):
        """Review 40754232: the owner farms this map too. A forage walk or a walk to a sighting clears stone, weeds and twig; its plan goes round a tree."""
        return 0 if run.call.name == 'forage' or 'route_seen_walk' in run.state else MAX_TREES-run.state.get('route_clear_trees', 0)

    @staticmethod
    def _debris_route_blocked_refs(state, context):
        memo = state.get('route_clear_blocked')
        return tuple(memo['refs']) if memo and memo.get('identity') == context.get('identity') else ()

    def _debris_route_blocked(self, run, step):
        """FINAL Part C4 (J L2942-L3121): the clear approach to the pinned target stalled (CLEAR_APPROACH_STALLED,
        decided before any further press). The target is NOT cleared: no receipt, piece credit or input refund. It
        stays occupied and is never re-selected this location visit; the same run resumes its navigation leg, whose
        next repair plans around it (blocking_path ``blocked``). A mature-tree FULL_BAG before ANY swing is the same
        unavailable auxiliary target (a52 d21 J549); after a swing its gather/reconciliation still owns the evidence.
        None for every other step."""
        if step is None or step.kind != 'fail':
            return None
        clearing = run.state.get('route_clear') or {}
        target = clearing.get('target') or {}
        goal = getattr(self, '_active', None)
        full_before_work = (step.reason == 'gather_full_bag' and target.get('kind') == 'tree'
                            and not small_tree_target(target) and getattr(goal, 'gather_steps', None) == 0
                            and not S._g(getattr(goal, 'gather_progress', None), 'one_tree_effect'))
        if step.reason not in (S.CLEAR_APPROACH_STALLED, 'route_clear_sweep_protected') and not full_before_work:
            return None
        clearing = run.state.pop('route_clear')
        target = clearing['target']
        memo = run.state.get('route_clear_blocked')
        if not memo or memo.get('identity') != clearing['identity']:
            memo = run.state['route_clear_blocked'] = dict(identity=clearing['identity'], refs=[])
        memo['refs'].append(target['ref'])
        self._journal(dict(ev='route_clear_blocked', target=target, reason=step.reason,
                           blocked=len(memo['refs']), cap=self.ROUTE_BLOCKED_MAX, removal_verified=False,
                           **({'sweep_refused': run.state.pop('route_clear_sweep_refused')}
                              if 'route_clear_sweep_refused' in run.state else {})))
        if len(memo['refs']) >= self.ROUTE_BLOCKED_MAX:
            return S.Step('fail', reason='route_clear_blocked_cap')
        run.state['route_resuming'] = True
        run.pending = None
        return S.Step('next_leg', reason='route_clear_blocked:' + target['kind'])

    ROUTE_INSPECTS_MAX = 6  # native inspect_tiles reads per location visit of one pergi_ke run
    ROUTE_INSPECTS_PER_DECISION = 2

    def _debris_route_inspected_plan(self, run, request, snap, context, destinations):
        """GAP8E: the exit stand is observed, no observed-open path reaches it, and blocking_path found no repair
        because the closed cells on the cheapest route have no fresh identity (scene caps; radius-1 gather window).
        Identify them with the existing bounded native inspect_tiles read (S17's), then run the SAME capped
        blocking_path over scene + read facts. Only a removable natural-debris target is accepted; a read that
        finds walls/fences/unknowns records them as closed and the survey continues unchanged. No input."""
        from arti_stardew_debris_route import (unidentified_path, inspect_square, inspected_closed, unremovable_tiles)
        from arti_stardew_native_targets import xy
        grid, here = context['nav_grid'], xy(snap.motor.tile)
        if here is None or not any(xy(d) in grid for d in destinations):
            return None
        pin = getattr(snap, 'water_context', None) or getattr(snap, 'movement_context', None)
        if pin is None:
            return None
        memo = run.state.setdefault('route_inspect', {})
        if memo.get('identity') != context.get('identity'):
            memo.clear()
            memo.update(identity=context.get('identity'), reads=0, closed=[], facts={})
        # FINAL Part C proof1 S26 (J L1745-L1779, L1828-L2289): each decision dropped the facts its reads identified,
        # so 33,48 was read three times (reads 1/3/5) and, after the one verified clear, the next blockers on the same
        # planned path (46,57 / 45,58 / 45,59, identified by read 6) had no identity left: blocking_path found nothing,
        # the six-read cap was spent, and the survey walked until exit_not_offered. Native read facts stay identities
        # for this location visit (the fresh scene wins for the same ref; only closed grid cells can become targets,
        # and the clear driver still pins the native ref/tile and verifies the tool effect).
        remembered = memo.setdefault('facts', {})
        targets = {**remembered, **context['targets']}
        tools = [f.get('qualified_item_id') for f in context['inventory']]
        if remembered:
            plan = blocking_path(grid, here, destinations, targets, tools,
                                 remaining=MAX_PIECES-run.state.get('route_clear_count', 0),
                                 tree_cap=context.get('route_clear_tree_cap', self._debris_route_tree_cap(run)),
                                 blocked=self._debris_route_blocked_refs(run.state, context),
                                 repair_cost=context.get('route_clear_cost'), details=True,
                                 route_path=context.get('route_clear_path'))
            found = plan.get('target') if plan else None
            if found is not None:
                self._journal(dict(ev='route_inspect', status='remembered', facts=len(remembered),
                                   reads=memo['reads'], target=found['ref']))
                return plan
        for _ in range(self.ROUTE_INSPECTS_PER_DECISION):
            # ADR 108 (a48 d12 J770, J1315, J1957): the cheapest path crossed a stump, so no other route was ever read.
            cells = unidentified_path(grid, here, destinations, targets,
                                      {tuple(t) for t in memo['closed']} | unremovable_tiles(targets, tools),
                                      route_path=context.get('route_clear_path'))
            if not cells or memo['reads'] >= self.ROUTE_INSPECTS_MAX:
                return None
            center, radius = inspect_square(cells)
            memo['reads'] += 1
            from arti_stardew_skills_runtime import SkillRuntimeMixin
            facts = SkillRuntimeMixin._skill_inspect_tiles(self, request, pin, center[0], center[1], radius)
            if facts is None:
                self._journal(dict(ev='route_inspect', center=list(center), radius=radius, status='unavailable',
                                   reads=memo['reads']))
                return None
            targets.update(facts)
            remembered.update(facts)
            closed = inspected_closed(grid, center, radius, facts)
            memo['closed'] = sorted({tuple(t) for t in memo['closed']} | set(closed))
            plan = blocking_path(grid, here, destinations, targets, tools,
                                 remaining=MAX_PIECES-run.state.get('route_clear_count', 0),
                                 tree_cap=context.get('route_clear_tree_cap', self._debris_route_tree_cap(run)),
                                 blocked=self._debris_route_blocked_refs(run.state, context),
                                 repair_cost=context.get('route_clear_cost'), details=True,
                                 route_path=context.get('route_clear_path'))
            found = plan.get('target') if plan else None
            from arti_stardew_debris_route import footprint, sweep_blockers
            first_cell = cells[0]
            first_occupants = [[ref, f.get('kind'), f.get('name') or f.get('terrain_type') or f.get('clump_type'),
                                (f.get('wild_tree') or {}).get('growth_stage')]
                               for ref, f in facts.items() if first_cell in footprint(f)]
            # d8 J644 could not say why identified weeds gave no target: name what a swing from each open side would cover.
            sweep = ([[list(t), sweep_blockers(scythe_area_tiles(*t, facing), targets, grid)] for t, facing in (
                ((first_cell[0], first_cell[1]-1), 2), ((first_cell[0]+1, first_cell[1]), 3),
                ((first_cell[0], first_cell[1]+1), 0), ((first_cell[0]-1, first_cell[1]), 1)) if grid.get(t) == '.']
                if found is None and any(o[2] == 'Weeds' for o in first_occupants) else None)
            self._journal(dict(ev='route_inspect', center=list(center), radius=radius, status='read',
                               unidentified=[list(t) for t in cells], facts=len(facts), closed=len(closed),
                               first_cell_occupants=first_occupants,
                               **({'first_cell_sweep': sweep} if sweep else {}),
                               reads=memo['reads'], target=found and found['ref'],
                               blocker=plan.get('blocker') if plan else None))
            if found is not None:
                return plan
        cells = unidentified_path(grid, here, destinations, targets,
                                  {tuple(t) for t in memo['closed']} | unremovable_tiles(targets, tools),
                                  route_path=context.get('route_clear_path'))
        pending = bool(cells) and memo['reads'] < self.ROUTE_INSPECTS_MAX
        self._journal(dict(ev='route_inspect', status='pending' if pending else 'exhausted',
                           reads=memo['reads'], remaining_unidentified=len(cells or ()),
                           read_cap=self.ROUTE_INSPECTS_MAX))
        if pending:
            return dict(inspect_pending=True)
        return None

    @staticmethod
    def _debris_route_survey_first(goal, snap, context, destinations):
        """Whether ``pergi_ke`` walks the observed route survey instead of selecting a route repair now.

        GAP8B J1 L10-L1309: the exit stand (41,1) lay INSIDE the observed rolling grid with no open path, yet
        a reachable survey waypoint suppressed every repair; 22 waypoints later the survey ran out
        (exit_not_offered) and the tree that gap7b cut (J L699) was never selected. An observed exit stand is
        a complete local read: surveying cannot open it, so the bounded repair runs (it returns None when an
        open path exists or no supported blocker is observed).
        GAP7B J1 L833-L838: the Backwoods exit (48,17) lay OUTSIDE the grid and the boundary-only repair felled
        a mature tree although observed-open cells already led toward that exit. While the exit is unobserved,
        an observed-open escape toward it, or an unexhausted survey with a reachable waypoint, wins.
        """
        from arti_stardew_goal_routes import open_paths, open_path
        from arti_stardew_native_targets import xy
        grid = context['nav_grid']
        reaches = [t for t in (xy(d) for d in destinations) if t is not None]
        if not reaches or any(t in grid for t in reaches):
            return False  # the exit stand is observed: the grid itself proves whether repair is needed
        here = xy(snap.motor.tile)
        parents = open_paths(grid, here)
        if here is None or not parents:
            return False
        distance = lambda t: min(abs(t[0]-d[0]) + abs(t[1]-d[1]) for d in reaches)
        start = distance(here)
        if any(distance(t) < start and any((t[0]+dx, t[1]+dy) not in grid for dx, dy in DELTAS)
               for t in parents):
            return True  # an observed-open escape toward the unseen exit: no tree for an unproven route
        if (getattr(goal, 'home_route_state', {}) or {}).get('survey_reset'):
            return False  # the bounded survey already ran out once; never loop it instead of a repair
        return any(e.get('kind') == 'home_route_stand' and e.get('tile') is not None
                   and open_path(parents, e['tile']) is not None
                   for e in S._g(snap, 'approach_targets', ()))

    def _debris_route_completed(self, run, goal, last):
        clearing = run.state['route_clear']
        target = clearing['target']
        if target['kind'] == 'tree' and not small_tree_target(target):
            progress = getattr(goal, 'gather_progress', None)
            effect = S._g(progress, 'one_tree_effect') or {}
            verified = (last.reason == 'observed_inventory_objective' and effect.get('target_ref') == target['ref']
                        and S._g(progress, 'status') == 'INVENTORY_SATISFIED')
        else:
            verified = (last.reason == 'observed_tool_effect' and last.observed.get('target_ref') == target['ref'])
        if not verified:
            return 'fail', 'route_clear_effect_unverified:' + target['kind']
        # Keep the native receipt and the SAME run/caps; removal is not arrival.
        run.state.setdefault('route_clear_evidence', []).append(dict(target=target, reason=last.reason,
                                                                     observed=dict(last.observed)))
        run.state['route_clear_count'] = run.state.get('route_clear_count', 0) + 1
        self._route_blockage = None  # a verified removal invalidates the old path's count
        if target['kind'] == 'tree':
            run.state['route_clear_trees'] = run.state.get('route_clear_trees', 0) + 1
        run.state.pop('route_clear')
        run.state['route_resuming'] = True
        run.pending = None
        return 'next_leg', 'route_resume_after_verified_clear'
