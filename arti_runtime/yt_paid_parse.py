"""Parse YouTube paid messages and memberships from live chat items."""


def extract_membership(item: dict) -> dict | None:
    """YouTube membership / gifted membership from one innertube item — pure.

    Super Chat and Super Sticker stay in extract_superchat. Same DonationEvent
    kind as parse_streamlabs_membership (platform=youtube, kind=membership).
    """
    import arti_donations  # noqa: PLC0415

    evs = arti_donations.parse_youtube_membership_item(item)
    if not evs:
        return None
    ev = evs[0]
    raw = ev.raw if isinstance(ev.raw, dict) else {}
    return {
        "name": ev.name,
        "message": ev.message,
        "membership_months": ev.membership_months,
        "membership_level": ev.membership_level,
        "renderer_key": raw.get("_renderer") or "",
        "id": raw.get("id") or "",
    }


def extract_superchat(item: dict) -> dict | None:
    """Data Super Chat / paid sticker dari satu item innertube — pure, testable.

    D0 rencana v0.7: `purchaseAmountText` selama ini DIBUANG oleh parse_action
    padahal datanya sudah mengalir. Return {'name','message','paid_amount'}
    atau None kalau bukan item berbayar.
    """
    for key in ("liveChatPaidMessageRenderer", "liveChatPaidStickerRenderer"):
        r = item.get(key)
        if not r:
            continue
        amount = ((r.get("purchaseAmountText") or {}).get("simpleText") or "").strip()
        author = ((r.get("authorName") or {}).get("simpleText") or "Unknown").strip()
        runs = (r.get("message") or {}).get("runs", [])
        msg = "".join(x.get("text", "") for x in runs).strip()
        return {"name": author, "message": msg, "paid_amount": amount}
    return None
