"""ADR 121: addressable, lossless sections on the existing read-only surface."""
import hashlib
import json


def detail_read_payload(payload, selected, root_handle, measure, ceiling):
    if not selected or selected[1] != 0:
        return None
    base = {k: payload[k] for k in ('live7', 'owner_goal', 'mind', 'budget', 'echo', 'mode', 'contract', 'reason', 'gift', 'action_facts') if k in payload}
    if (payload.get('facts') or {}).get('recent_failures'):
        from scripts.live7.prompt_grounding import protected_failures
        base['facts'] = dict(recent_failures=protected_failures(payload['facts']['recent_failures']))
    # Choices ride every section. Their full prose/facts remain in the tree.
    choices = [{k: v for k, v in e.items() if k in ('name', 'args')
                or e.get('name') == 'cek_jurnal' and k in ('jurnal', 'jurnal_baca')}
               for e in payload['skills'] if e['name'] != 'read_boundary']
    base['skills'] = choices
    root = {k: v for k, v in payload.items()
            if k not in ('boundary_page', 'render_budget', 'read_page', 'question_reads_closed')}
    wanted = selected[0]

    def handle(path):
        if not path:
            return root_handle
        return 'section:' + hashlib.sha256(json.dumps([root_handle, path], ensure_ascii=False).encode()).hexdigest()[:16]

    def children(value, path):
        if isinstance(value, dict):
            return [(path + [k], v) for k, v in value.items()]
        if isinstance(value, list):
            return [(path + [i], v) for i, v in enumerate(value)]
        if isinstance(value, str) and len(value) > 1:
            size = max(1, min(4096, ceiling // 4, len(value) // 2))
            leaf = dict(base, detail_page=dict(handle=handle(path), path=path, value=value),
                        skills=choices + [dict(name='read_boundary', args=[root_handle + ':0'])])
            if measure(leaf) > ceiling:
                return [(path + ['text', i // size], value[i:i + size]) for i in range(0, len(value), size)]
        return []

    def nodes(value, path):
        yield path, value, None
        items = children(value, path)
        for start in range(0, len(items), 12):
            group = items[start:start + 12]
            if len(items) > 12:
                yield path + [dict(group=start // 12)], None, group
            for child, part in group:
                yield from nodes(part, child)

    for path, value, group in nodes(root, []):
        if handle(path) != wanted:
            continue
        detail = dict(handle=wanted, path=path)
        back = [] if not path else [root_handle + ':0']

        def page(d, links):
            return dict(base, detail_page=d,
                        skills=choices + ([dict(name='read_boundary', args=back + links)] if back or links else []))

        whole = page(dict(detail, value=value), [])
        if group is None and measure(whole) <= ceiling:
            return whole
        items = group if group is not None else children(value, path)
        grouped = None
        if group is None and len(items) > 12:
            grouped = items
            items = [(path + [dict(group=i // 12)], grouped[i:i + 12]) for i in range(0, len(items), 12)]
        sections = [dict(path=p, arg=handle(p) + ':0') for p, _ in items]
        if grouped is not None:
            for section, (_, members) in zip(sections, items):
                section['contains'] = [p[-1] for p, _ in members]
        result = page(dict(detail, sections=sections,
                           note='read-only; open a section directly; every choice remains here'),
                      [e['arg'] for e in sections])
        if measure(result) <= ceiling:
            return result
        return None  # the ordinary lossless pager handles an extreme choice/header floor
    return None
