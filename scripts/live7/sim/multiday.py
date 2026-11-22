"""Small multi-day risk fixtures. Effects remain synthetic, never live evidence."""
from .world import World


def bag_full(*, chest=False):
    world = World.spring1(location='Farm', clock=1000, inventory={
        '(O)388': 60, '(O)390': 9, '(O)92': 8, '(O)771': 6, '(O)24': 4, '(O)18': 3, '(O)330': 1})
    world.names.update({'(O)24': ('Parsnip', -75, 35), '(O)18': ('Daffodil', -81, 30)})
    world.places['Farm'].shipping_bin = True
    if chest:
        x, y = world.here.tile
        world.here.chests[(x + 2, y + 2)] = {}
    return world


def bag_full_unshippable():
    """Day-3/4 audit bag: 12/12 with tools and protected resources only, wood above one chest."""
    return World.spring1(location='Farm', clock=1000, inventory={
        '(O)388': 60, '(O)390': 9, '(O)92': 8, '(O)771': 6, '(O)330': 2, '(O)382': 3, '(O)378': 4})


def bag_full_crops():
    """Full bag with seeds, an empty hoed plot and a dry crop: the crop chain must still lead."""
    world = bag_full()
    world.inventory.pop('(O)330')
    world.inventory['(O)472'] = 3
    x, y = world.here.tile
    world.hoe((x - 1, y), crop=dict(phase=0, qualified_item_id='(O)24', dead=False,
        watered=False, ready_for_harvest=False))
    world.hoe((x + 1, y))
    return world


def low_money():
    world = bag_full()
    world.inventory['(O)330'] = 0
    world.money = 20
    return world


def rain_day():
    world = World.spring1(location='Farm', clock=1000, inventory={'(O)472': 3})
    world.date, world.weekday, world.weather = (1, 'spring', 3), 'Wed', 'rain'
    x, y = world.here.tile
    world.hoe((x - 1, y), crop=dict(phase=0, qualified_item_id='(O)24', dead=False,
        watered=True, ready_for_harvest=False))
    world.hoe((x, y + 1), crop=dict(phase=None, watered=True))
    return world


def wet_tiles():
    """live7-game-m2 day 2: five dry day-1 crops two tiles apart on a stale global scene.

    Watering flips each tile in place; only the fresh 3x3 window around the last tool tile shows it."""
    world = World.spring1(location='Farm', clock=1100, inventory={})
    world.date, world.weekday = (1, 'spring', 2), 'Tue'
    world.stale_scene = True
    x, y = world.here.tile
    world.hoe(*[(x + 3 + 2 * i, y + 3) for i in range(5)], crop=dict(phase=1, qualified_item_id='(O)24', dead=False,
              watered=False, ready_for_harvest=False))
    world.take_snapshot()
    return world


def out_of_view_crops(offset=0):
    """M4 Forest gap: own five day-1 plantings, far from the next morning's Farm arrival."""
    world = World.spring1(location='Farm', clock=1000, inventory={'(O)472': 5})
    farm = world.here
    x, y = farm.tile
    x += offset
    farm.tile = (x, y)
    farm.radius = 5
    farm.debris.clear()
    farm.trees = farm.stones = 0
    farm.areas['morning'] = (x-28, y)
    farm.arrivals['Cabin'] = 'morning'
    world.navigation_radius = 20
    world.hoe((x-1, y), (x+1, y), (x-1, y+1), (x, y+1), (x+1, y+1))
    return world


def wednesday_no_seeds(location='Farm', clock=850):
    """live7-game-m2 day 3: Wednesday, no seeds, 300g; Pierre's is closed all day."""
    world = World.spring1(location=location, clock=clock, inventory={}, money=300)
    world.date, world.weekday = (1, 'spring', 3), 'Wed'
    return world
