"""Model routing, prompt trimming, and fallback for Groq voice turns."""


def _remove_system_prompt_block(
    text: str, marker: str,
    *,
    re,
    _SYSTEM_PROMPT_BLOCK_MARKERS,
    _SYSTEM_PROMPT_KEEP_MARKERS,
) -> str:
    """Hapus satu blok opsional; blok lain setelahnya tetap."""
    start = text.find(marker)
    if start < 0:
        return text
    tail = text[start + len(marker) :]
    # An optional block ends at the next header of any kind. Ending only at listed markers let
    # [HARI INI] take [STARDEW] and [NATIVE_FACTS] after it with it.
    next_header = re.search(r"(?m)^\[[A-Z]", tail)
    end_rel = next_header.start() if next_header else len(tail)
    for other in _SYSTEM_PROMPT_BLOCK_MARKERS + _SYSTEM_PROMPT_KEEP_MARKERS:
        if other == marker:
            continue
        pos = tail.find(other)
        if pos >= 0:
            end_rel = min(end_rel, pos)
    end = start + len(marker) + end_rel
    return (text[:start] + text[end:]).rstrip()


def _split_stardew_status(
    system_prompt: str,
    *,
    config,
    re,
) -> tuple[str, str]:
    """Reserve status, intent and generic current time/place facts outside every trim."""
    # Her role, native facts and plan dates are facts too; the trim bug showed what losing them costs.
    pattern = r"(?m)^\[(?:STARDEW(?: STATUS)?|NIATKU|KINI|NATIVE_FACTS|PLAN_DATES)\][^\r\n]*"
    if config.get("voice_game_memory_enabled"):
        from arti_runtime import experience_digest  # her game memory line is a fact line too
        pattern = experience_digest.RESERVED_PATTERN
    facts = re.findall(pattern, system_prompt)
    if not facts:
        return system_prompt, ""
    return re.sub(pattern, "", system_prompt), "\n" + "\n".join(facts) + "\n"


def pick_groq_model(
    user_text: str,
    config: dict | None = None,
    prompt_chars: int = 0,
    *,
    default_config,
    pick_groq_model,
) -> str:
    """Pilih model Groq by complexity; fallback round-robin jika smart off."""
    cfg = config or default_config
    models = cfg.get("groq_models", ["llama-3.1-8b-instant"])
    fast = cfg.get("groq_model_fast", "llama-3.1-8b-instant")

    def _pick(preferred: str) -> str:
        return preferred if preferred in models else (models[0] if models else preferred)

    # Prompt besar → model fast (hindari HTTP 413 di Groq)
    if prompt_chars > int(cfg.get("groq_prompt_char_soft_cap", 33000)):
        return _pick(fast)

    if not cfg.get("smart_groq_routing", True):
        if not hasattr(pick_groq_model, "_rr_idx"):
            pick_groq_model._rr_idx = 0
        m = models[pick_groq_model._rr_idx % len(models)]
        pick_groq_model._rr_idx += 1
        return m

    medium = cfg.get("groq_model_medium", "openai/gpt-oss-20b")
    strong = cfg.get("groq_model_strong", "openai/gpt-oss-120b")
    rare = cfg.get("groq_model_rare", "openai/gpt-oss-120b")

    t = (user_text or "").lower()
    complex_kw = ("kenapa", "jelaskan", "bagaimana", "bandingkan", "explain", "detail", "ceritain", "maksudnya")
    if len(user_text) > 180 or sum(1 for k in complex_kw if k in t) >= 2:
        return _pick(rare)
    if "?" in user_text or len(user_text) > 100 or any(k in t for k in complex_kw):
        return _pick(strong)
    if len(user_text) > 55:
        return _pick(medium)
    return _pick(fast)


def _groq_fallback_chain(primary: str, config: dict) -> list[str]:
    """Urutan coba: model pilihan dulu, lalu sisanya (fast diutamakan saat limit)."""
    all_models = list(config.get("groq_models") or [])
    fast = config.get("groq_model_fast", "llama-3.1-8b-instant")
    tail: list[str] = []
    for m in [fast, *all_models]:
        if m and m not in tail:
            tail.append(m)
    chain = [primary] if primary else []
    for m in tail:
        if m not in chain:
            chain.append(m)
    return chain or all_models or [fast]


