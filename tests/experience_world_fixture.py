"""Explicit prior-visit evidence for known-world replay fixtures (ADR 178)."""
from arti_stardew_interact import _LOCATION_ALIASES


def prior_visits(runtime, *destinations):
    catalog = runtime.ctx['world_knowledge']
    home = catalog['world_graph'].get('home')
    rows = list(catalog.get('experience_records') or ())
    for destination in destinations:
        place = home if destination == 'home' else _LOCATION_ALIASES.get(destination, destination.removeprefix('@'))
        rows.append(dict(kind='location_entered', subject=place, source='fixture:prior_personal_visit'))
    catalog['experience_records'] = rows
    return runtime


def book_read(catalog):
    return dict(catalog, experience_records=list(catalog.get('experience_records') or ()) +
                [dict(kind='system_read', subject='cc', source='fixture:readable_bundle_book')])
