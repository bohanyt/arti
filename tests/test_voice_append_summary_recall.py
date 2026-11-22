"""ADR 200 fact summaries and session-only recall; synthetic data, no providers."""
from dataclasses import replace

import pytest

from arti_runtime import voice_context as vc


def parts(log, request="laundry berapa kilo?"):
    sid, lines = log.snapshot()
    return vc.VoiceContextParts("STABLE", {}, lines, request, 0, sid)


def seeded():
    log = vc.SessionLog()
    log.append("Viewer Contoh1", "laundry 7 kilo, ambil Jumat", "10:01:02")
    log.append("Arti", "Aku janji bantu thumbnail besok", "10:02:03", spoken=True)
    value = parts(log)
    cp = vc.Checkpoint(value.session_id, value.lines[-1].seq, "- fakta tersimpan", 0)
    log.append("Streamer", "sekarang memancing", "10:03:04")
    return log, cp


@pytest.mark.parametrize("warm", [False, True])
def test_each_request_recalls_only_missing_prefix_with_names_and_clocks(warm):
    log, cp = seeded()
    ctx = vc.ThreadContext()
    if warm:
        value = parts(log)
        warmup = ctx.compose_warmup(value.stable, value.session_id, value.lines, cp)
        assert vc.EARLIER_HEADER not in warmup  # no request to retrieve for yet
    message = ctx.compose(parts(log), cp)
    assert vc.EARLIER_HEADER in message
    assert "[10:01:02] [Viewer Contoh1] laundry 7 kilo" in message
    assert message.index(vc.EARLIER_HEADER) < message.index("laundry berapa kilo?")
    second = ctx.compose(parts(log, "thumbnail kapan?"))
    assert vc.EARLIER_HEADER in second and "[10:02:03] [Arti]" in second
    assert vc.LINES_HEADER_NEXT not in second
    absent = ctx.compose(parts(log, "zebracorn"))
    assert vc.EARLIER_HEADER not in absent and "tidak berlaku lagi" not in absent


def test_no_retrieval_when_thread_holds_all_lines_even_with_a_new_checkpoint():
    log, cp = seeded()
    ctx = vc.ThreadContext()
    ctx.compose(parts(log))
    assert vc.EARLIER_HEADER not in ctx.compose(parts(log), cp)
    assert ctx.earlier_upto_seq == 0


def test_retrieval_never_crosses_privacy_boundary_or_uses_stale_checkpoint():
    log, cp = seeded()
    old = parts(log)
    ctx = vc.ThreadContext()
    ctx.compose(old, cp)
    log.clear()
    log.append("Viewer Contoh2", "laundry 2 kilo", "11:00:00")
    new = parts(log)
    assert not ctx.compatible(new)
    assert vc.retrieve_earlier(new, ctx.session_id, cp.upto_seq) == []
    replacement = vc.ThreadContext()
    message = replacement.compose(new, cp)
    assert "Contoh1" not in message and "7 kilo" not in message
    assert vc.EARLIER_HEADER not in message and vc.SUMMARY_HEADER not in message
    # Even a fresh checkpoint after the boundary can retrieve only the new snapshot.
    cp2 = vc.Checkpoint(new.session_id, new.lines[-1].seq, "- baru", 1)
    fresh = vc.ThreadContext().compose(replace(new, privacy_epoch=1), cp2)
    assert "2 kilo" in fresh and "Contoh1" not in fresh


def test_retrieval_is_positive_bm25_bounded_and_ignores_post_checkpoint_lines():
    log, cp = seeded()
    log.append("Streamer", "laundry " * 250, "10:04:05")
    value = parts(log)
    hits = vc.retrieve_earlier(value, value.session_id, cp.upto_seq, k=99)
    assert 0 < len(hits) <= 2 and all(len(hit) <= 420 for hit in hits)
    assert all("10:04:05" not in hit and "10:03:04" not in hit for hit in hits)
    assert vc.retrieve_earlier(value, value.session_id, cp.upto_seq, k=0) == []
    assert vc.retrieve_earlier(replace(value, request="zebracorn"), value.session_id, cp.upto_seq) == []


def test_long_line_chunks_keep_attribution_and_exact_character_bound():
    log = vc.SessionLog()
    line = log.append("Viewer Contoh1", "kata " * 200 + "laundry 7 kilo", "10:00:00")
    value = parts(log)
    hits = vc.retrieve_earlier(value, value.session_id, line.seq)
    assert hits and "laundry 7 kilo" in hits[0]
    assert all(hit.startswith("[10:00:00] [Viewer Contoh1]") and len(hit) <= 420 for hit in hits)


def test_summary_prompt_requests_atomic_attributed_facts_and_preserves_old_details():
    log, cp = seeded()
    prompt = vc.summary_prompt(cp, log.snapshot()[1][2:])
    for field in ("Bahasa Indonesia", "baris pendek", "HH:MM:SS", "siapa bilang apa", "nama",
                  "tempat", "benda", "angka", "rencana", "laundry", "thumbnail", "janji",
                  "pertanyaan", "ringkasan sebelumnya", "Jangan mengarang", "[terpotong]",
                  str(vc.SUMMARY_MAX_CHARS), str(vc.SUMMARY_MAX_LINES)):
        assert field in prompt
    assert cp.summary in prompt and "sekarang memancing" in prompt


def test_summary_render_is_bounded_even_if_model_ignores_instruction():
    log, cp = seeded()
    cp = replace(cp, summary="\n".join("- synthetic " * 300 for _ in range(200)))
    summary = vc.render_summary(cp.summary)
    assert len(summary) <= vc.SUMMARY_MAX_CHARS
    assert len(summary.splitlines()) <= vc.SUMMARY_MAX_LINES
    assert summary in vc.ThreadContext().compose(parts(log), cp)
    assert summary in vc.summary_prompt(cp, [])