def _trim_llm_payload(
    system_prompt: str,
    user_content: str,
    max_chars: int = 14000,
    *,
    _split_stardew_status,
    _remove_system_prompt_block,
    _SYSTEM_PROMPT_BLOCK_MARKERS,
    SOUL_MARKER,
    re,
) -> tuple[str, str]:
    """Fit Groq's 413 retry, in order: history, optional blocks, the soul down to its head, then the rest.

    The current message, the fact lines and the soul's head go last of all. They can pass max_chars
    together: a slightly big retry beats one that answers without knowing what is true or who she is."""
    def over():
        return len(system_prompt) + len(user_content) - max_chars

    if over() <= 0:
        return system_prompt, user_content
    history, sep, current = user_content.partition("[Pesan/Panggilan Sekarang:]")
    if sep:
        note = "...(history dipangkas)...\n\n"
        keep = max(0, len(history) - over() - len(note))
        user_content = note + (history[-keep:] if keep else "") + sep + current
    elif len(user_content) > max_chars:
        user_content = user_content[-max_chars:]
    if over() <= 0:
        return system_prompt, user_content
    text, facts = _split_stardew_status(system_prompt)
    system_prompt = text + facts
    for marker in _SYSTEM_PROMPT_BLOCK_MARKERS:
        if over() <= 0:
            break
        text = _remove_system_prompt_block(text, marker)
        system_prompt = text + facts
    if over() <= 0:
        return system_prompt, user_content
    start = text.find(SOUL_MARKER)
    before, soul, rest = text, "", ""
    if start >= 0:
        after = start + len(SOUL_MARKER)
        found = re.search(r"(?m)^\[[A-Z]", text[after:])
        end = after + found.start() if found else len(text)
        before, soul, rest = text[:start], text[start:end], text[end:]
        # A 2000-character floor: her identity and core personality come first in the file. Whole lines only.
        keep = max(min(len(soul), 2000), len(soul) - over())
        if keep < len(soul):
            soul = soul[:keep].rsplit("\n", 1)[0] + "\n"
        system_prompt = before + soul + rest + facts
    if over() > 0:
        before = before[:max(4000, len(before) - over())]
        system_prompt = before + soul + rest + facts
    if over() > 0:
        rest = rest[:max(0, len(rest) - over())]
    return before + soul + rest + facts, user_content


def _openrouter_after_groq(
    system_prompt: str,
    user_content: str,
    cfg: dict,
    reason,
    *,
    screen_privacy,
    _groq_health_tl,
    arti_provider_health,
    arti_openrouter,
) -> tuple[str | None, str | None]:
    if not screen_privacy.current(cfg.get("_screen_privacy_epoch", screen_privacy.epoch)):
        return None, None
    # Penanda untuk pembungkus groq_chat_completion: Groq jatuh ke sini = Groq gagal.
    _groq_health_tl.alasan = reason
    if not cfg.get("openrouter_live_fallback_enabled", True):
        return None, None
    if not arti_provider_health.is_available("openrouter:voice"):
        return None, None
    print(f"[Brain] Fallback OpenRouter (groq: {reason})...")
    reply, or_model = arti_openrouter.openrouter_live_completion(
        system_prompt, user_content, cfg
    )
    if reply:
        arti_provider_health.record_success("openrouter:voice")
    else:
        arti_provider_health.record_failure("openrouter:voice", "unknown")
    return reply, or_model


def _groq_model_hilang(response) -> bool:
    """HTTP 400 `model_decommissioned` = model ini MATI, bukan permintaan salah.

    Groq memensiunkan model kira-kira tiap 2 bulan.
    Sampai 2026-08-13 kode di bawah memperlakukan 400 sebagai kegagalan fatal
    lalu `break` — jadi SATU model mati di tengah rantai menyumbat seluruh
    sisanya: primary kena 429 -> model mati -> berhenti -> OpenRouter, tanpa
    pernah menyentuh model Groq yang masih hidup. Dibuktikan dengan
    menjalankan groq_chat_completion terhadap transport palsu berbentuk error
    asli (diambil dari `llama3-8b-8192` yang sudah mati sejak Agu 2025).

    Sengaja SEMPIT — hanya kode/pesan yang jelas menyoal model. Permintaan
    yang memang rusak (payload salah) harus tetap gagal cepat, bukan
    menggilir lima model dan membuang lima kali round-trip.
    """
    if response.status_code not in (400, 404):
        return False
    kode = pesan = ""
    try:
        err = (response.json() or {}).get("error") or {}
        if isinstance(err, dict):
            kode = str(err.get("code") or "")
            pesan = str(err.get("message") or "")
    except Exception:  # noqa: BLE001 - body bukan JSON
        pesan = response.text or ""
    campur = f"{kode} {pesan}".lower()
    return any(
        p in campur
        for p in (
            "model_decommissioned",
            "model_not_found",
            "has been decommissioned",
            "no longer supported",
            "does not exist",
        )
    )


