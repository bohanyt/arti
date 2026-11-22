"""Read-only proof-save extraction; no game/transport imports or gameplay inputs.

Save occupancy is evidence, not a native collision capture. The fixture's grid
is a unit double whose occupied cells come from the retained XML slices.
"""
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def extract(source, bounds):
    raw = source.read_bytes()
    root = ET.fromstring(raw)
    farm = next(f for f in root.iter('GameLocation') if f.findtext('name') == 'Farm')
    records = []
    for collection, tag in [('objects', 'Object'), ('terrainFeatures', 'TerrainFeature')]:
        for entry in farm.find(collection):
            key = entry.find('key/Vector2')
            x, y = (int(float(key.findtext(k))) for k in ('X', 'Y'))
            if bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]:
                value = entry.find('value/' + tag)
                records.append(dict(collection=collection, tile=[x, y], xml=ET.tostring(value, encoding='unicode')))
    clumps = [ET.tostring(c, encoding='unicode') for c in farm.findall('resourceClumps/ResourceClump')]
    buildings = [{k: b.findtext(k) for k in ('buildingType', 'tileX', 'tileY', 'tilesWide', 'tilesHigh', 'humanDoor')}
                 for b in farm.findall('buildings/Building')]
    tools = [{k: i.findtext(k) for k in ('name', 'itemId', 'upgradeLevel')}
             for f in root.iter('Farmer') if f.findtext('name') == 'artihand'
             for i in f.findall('items/Item') if i.find('upgradeLevel') is not None]
    facts = {}
    grid = {(x, y): '.' for x in range(bounds[0], bounds[2]+1) for y in range(bounds[1], bounds[3]+1)}
    for r in records:
        value = ET.fromstring(r['xml'])
        x, y = r['tile']
        tile = dict(x=x, y=y)
        if r['collection'] == 'objects':
            ref = f"obj:Farm:{x},{y}"
            facts[ref] = dict(kind='object', name=value.findtext('name'), tile=tile,
                              qualified_item_id='(O)' + value.findtext('itemId'),
                              big_craftable=value.findtext('bigCraftable') == 'true',
                              spawned=value.findtext('isSpawnedObject') == 'true')
            grid[x, y] = '#'
        else:
            kind = value.get('{http://www.w3.org/2001/XMLSchema-instance}type')
            ref = f"tf:Farm:{x},{y}:{kind}"
            fact = facts[ref] = dict(kind='terrain', terrain_type=kind, tile=tile)
            if kind == 'Tree':
                fact['wild_tree'] = dict(growth_stage=int(value.findtext('growthStage')),
                                        tapped=value.findtext('tapped') == 'true',
                                        stump=value.findtext('stump') == 'true')
                if fact['wild_tree']['growth_stage'] >= 3:
                    grid[x, y] = '#'
            elif kind == 'HoeDirt' and value.find('crop') is not None:
                fact['crop'] = dict(phase=value.findtext('crop/currentPhase'))
    for b in buildings:
        x, y, w, h = (int(b[k]) for k in ('tileX', 'tileY', 'tilesWide', 'tilesHigh'))
        facts['building:' + b['buildingType']] = dict(kind='building', name=b['buildingType'],
                                                      tile=dict(x=x, y=y), width=w, height=h)
        for dx in range(w):
            for dy in range(h):
                if (x+dx, y+dy) in grid:
                    grid[x+dx, y+dy] = '#'
    return dict(source_sha256=hashlib.sha256(raw).hexdigest(), whichFarm=root.findtext('whichFarm'),
                bounds=list(bounds), records=records, resource_clumps_xml=clumps, buildings=buildings, tools=tools,
                facts=facts, cells=[[x, y, c] for (x, y), c in sorted(grid.items())],
                grid_evidence='UNIT DOUBLE: save occupancy on assumed open ground; not native collision')


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('source', type=Path)
    p.add_argument('output', type=Path)
    p.add_argument('--bounds', type=int, nargs=4, required=True)
    a = p.parse_args()
    result = extract(a.source, a.bounds)
    a.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(sha256=result['source_sha256'], records=len(result['records']),
                         objects=[(r['tile'], ET.fromstring(r['xml']).findtext('name'))
                                  for r in result['records'] if r['collection'] == 'objects']), indent=2))
