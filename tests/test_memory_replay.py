"""Memory replay evidence is synthetic and mocked; no private files or API calls."""
import json
from decimal import Decimal

import pytest

from scripts.livepath import memory_replay as replay


def event(t, text, kind="owner", who="synthetic"):
    return replay.Event(t, kind, who, text)


def question(age=3600, qid="q1"):
    return replay.Question(qid, 4000, "Which mineral was stored?", "amethyst", 4000 - age,
                           ("purple crystal",))


class FakeModel:
    def __init__(self, text="Amethyst.", usage=None, error=None):
        self.calls = []
        self.text = text
        self.usage = usage or {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 20},
                               "output_tokens": 30}
        self.error = error

    def __call__(self, body):
        self.calls.append(body)
        if self.error:
            raise self.error
        return {"status": "completed", "usage": self.usage,
                "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": self.text}]}]}


def test_today_keeps_exact_configured_last_n_and_no_future():
    events = [event(i, f"{kind}-marker-{i}", kind)
              for i in range(5) for kind in ("owner", "chat", "reply")]
    events.append(event(100, "future-secret"))
    cfg = replay.Config(streamer_max=2, viewer_max=1, arti_max=3, rag_k=0)
    context = replay.build_today(events, 5, "stable", "query", cfg)
    for kind, count in (("owner", 2), ("chat", 1), ("reply", 3)):
        for i in range(5):
            assert context.count(f"{kind}-marker-{i}") == int(i >= 5 - count)
    assert "future-secret" not in context
    assert context.count("stable") == 1
    assert replay.build_today(events, 5, "stable", "query", replay.Config(
        streamer_max=0, viewer_max=0, arti_max=0, rag_k=0)) == "stable"


def test_append_every_event_once_in_order_including_moments_and_donations():
    events = [event(i * 61, f"unique-{i}", kind, None if i == 0 else "speaker")
              for i, kind in enumerate(("owner", "chat", "reply", "moment", "donation"))]
    events.append(event(999, "future"))
    context = replay.build_append(events, 300, "stable")
    expected = ["stable"] + [replay.format_event(e) for e in events[:-1]]
    assert context.splitlines() == expected
    assert expected[1] == "[00:00] owner null: unique-0"
    assert "[01:01] chat speaker: unique-1" in context
    assert "future" not in context


def test_hour_old_fact_only_in_append_unless_retrieved():
    events = [event(0, "The mineral amethyst was stored in a chest.")]
    events += [event(3500 + i, f"Routine filler {i}.") for i in range(5)]
    events.append(event(3700, "The mineral diamond was stored later."))
    assert "amethyst" in replay.build_append(events, 3600, "stable")
    assert "amethyst" not in replay.build_today(events, 3600, "stable", "mineral", replay.Config(rag_k=0))
    retrieved = replay.build_today(events, 3600, "stable", "mineral", replay.Config(rag_k=2))
    assert "amethyst" in retrieved
    assert "diamond" not in retrieved
    assert replay.retrieve_chunks(events[:1], "unmatchedword", 2) == []


def test_bm25_chunks_are_bounded_and_ranking_is_deterministic():
    events = [event(0, "filler " * 120), event(1, "Rare amethyst mineral in chest.")]
    first = replay.retrieve_chunks(events, "amethyst mineral", 1)
    assert len(first) == 1 and "amethyst" in first[0]
    assert len(first[0]) <= 420
    assert first == replay.retrieve_chunks(events, "amethyst mineral", 1)
    assert all(len(chunk) <= 420 for chunk in replay.timeline_chunks(events))


@pytest.mark.parametrize("reply,answer,aliases,expected", [
    ("AMETHYST!", "amethyst", (), "correct"),
    ("A purple-crystal.", "amethyst", ("purple crystal",), "correct"),
    ("I stored diamond.", "amethyst", (), "wrong"),
    ("tidak tahu", "amethyst", (), "unknown"),
    ("Aku nggak ingat.", "amethyst", (), "unknown"),
    ("I don't know.", "amethyst", (), "unknown"),
    ("I do not remember.", "amethyst", (), "unknown"),
    ("I don't know why, but amethyst.", "amethyst", (), "correct"),
    ("The cart is empty.", "art", (), "wrong"),
])
def test_scoring(reply, answer, aliases, expected):
    assert replay.score_reply(reply, answer, aliases) == expected


@pytest.mark.parametrize("seconds,bucket", [(600, 10), (1800, 30), (3600, 60),
                                                         (1199, 10), (1201, 30), (2701, 60)])
def test_age_buckets(seconds, bucket):
    assert replay.age_bucket(seconds) == bucket