def groq_chat_completion(
    primary_model: str,
    system_prompt: str,
    user_content: str,
    config: dict | None = None,
    *,
    default_config,
    _openrouter_after_groq,
    _groq_fallback_chain,
    screen_privacy,
    live_max_tokens_for_trigger,
    REPLY_THINKING_HEADROOM_TOKENS,
    requests,
    arti_reply_policy,
    _trim_llm_payload,
    _groq_model_hilang,
) -> tuple[str | None, str | None]:
    """Groq dengan model pilihan (+ opsi roll semua model) → OpenRouter jika gagal."""
    cfg = config or default_config
    key = cfg.get("groq_api_key")
    if not key or key == "YOUR_GROQ_API_KEY":
        return _openrouter_after_groq(system_prompt, user_content, cfg, "no groq key")

    if cfg.get("groq_roll_all_models_on_limit", False):
        chain = _groq_fallback_chain(primary_model, cfg)
    else:
        chain = [primary_model] if primary_model else []

    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    url = "https://api.groq.com/openai/v1/chat/completions"
    retryable = (429, 502, 503)
    last_status = None
    tried_413_retry = False
    dicoba: list[str] = []   # yang SUNGGUH ditembak, bukan panjang rantai

    for model in chain:
        if not screen_privacy.current(cfg.get("_screen_privacy_epoch", screen_privacy.epoch)):
            return None, None
        dicoba.append(model)
        payload = {
            "model": model,
            "max_tokens": live_max_tokens_for_trigger(user_content, cfg),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        }
        if "qwen" in model.lower():
            # "/no_think" diabaikan qwen3.6 — pakai reasoning_effort API param
            payload["reasoning_effort"] = "none"
        elif "gpt-oss" in model.lower():
            # gpt-oss juga model reasoning tapi selama ini TANPA peredam —
            # giliran curious (teks panjang -> routing rare =
            # gpt-oss-120b) bisa balik 200 dengan content KOSONG karena seluruh
            # budget habis buat nalar. Groq menghormati "low" (beda dari
            # provider free OpenRouter yang mengabaikannya) — nalar singkat,
            # jawaban tetap utuh; nalar dibiarkan menyala, tapi serendah mungkin.
            payload["reasoning_effort"] = "low"
            payload["max_tokens"] += REPLY_THINKING_HEADROOM_TOKENS
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=(3, 8))
        except Exception as e:
            print(f"[Groq] {model} error: {e}")
            if not cfg.get("groq_roll_all_models_on_limit", False):
                break
            continue

        if response.status_code == 200:
            _pilihan = response.json()["choices"][0]
            isi = arti_reply_policy.trim_if_length_cut(
                _pilihan["message"]["content"], _pilihan.get("finish_reason"), f"Groq {model}"
            )
            if isi and str(isi).strip():
                return isi, model
            # HTTP 200 dengan content kosong = GAGAL, bukan sukses. Dulu
            # nilai kosong ini di-return apa adanya -> pemanggil menganggap
            # provider "sudah menjawab" -> OpenRouter tak pernah dicoba ->
            # giliran curious membisu.
            print(f"[Groq] {model} 200 tapi content KOSONG (budget termakan nalar?) — lanjut")
            last_status = 200
            continue

        last_status = response.status_code
        if response.status_code == 413 and not tried_413_retry:
            if not screen_privacy.current(cfg.get("_screen_privacy_epoch", screen_privacy.epoch)):
                return None, None
            tried_413_retry = True
            fast = cfg.get("groq_model_fast", "llama-3.1-8b-instant")
            sys_t, user_t = _trim_llm_payload(system_prompt, user_content)
            print(f"[Groq] HTTP 413 — retry {fast} dengan prompt dipangkas...")
            payload["model"] = fast
            payload["messages"] = [
                {"role": "system", "content": sys_t},
                {"role": "user", "content": user_t},
            ]
            try:
                response = requests.post(url, headers=headers, json=payload, timeout=(3, 8))
            except Exception as e:
                print(f"[Groq] retry 413 error: {e}")
                break
            if response.status_code == 200:
                _pilihan = response.json()["choices"][0]
                return arti_reply_policy.trim_if_length_cut(
                    _pilihan["message"]["content"], _pilihan.get("finish_reason"), f"Groq {fast}"
                ), fast
            last_status = response.status_code
        if _groq_model_hilang(response):
            print(
                f"[Groq] {model} SUDAH PENSIUN "
                f"(HTTP {response.status_code}) — lanjut model berikutnya."
            )
            continue
        if response.status_code in retryable:
            if cfg.get("groq_roll_all_models_on_limit", False):
                print(f"[Groq] {model} HTTP {response.status_code} — coba Groq lain...")
                continue
            print(f"[Groq] {model} HTTP {response.status_code} — langsung OpenRouter.")
            break
        if response.status_code not in (413,):
            print(f"[Groq] {model} HTTP {response.status_code}: {response.text[:150]}")
            break
        print(f"[Groq] {model} HTTP 413 — langsung OpenRouter.")
        break

    if len(dicoba) > 1:
        # Dulu mencetak len(chain) — bohong: "Gagal setelah 5 model" padahal
        # cuma 1-2 yang ditembak sebelum break.
        print(
            f"[Groq] Gagal setelah {len(dicoba)} model dicoba "
            f"({', '.join(dicoba)}) — HTTP {last_status}"
        )
    return _openrouter_after_groq(system_prompt, user_content, cfg, last_status)


