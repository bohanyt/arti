"""Jumlah penonton live YouTube → sinyal kehidupan proaktif (spek streamer 2026-08-03).

Naik = ada yang baru masuk → bangunkan Arti (bump jam kehidupan + bahan
sapaan). Turun/stabil = BUKAN tanda kehidupan; digabung jam chat/mic,
5 menit sunyi total = proaktif tidur (arti_curious.is_dormant).

Sumber angka: endpoint innertube `updated_metadata` — jalur yang sama dengan
listener chat (requests polos, tanpa API key resmi / dependensi baru).
"""

from __future__ import annotations

import re
import time
from typing import Callable

DEFAULT_POLL_SEC = 30.0


def parse_viewer_count(data) -> int | None:
    """Angka "watching now" dari respons updated_metadata — pure, testable.

    Bentuk respons (diverifikasi live 2026-08-03): actions[] →
    updateViewershipAction → viewCount → videoViewCountRenderer →
    {isLive, originalViewCount, unlabeledViewCountValue, viewCount
    (runs[]/simpleText "1,625 watching now")}.

    WAJIB cek isLive: video yang sudah TIDAK live tetap dijawab endpoint
    ini, tapi angkanya = TOTAL VIEWS KUMULATIF — tanpa gerbang ini, stream
    berakhir terbaca "penonton melonjak ribuan" dan Arti nyapa hantu.
    Teks ada tapi tanpa digit (mis. "No one watching") = 0. Tidak
    ketemu / bukan live = None.
    """
    if not isinstance(data, dict):
        return None
    for action in data.get("actions") or []:
        try:
            renderer = action["updateViewershipAction"]["viewCount"][
                "videoViewCountRenderer"]
        except (KeyError, TypeError):
            continue
        if not renderer.get("isLive"):
            # Renderer non-live = total views kumulatif; action lain di
            # respons yang sama masih bisa live (audit 3/8: return di sini
            # membutakan telemetri permanen).
            continue
        # Angka bersih lebih dulu (originalViewCount / unlabeled), teks
        # berlabel ("1,625 watching now") sebagai cadangan. 0 penonton valid.
        orig = renderer.get("originalViewCount")
        raw = str(orig) if orig is not None and str(orig).strip() else ""
        if not raw:
            unl = renderer.get("unlabeledViewCountValue")
            if isinstance(unl, dict):
                raw = unl.get("simpleText") or ""
        text = raw
        if not text.strip():
            vc = renderer.get("viewCount")
            if isinstance(vc, dict):
                text = vc.get("simpleText") or "".join(
                    r.get("text", "") for r in vc.get("runs", [])
                    if isinstance(r, dict)
                )
            elif vc is not None:
                text = str(vc)
        n = _text_to_count(text)
        if n is not None:
            return n
    return None


_ABBREV_MULT = {"k": 1_000, "rb": 1_000, "ribu": 1_000,
                "jt": 1_000_000, "juta": 1_000_000, "m": 1_000_000}


def _text_to_count(text: str) -> int | None:
    """Teks angka penonton → int. "1,625" = 1625; "1.6K"/"1,2 rb" dikali
    benar (audit 3/8: strip-non-digit membaca 1.6K sebagai 16 → lonjakan
    palsu); teks tanpa digit ("No one watching") = 0; kosong = None."""
    t = (text or "").strip()
    if not t:
        return None
    m = re.search(
        r"([\d][\d.,]*)\s*(k|rb|ribu|jt|juta|m)\b", t, re.IGNORECASE
    )
    if m:
        num = m.group(1).replace(",", ".")
        try:
            return int(float(num) * _ABBREV_MULT[m.group(2).lower()])
        except ValueError:
            pass
    digits = re.sub(r"[^\d]", "", t)
    if digits:
        return int(digits)
    return 0


def make_innertube_fetch(config: dict) -> Callable[[], "int | None"]:
    """Fetch asli () -> int | None. Error jaringan/parse = None (worker skip)."""
    video_id = (config.get("youtube_video_id") or "").strip()

    def _fetch() -> int | None:
        import requests  # noqa: PLC0415

        try:
            resp = requests.post(
                "https://www.youtube.com/youtubei/v1/updated_metadata?prettyPrint=false",
                json={
                    "context": {"client": {
                        "clientName": "WEB",
                        "clientVersion": "2.20240101.00.00",
                    }},
                    "videoId": video_id,
                },
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0.0.0 Safari/537.36"
                    ),
                },
                timeout=10,
            )
            resp.raise_for_status()
            return parse_viewer_count(resp.json())
        except Exception:  # noqa: BLE001
            return None

    return _fetch


def viewer_count_worker(
    config: dict,
    *,
    fetch_count: Callable[[], "int | None"],
    on_increase: Callable[[int, int], None],
    on_decrease: Callable[[int, int], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Thread telemetri penonton: poll tiap yt_viewer_poll_sec.

    Naik → on_increase(prev, now); turun → on_decrease(prev, now).
    Sampel pertama cuma jadi baseline (belum tahu arah). fetch None = skip
    (video belum live / jaringan) — prev dipertahankan supaya kedip fetch
    tidak dihitung sebagai "naik" palsu.
    """
    poll = float(config.get("yt_viewer_poll_sec", DEFAULT_POLL_SEC))
    prev: int | None = None
    while config.get("youtube_chat_enabled"):
        try:
            count = fetch_count()
        except Exception:  # noqa: BLE001
            count = None
        if count is not None:
            if prev is not None and count > prev:
                try:
                    on_increase(prev, count)
                except Exception:  # noqa: BLE001
                    pass
            elif prev is not None and count < prev and on_decrease is not None:
                try:
                    on_decrease(prev, count)
                except Exception:  # noqa: BLE001
                    pass
            prev = count
        sleep(poll)