def test_cap_zero_stops_all_calls():
    model = FakeModel()
    result = replay.run_replay([], [question()], "stable", replay.Config(cap_usd=Decimal(0)), model)
    assert model.calls == []
    assert result["stopped"] == "cap"
    assert {row["status"] for row in result["results"]} == {"cap"}
    assert result["summary"]["today"]["overall"]["recall_score"] is None


def test_cap_reserves_full_request_including_output_and_is_shared_between_builds():
    q = question()
    body = replay.request_body("stable", q.question)
    reserve = replay.reserve_usd(body)
    model = FakeModel(usage={"input_tokens": 0, "output_tokens": 256})
    actual = replay.usage_cost(model.usage)
    cap = reserve + actual / 2
    result = replay.run_replay([], [q], "stable", replay.Config(cap_usd=cap), model)
    assert len(model.calls) == 1
    assert [r["status"] for r in result["results"]] == ["completed", "cap"]
    assert Decimal(str(result["spend_usd"])) == actual
    assert replay.reserve_usd(replay.request_body("stable" * 1000, q.question)) > reserve


def test_unknown_billing_stops_and_retains_reservation_without_scoring():
    model = FakeModel(error=RuntimeError("private payload must never escape"))
    result = replay.run_replay([], [question()], "stable", replay.Config(), model)
    assert len(model.calls) == 1
    assert result["stopped"] == "model_error"
    assert result["uncertain_usd"] > 0
    assert [r["verdict"] for r in result["results"]] == [None, None]
    assert "private payload" not in json.dumps(result)


def test_model_settings_usage_discount_summary_scores_and_ages():
    model = FakeModel()
    qs = [question(age, f"q-{age}") for age in (600, 1800, 3600)]
    result = replay.run_replay([], qs, "stable", replay.Config(), model)
    assert len(model.calls) == 6
    for body in model.calls:
        assert body["model"] == "gpt-6-luna"
        assert body["reasoning"] == {"effort": "low"}
        assert body["service_tier"] == "priority"
        assert body["store"] is False
        assert body["max_output_tokens"] == 256
        assert "tidak tahu" in body["instructions"]
        assert "amethyst" not in json.dumps(body)
    assert replay.usage_cost(model.usage) == Decimal("0.0000464")
    for build in ("today", "append"):
        summary = result["summary"][build]
        assert summary["overall"]["correct"] == 3
        assert summary["overall"]["recall_score"] == 1.0
        assert summary["overall"]["median_latency_sec"] >= 0
        for age in ("10", "30", "60"):
            assert summary["age_buckets_min"][age]["correct"] == 1


def test_dry_run_does_not_call_and_output_contains_no_text():
    model = FakeModel()
    q = question()
    events = [event(1, "CONTEXT_SENTINEL")]
    result = replay.run_replay(events, [q], "STABLE_SENTINEL", replay.Config(dry_run=True), model)
    assert model.calls == []
    serialized = json.dumps(result)
    for secret in (q.question, q.answer, *q.aliases, "CONTEXT_SENTINEL", "STABLE_SENTINEL"):
        assert secret not in serialized
    assert all(r["context_chars"] > 0 and r["context_estimated_tokens"] > 0
               and r["latency_sec"] is None and r["verdict"] is None for r in result["results"])
    assert result["spend_usd"] == 0


def test_completed_output_is_text_free_and_summaries_count_all_verdicts():
    replies = iter(["amethyst", "amethyst", "diamond", "diamond", "tidak tahu", "tidak tahu"])
    def model(body):
        return FakeModel(next(replies))(body)
    qs = [question(age, f"q-{age}") for age in (600, 1800, 3600)]
    result = replay.run_replay([event(0, "PRIVATE_CONTEXT_SENTINEL")], qs,
                               "PRIVATE_STABLE_SENTINEL", replay.Config(), model)
    for build in ("today", "append"):
        summary = result["summary"][build]
        assert summary["overall"]["correct"] == 1
        assert summary["overall"]["wrong"] == 1
        assert summary["overall"]["unknown"] == 1
        assert summary["overall"]["recall_score"] == pytest.approx(1 / 3)
        assert summary["age_buckets_min"]["30"]["wrong"] == 1
        assert summary["age_buckets_min"]["60"]["unknown"] == 1
    serialized = json.dumps(result)
    for text in (qs[0].question, qs[0].answer, *qs[0].aliases, "diamond", "tidak tahu",
                 "PRIVATE_CONTEXT_SENTINEL", "PRIVATE_STABLE_SENTINEL"):
        assert text not in serialized


