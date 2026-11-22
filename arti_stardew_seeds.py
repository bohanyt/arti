"""Crop seed and growth tables: a stub without data in this public tree.

The working repository fills these tables from the installed game's own content files (crop seasons, growth days,
seed-to-crop pairs). That is the game's data, so it is not published. With empty tables every lookup below answers
"unknown" (None, an empty tuple or a placeholder name), and the runtime falls back to the native facts the mod reads
from the running game. Fill the tables from your own game files if you want the offline estimates back.
"""

SEASONS = ("spring", "summer", "fall", "winter")
# qid: (internal English item name, seasons the crop grows in)
CROP_SEEDS = {}

# harvest item qid: (English name, total growth days)
CROP_DATA = {}

# seed qid -> the harvest item CROP_DATA keys its days by
SEED_CROP = {}


def seed_in_season(qid, season):
    """True/False for a known crop and season; None when native data is required."""
    row = CROP_SEEDS.get(qid)
    return season in row[1] if row is not None and season in SEASONS else None


def owned_crop_seeds(owned, season=None):
    """Known crop seeds present by qid or exact internal name (legacy hotbar facts)."""
    return tuple(qid for qid, (name, seasons) in CROP_SEEDS.items()
                 if (qid in owned or name in owned) and (season is None or season in seasons))


def crop_seasons(crop_qid):
    """The seasons a harvest item's crop lives in, or None when no seed row names it."""
    for seed, crop in SEED_CROP.items():
        if crop == crop_qid:
            return CROP_SEEDS[seed][1]
    return None


def crop_name(qid):
    """Known crop name; an unknown native kind is never printed as an ID."""
    row = CROP_DATA.get(qid)
    return row[0] if row else "tanaman (nama belum diketahui)"


def crop_grow_days(qid):
    """Total growth days for a known crop, else None."""
    row = CROP_DATA.get(qid)
    return row[1] if row else None
