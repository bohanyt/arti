"""Offline parser tests: no bridge, providers, audio or model imports."""

import json
import unittest

from tools.live_forensic_parser import parse_log, parse_telemetry, summarize


class ForensicParserTests(unittest.TestCase):
    def test_quantiles_use_linear_interpolation(self):
        self.assertEqual(summarize([10, 20, 30, 40]), {
            "count": 4, "sum": 100, "median": 25.0,
            "p90": 37.0, "p95": 38.5, "maximum": 40,
        })
        self.assertIsNone(summarize([])["median"])
        self.assertEqual(summarize([7])["p95"], 7)

    def test_latency_does_not_double_count_tts(self):
        result = parse_log('[Latency] vts_mikir=2ms rag=3ms llm=4ms '
                           'tts_synth=5ms tts_play=6ms tts_jeda=7ms '
                           'tts=11ms tts_chunks=2 lain=-1ms total=26ms')
        self.assertEqual(result["latency"][0]["sum_components_ms"], 26)
        self.assertEqual(result["latency"][0]["residual_ms"], 0)
        self.assertEqual(result["latency"][0]["tts_chunks"], 2)

    def test_optional_pause_is_zero_but_missing_stage_is_unknown(self):
        result = parse_log('[Latency] vts_mikir=2ms rag=3ms llm=4ms '
                           'tts_synth=5ms tts_play=6ms lain=1ms total=21ms\n'
                           '[Latency] total=4ms')
        self.assertEqual(result["latency"][0]["tts_jeda"], 0)
        self.assertIsNone(result["latency"][1]["sum_components_ms"])
        self.assertEqual(result["incomplete_latency_lines"], [2])

    def test_concatenated_thread_output_keeps_events_and_physical_lines(self):
        result = parse_log('other[Agy] proses dibuang (timeout)[Viewers] 4\n'
                           '[Agy] timeout 8s — jatuh ke Luna\n'
                           '[Agy] utama gagal (timeout) — jatuh ke Luna\n'
                           '\x1b[32m[Codex] gpt-5.6-luna/high 123ms (turn 2) in=1\x1b[0m')
        self.assertEqual(result["counts"]["agy_timeout"], 1)
        self.assertEqual(result["recycles"][0]["line"], 1)
        self.assertEqual(result["codex"][0]["latency_ms"], 123)

    def test_attempt_success_parse_failure_and_uptime_are_distinct(self):
        result = parse_log('[Vision] Trying google_gemini_lite...\n'
                           '[Vision] google_gemini_lite parse fail (200ms)\n'
                           '[Vision] Trying ollama...\n'
                           '[Vision] uptime ok=10 fail=0 fallbacks=3\n'
                           '[Vision] OK ollama (100ms)')
        self.assertEqual(len(result["vision_attempts"]), 2)
        self.assertEqual(len(result["vision_successes"]), 1)
        self.assertEqual(len(result["vision_failures"]), 1)
        self.assertEqual(result["vision_uptime"][-1]["ok"], 10)

    def test_still_screen_counts_messages_not_hidden_skips(self):
        result = parse_log('[Vision] Layar diam (x4)\n[Vision] Layar diam (x8)')
        self.assertEqual(result["counts"]["still_screen_messages"], 2)

    def test_progress_carriage_returns_do_not_change_physical_line_numbers(self):
        result = parse_log('progress 1\rprogress 2\rprogress 3\n[Latency] total=3ms\n')
        self.assertEqual(result["line_count"], 2)
        self.assertEqual(result["latency"][0]["line"], 2)

    def test_all_latency_occurrences_and_malformed_record_are_accounted_for(self):
        result = parse_log('[Latency] total=1ms[Latency] total=2ms\n[Latency] garbage')
        self.assertEqual(result["latency_marker_count"], 3)
        self.assertEqual(len(result["latency"]), 2)
        self.assertEqual(result["malformed_latency_lines"], [2])

    def test_telemetry_filters_reused_session_id_by_timestamp(self):
        rows = [dict(ts=ts, session_id="same", subsystem="asr", provider="groq",
                     ok=ok, latency_ms=10) for ts, ok in [
                         ("2026-09-02T19:17:00", True),
                         ("2026-09-02T19:48:35", True),
                         ("2026-09-02T22:27:00", False)]]
        result = parse_telemetry('\n'.join(map(json.dumps, rows)),
                                 start="2026-09-02T19:48:35",
                                 end="2026-09-02T22:27:00")
        self.assertEqual(result["record_count"], 1)
        self.assertEqual(result["outside_window"], 2)

    def test_bad_telemetry_is_reported_not_silently_skipped(self):
        result = parse_telemetry('{}\n{bad\n')
        self.assertEqual(result["invalid_lines"], [1, 2])


if __name__ == "__main__":
    unittest.main()
