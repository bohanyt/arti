"""W1 gap B4 (CT 2026-10-19, flagged for owner veto): live 8d accepted quest 117, then exhausted 160/160 inputs at the
BusStop before reaching Pierre. Only the owner-named S28 delivery alone gets the 260-input / 900 s route envelope."""
import sys
from pathlib import Path

import arti_stardew_skills as S

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import stardew_m1_trial as T  # noqa: E402

GOAL = "Arti, main mandiri: surat_quest sashimi ke pierre"


def test_owner_named_s28_delivery_gets_the_route_envelope():
    assert S.skill_costs(S.SkillCall("surat_quest", "sashimi ke pierre")) == S.QUEST_DELIVERY_COSTS == (260, 260, 900)
    assert T.input_approval_max(GOAL) == 260 and T.seconds_approval_max(GOAL) == 900


def test_bare_s28_and_other_skills_keep_their_caps():
    assert S.skill_costs(S.SkillCall("surat_quest")) != S.QUEST_DELIVERY_COSTS
    assert T.input_approval_max("Arti, main mandiri: crab_pot ambil") == 200
    assert T.input_approval_max("Arti, main mandiri: upgrade_alat watering can") == 160
