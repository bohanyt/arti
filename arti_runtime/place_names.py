"""Native place ids in the words speech and her page share (ADR 110). No tile, no coordinate, no new read."""
from arti_runtime.native_rows import native_rows
import re

TEMP_NAME = "peta acara sementara"  # Temp exists only while the game runs an event on a temporary map
_INSTANCE = re.compile(r"\A(Cabin|FarmHouse)")
_MINE = re.compile(r"\AUndergroundMine(\d+)\Z")


def place_label(place):
    """``toko Pierre (SeedShop)``: the existing readable name, native id kept; None for a place with no name."""
    if not isinstance(place, str) or not place:
        return None
    if place == "Temp":
        return "%s (Temp)" % TEMP_NAME
    if mine := _MINE.match(place):
        return "tambang lantai %s (%s)" % (mine.group(1), place)
    from arti_stardew_initiative import _LOCATIONS
    instance = _INSTANCE.match(place)
    if instance and place != instance.group(1):
        return "rumah/kabin (pemilik belum diketahui)"
    native = instance.group(1) if instance else place
    name = _LOCATIONS.get(native)
    return "%s (%s)" % (name.removeprefix("di "), native) if name else None


def source_place(place, catalog=None):
    """Native ownership and display label from the event's own world read."""
    from arti_runtime.fact_names import display_name
    catalog = catalog or {}
    rest = catalog.get("rest") or {}
    if place and place == rest.get("home"):
        return "rumahmu" if place == "FarmHouse" else "kabinmu"
    for row in native_rows((catalog.get("world_graph") or {}).get("places", ())):
        if row.get("id") == place and (name := display_name(row.get("label"))) and name != place:
            return name
    return place_label(place) or "tempat (nama belum diketahui)"


def festival_today(calendar, today):
    """The name of a native festival due on ``today``, from ``calendar_context`` rows, or None."""
    if not isinstance(today, str):
        return None
    for row in calendar if isinstance(calendar, (list, tuple)) else ():
        if (isinstance(row, dict) and row.get("kind") == "festival" and isinstance(row.get("name"), str)
                and "%s; today %s: today" % (today, today) in str(row.get("date_fact"))):
            return row["name"]
    return None


def current_place_label(place, calendar=None, today=None):
    """``place_label``, except that Temp on a native festival day is that festival. Unknown stays None."""
    if place == "Temp" and (festival := festival_today(calendar, today)):
        return "%s, kamu sedang di festival ini (peta Temp)" % festival
    return place_label(place)
