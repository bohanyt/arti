"""Round-trip displayed item vocabulary through the boundary's native calls."""
import re
from collections import Counter
from dataclasses import replace

from arti_runtime.choice_contract import folded


def display_labels(rows):
    """Qualify a shared name by the observed stack and quality, then slot."""
    identities = {(row['name'], row['item']) for row in rows}
    counts = Counter(folded(name) for name, _ in identities)
    labels = []
    for row in rows:
        name = row['name']
        quality, count = row.get('quality'), row.get('count')
        details = [f"kualitas {quality}"] if type(quality) is int else []
        if type(count) is int and count > 0:
            details.append(f"×{count}")
        labels.append(f"{name} ({' '.join(details)})" if counts[folded(name)] > 1 and details else name)
    duplicates = Counter(folded(label) for label in labels)
    return [f"{label} (slot {row['slot'] + 1})" if duplicates[folded(label)] > 1
            and type(row.get('slot')) is int and counts[folded(row['name'])] > 1 else label
            for row, label in zip(rows, labels)]


def present(offer):
    """Replace item args, retaining the reverse mapping solely on the request."""
    aliases = {}
    for entry in offer.get('skills', ()):
        native = entry.pop('_item_names', {})
        if not native:
            continue
        rows = [dict(item=arg, **native[arg]) for arg in entry.get('args', ())
                if arg in native and isinstance(native[arg].get('name'), str) and native[arg]['name']]
        labels = display_labels(rows)
        translated = {row['item']: label for row, label in zip(rows, labels)
                      if labels.count(label) == 1}
        args = entry.get('args', ())
        entry['args'] = [translated.get(arg, arg) for arg in args if arg not in native or arg in translated]
        aliases[entry['name']] = {label: arg for arg, label in translated.items() if label != arg}
        for key in ('nama', 'recipes'):
            if key in entry:
                entry[key] = {translated.get(arg, arg): value for arg, value in entry[key].items()
                              if arg not in native or arg in translated}
        if 'items' in entry:
            entry['items'] = [dict(item, arg=translated.get(item['arg'], item['arg'])) for item in entry['items']
                              if item['arg'] not in native or item['arg'] in translated]
    return aliases


def purchase_aliases(facts, actions):
    """Name a quantity click from that receiver's visible stock row."""
    from collections.abc import Mapping
    from arti_runtime.native_rows import native_rows

    shop = facts.get('shop')
    if not isinstance(shop, Mapping):
        return {}
    stock = []
    for slot, row in enumerate(native_rows(shop.get('stock'))):
        if (isinstance(row, (list, tuple)) and len(row) >= 9 and type(row[8]) is int
                and isinstance(row[2], str) and row[2] and isinstance(row[1], str) and row[2] != row[1]):
            stock.append(dict(name=row[2], item=row[1], count=row[5], slot=slot, control=row[8]))
    controls = Counter(row['control'] for row in stock)
    by_control = {str(row['control']): label for row, label in zip(stock, display_labels(stock))
                  if controls[row['control']] == 1}
    offered = []
    for key in actions:
        if not isinstance(key, str) or not key.startswith('buy:'):
            continue
        _, _, pair = key.partition(':')
        control, _, count = pair.partition(':')
        if control in by_control and count.isdecimal():
            offered.append((f"buy: {by_control[control]} ×{count}", key))
    counts = Counter(folded(label) for label, _ in offered)
    return {label: key for label, key in offered if counts[folded(label)] == 1}


NATIVE_BUY = re.compile(r'buy:\d+:\d+')  # Native stock ID and a whole requested quantity.


def native_reply(decision, aliases):
    """Displayed item words back to the request's native args. ADR 252 (summer 4, 14 of 14 purchases refused): a native
    `buy:N:Q` the page itself listed (its `shop.buy_actions`, her retry feedback) is the arg, unchanged; only a displayed
    stock label is translated. The shop boundary resolves the stock control and the native presses for the requested
    count. An unknown label still names no call."""
    def translate(name, arg):
        if not isinstance(arg, str) or name not in aliases:
            return arg
        by_name = {folded(label): native for label, native in aliases[name].items()}
        if name == 'ui_action' and arg.casefold().startswith('buy:'):
            if folded(arg) in by_name:
                return by_name[folded(arg)]
            return arg.strip() if NATIVE_BUY.fullmatch(arg.strip()) else 'unknown_item_choice'
        if name == 'simpan_ke_chest':
            return ';'.join(by_name.get(folded(part), part.strip()) for part in arg.split(';'))
        return by_name.get(folded(arg), arg)

    choice = decision.choice
    args = getattr(choice, 'args', None)
    if not aliases or not isinstance(args, dict) or 'arg' not in args:
        return decision
    plan = tuple(dict(step, arg=translate(step['name'], step['arg']),
                      **({'presses': [translate(step['name'], arg) for arg in step['presses']]}
                         if step.get('presses') else {})) for step in decision.plan)
    return replace(decision, choice=replace(choice, args=dict(args, arg=translate(args.get('name'), args['arg']))),
                   plan=plan)