# --- Kesehatan provider Groq (2026-10-23) ---------------------------------
# Pembungkus TIPIS, badan groq_chat_completion asli tidak disentuh. Groq
# dianggap gagal kalau alurnya jatuh ke _openrouter_after_groq (penanda
# thread-local); sukses kalau jawaban balik tanpa lewat sana. Groq mati /
# breaker terbuka -> langsung OpenRouter tanpa menembak 5 model Groq.
_groq_chat_completion_inti = groq_chat_completion


def groq_chat_completion(  # noqa: F811 — sengaja membungkus versi inti di atas
    primary_model: str,
    system_prompt: str,
    user_content: str,
    config: dict | None = None,
    *,
    default_config,
    arti_provider_health,
    _groq_chat_completion_inti,
    _openrouter_after_groq,
    _groq_health_tl,
    _GROQ_TANPA_FALLBACK,
) -> tuple[str | None, str | None]:
    """Groq + provider health (gerbang + catat), lalu perilaku lama apa adanya."""
    cfg = config or default_config
    if not arti_provider_health.enabled():
        return _groq_chat_completion_inti(primary_model, system_prompt, user_content, config)
    if not arti_provider_health.is_available("groq"):
        return _openrouter_after_groq(system_prompt, user_content, cfg, "groq breaker/dead")
    _groq_health_tl.alasan = _GROQ_TANPA_FALLBACK
    reply, used = _groq_chat_completion_inti(primary_model, system_prompt, user_content, config)
    alasan = getattr(_groq_health_tl, "alasan", _GROQ_TANPA_FALLBACK)
    _groq_health_tl.alasan = _GROQ_TANPA_FALLBACK
    if alasan is not _GROQ_TANPA_FALLBACK:
        if alasan != "no groq key":
            kelas = (arti_provider_health.classify_status(alasan)
                     if isinstance(alasan, int) else "unknown")
            # Status yang dilihat di sini milik model TERAKHIR di rantai Groq
            # (mis. 429,429,404): 404/auth BUKAN bukti Groq mati -> breaker
            # biasa. Vonis mati hanya dari probe startup.
            if kelas in arti_provider_health.HARD:
                kelas = "unknown"
            arti_provider_health.record_failure("groq", kelas)
    elif reply:
        arti_provider_health.record_success("groq")
    return reply, used