@pytest.mark.parametrize("response", [
    {"status": "completed", "usage": {"input_tokens": -1, "output_tokens": 1}},
    {"status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1,
                                        "input_tokens_details": {"cached_tokens": 2}}},
    {"status": "completed", "usage": None},
])
def test_bad_usage_retains_reservation_and_stops_without_free_calls(response):
    calls = []
    def model(body):
        calls.append(body)
        return response
    result = replay.run_replay([], [question()], "stable", replay.Config(), model)
    assert len(calls) == 1
    assert result["stopped"] == "model_error"
    assert result["uncertain_usd"] == result["results"][0]["reserved_usd"]
    assert result["summary"]["today"]["overall"]["evaluated"] == 0


def test_transport_uses_responses_and_only_in_process_key(monkeypatch, capsys):
    import io

    requests = []
    class Opener:
        def open(self, request, timeout):
            requests.append((request, timeout))
            return io.BytesIO(b'{"status":"completed"}')
    monkeypatch.setattr(replay, "read_api_key", lambda: "synthetic-secret")
    monkeypatch.setattr(replay.urllib.request, "build_opener", lambda handler: Opener())
    body = replay.request_body("synthetic context", "synthetic question")
    assert replay.call_model(body) == {"status": "completed"}
    request, timeout = requests[0]
    assert request.full_url == "https://api.openai.com/v1/responses"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer synthetic-secret"
    assert json.loads(request.data) == body
    assert timeout == 60
    assert "synthetic-secret" not in capsys.readouterr().out
    # Do not forward the key to any redirect target.
    assert replay._NoRedirect().redirect_request(
        request, None, 302, "redirect", {}, "https://example.invalid") is None


def write_inputs(tmp_path):
    timeline, questions, stable = (tmp_path / name for name in ("timeline.jsonl", "questions.jsonl", "stable.txt"))
    timeline.write_text(json.dumps({"t": 0, "kind": "moment", "who": None,
                                    "text": "SYNTHETIC_CONTEXT"}) + "\n", encoding="utf-8")
    questions.write_text(json.dumps({"id": "synthetic-id", "ask_at": 3600,
        "question": "SYNTHETIC_QUESTION", "answer": "SYNTHETIC_ANSWER", "about_t": 0,
        "aliases": ["SYNTHETIC_ALIAS"]}) + "\n", encoding="utf-8")
    stable.write_text("SYNTHETIC_STABLE", encoding="utf-8")
    return timeline, questions, stable


def test_cli_dry_run_never_reads_key_and_writes_only_metrics(tmp_path, monkeypatch, capsys):
    timeline, questions, stable = write_inputs(tmp_path)
    def forbidden(*args):
        pytest.fail("dry-run must not read credentials or call the network")
    monkeypatch.setattr(replay, "call_model", forbidden)
    monkeypatch.setattr(replay, "read_api_key", forbidden)
    output = tmp_path / "metrics.json"
    assert replay.main(["--timeline", str(timeline), "--questions", str(questions),
        "--stable-part", str(stable), "--output", str(output), "--dry-run"]) == 0
    serialized = output.read_text(encoding="utf-8") + capsys.readouterr().out
    assert "SYNTHETIC_" not in serialized
    assert len(json.loads(output.read_text())["results"]) == 2


def test_cli_refuses_output_over_input(tmp_path):
    timeline, questions, stable = write_inputs(tmp_path)
    original = timeline.read_bytes()
    assert replay.main(["--timeline", str(timeline), "--questions", str(questions),
        "--stable-part", str(stable), "--output", str(timeline), "--dry-run"]) == 2
    assert timeline.read_bytes() == original


@pytest.mark.parametrize("field,value", [("t", -1), ("t", float("nan")), ("kind", "bad"),
                                         ("text", 42), ("who", 42)])
def test_timeline_validation(tmp_path, field, value):
    row = {"t": 0, "kind": "owner", "who": None, "text": "synthetic"}
    row[field] = value
    path = tmp_path / "timeline.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        replay.load_timeline(path)


def test_rejects_unsorted_timeline_and_duplicate_question_ids(tmp_path):
    timeline, questions, _ = write_inputs(tmp_path)
    row = {"t": 5, "kind": "owner", "who": None, "text": "synthetic"}
    timeline.write_text(json.dumps(row) + "\n" + json.dumps(dict(row, t=4)), encoding="utf-8")
    with pytest.raises(ValueError):
        replay.load_timeline(timeline)
    questions.write_text(questions.read_text() * 2, encoding="utf-8")
    with pytest.raises(ValueError):
        replay.load_questions(questions)


def test_key_is_read_only_from_explicit_file_in_process(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.env"
    path.write_text('OTHER=ignored\nOPENAI_API_KEY="synthetic-fake-key"\n', encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "wrong-inherited-value")
    assert replay.read_api_key(path) == "synthetic-fake-key"


@pytest.mark.parametrize("cap", [Decimal("NaN"), Decimal("Infinity"), Decimal("-1")])
def test_invalid_cap_rejected(cap):
    with pytest.raises(ValueError):
        replay.Config(cap_usd=cap)
