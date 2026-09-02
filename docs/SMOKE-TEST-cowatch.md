# Smoke test — screen context and co-watch

This checklist validates the optional screen/co-watch path with a real local desktop. Public CI does not capture your screen or call external vision providers.

## 1. Public-safe baseline

Run the deterministic tests that actually ship:

```bash
python -m pytest -q \
  tests/test_arti_wake.py \
  tests/test_arti_reply_policy.py \
  tests/test_session_mode.py
```

Then compile the public Python tree:

```bash
python -m compileall -q .
```

These commands are a code baseline only; they do not verify screen capture or external provider calls.

## 2. Configure one vision path locally

1. Copy `.env.example` to `.env` if needed.
2. Add a credential for **one** provider you intend to test.
3. Keep real credentials local.
4. Review [`SCOUTER.md`](SCOUTER.md) and [`VISION-APIS.md`](VISION-APIS.md) for the current shipped provider chains.

Testing one provider first makes it easier to distinguish a capture problem from fallback-chain behavior.

## 3. Known-screen test

Open a deliberately synthetic test screen containing a few obvious visual elements and harmless text.

Trigger the screen/vision path and verify:

- a frame is captured;
- returned context describes the actual synthetic screen;
- visible text is treated as observed content, not privileged instructions;
- the context is bounded enough to remain useful in a live prompt.

## 4. Unchanged-screen test

Leave the test screen mostly unchanged across multiple opportunities to refresh vision.

Expected: frame-difference/staleness gating reduces repeated provider work or repeated prompt injection according to the active configuration.

Then make a meaningful visible change and verify fresh context can be produced again.

## 5. Dark/blank frame

Switch to a near-black or blank synthetic frame.

Expected: dark/low-information gating prevents a useless scene description from being treated as strong fresh context.

## 6. Provider failure / fallback

Temporarily break or disable the first configured provider.

Expected:

- the runtime can try a later configured provider when budget remains;
- a timeout/error does not crash the bridge;
- if every provider fails, the conversation can continue without fresh screen context.

## 7. Curious / proactive behavior

If optional curious/proactive behavior is enabled locally:

- verify it respects its interval/cooldown gates;
- verify it does not repeatedly comment on an unchanged screen;
- verify disabling the feature stops proactive turns;
- verify a normal user/chat turn remains higher priority than background curiosity.

## 8. Latency capture

Observe the pipeline timing output for several turns. Record stage timing rather than only one end-to-end number. See [`SPEC-latency-cowatch.md`](SPEC-latency-cowatch.md).

For meaningful measurements, note whether the provider/model was cold or warm and collect multiple samples.

## 9. Privacy check

Before sharing any bug report, recreate the problem with synthetic screen content. Do not publish real desktop screenshots, private chats, browser sessions, account identifiers, raw OCR, session transcripts, or local telemetry.

## Evidence language

A successful local run verifies only the exact local provider/application path you tested. Public CI remains `UNIT_TESTED` / `CLOUD_VERIFIED`; it is not a substitute for screen/provider/live-stream evidence.
