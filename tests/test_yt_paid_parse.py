"""Golden paid-chat parser behavior captured from the bridge before the split."""

import ast
from pathlib import Path

import pytest

from arti_runtime import yt_paid_parse


def test_bridge_reexports_the_same_parsers():
    import arti_bridge

    assert arti_bridge.extract_membership is yt_paid_parse.extract_membership
    assert arti_bridge.extract_superchat is yt_paid_parse.extract_superchat


def test_paid_parsers_do_not_import_the_bridge():
    source = Path(yt_paid_parse.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            assert "arti_bridge" not in (node.module or "").split(".")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            assert all("arti_bridge" not in alias.name.split(".") for alias in node.names)


def test_superchat_joins_text_runs_and_preserves_the_formatted_amount():
    item = {
        "liveChatPaidMessageRenderer": {
            "authorName": {"simpleText": " @donor "},
            "purchaseAmountText": {"simpleText": " Rp 42.500 "},
            "message": {"runs": [{"text": " Semangat "}, {"emoji": {}}, {"text": "ya! "}]},
        },
    }
    assert yt_paid_parse.extract_superchat(item) == {
        "name": "@donor",
        "message": "Semangat ya!",
        "paid_amount": "Rp 42.500",
    }
    assert yt_paid_parse.extract_membership(item) is None


def test_super_sticker_without_author_or_message():
    item = {"liveChatPaidStickerRenderer": {"purchaseAmountText": {"simpleText": " ¥500 "}}}
    assert yt_paid_parse.extract_superchat(item) == {
        "name": "Unknown",
        "message": "",
        "paid_amount": "¥500",
    }
    assert yt_paid_parse.extract_membership(item) is None


def test_membership_keeps_renderer_id_level_and_months():
    item = {
        "liveChatMembershipItemRenderer": {
            "id": "member-1",
            "authorName": {"simpleText": "Viewer Aman"},
            "headerPrimaryText": {"runs": [{"text": "Member for "}, {"text": "7 months"}]},
            "headerSubtext": {"simpleText": "Kopi"},
            "message": {"runs": [{"text": "Halo!"}]},
        },
    }
    assert yt_paid_parse.extract_membership(item) == {
        "name": "Viewer Aman",
        "message": "Halo!",
        "membership_months": 7,
        "membership_level": "Kopi",
        "renderer_key": "liveChatMembershipItemRenderer",
        "id": "member-1",
    }
    assert yt_paid_parse.extract_superchat(item) is None


def test_gift_purchase_uses_the_nested_header_and_defaults():
    item = {
        "liveChatSponsorshipsGiftPurchaseAnnouncementRenderer": {
            "header": {
                "liveChatSponsorshipsHeaderRenderer": {
                    "authorName": {"simpleText": "Gifter"},
                    "primaryText": {"runs": [{"text": "sent 5 Gift memberships"}]},
                },
            },
        },
    }
    assert yt_paid_parse.extract_membership(item) == {
        "name": "Gifter",
        "message": "sent 5 Gift memberships",
        "membership_months": 0,
        "membership_level": "",
        "renderer_key": "liveChatSponsorshipsGiftPurchaseAnnouncementRenderer",
        "id": "",
    }
    assert yt_paid_parse.extract_superchat(item) is None


@pytest.mark.parametrize(
    "item",
    [
        {"liveChatTextMessageRenderer": {"message": {"runs": [{"text": "Halo!"}]}}},
        {"liveChatMembershipItemRenderer": ["malformed"]},
    ],
)
def test_ordinary_chat_and_malformed_memberships_are_not_paid_events(item):
    assert yt_paid_parse.extract_membership(item) is None
    assert yt_paid_parse.extract_superchat(item) is None
