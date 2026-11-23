"""Interactive startup checks and config persistence for the bridge."""


def _wizard_input(
    prompt: str, default: str = "",
    *,
    sys,
    input,
) -> str:
    if not sys.stdin.isatty():
        return default
    try:
        raw = input(prompt).strip()
        return raw if raw else default
    except (EOFError, KeyboardInterrupt):
        return default


def prompt_live_session_setup(
    *,
    config,
    resolve_asr_input_device,
    _wizard_input,
    _extract_yt_video_id,
    sys,
    bridge_health,
    print,
) -> bool:
    """Tanya YouTube + VTS port SETIAP startup — Enter = pakai nilai sekarang."""
    changed = False
    yt = config.get("youtube_video_id", "") or "(kosong)"
    yt_on = config.get("youtube_chat_enabled", True)
    vts_port = int(config.get("vts_api_port", 8002))
    sub_port = int(config.get("subtitle_port", 9988))
    profile = config.get("active_profile", "default")

    print("\n" + "=" * 60)
    print("  LIVE SESSION SETUP  (Enter = keep current values)")
    print("=" * 60)
    mic_id, mic_name = resolve_asr_input_device()
    mic_label = f"#{mic_id} {mic_name}" if mic_id is not None else mic_name

    print(f"  YouTube : {yt}  chat={'ON' if yt_on else 'OFF'}")
    print(f"  VTS port: {vts_port}")
    print(f"  Subtitle: {sub_port}  |  Profil: {profile}")
    print(f"  Mic     : {mic_label}")
    print("-" * 60)

    raw_yt = _wizard_input(
        "  >> YouTube URL/ID (off=matikan chat, Enter=keep): ",
        "",
    )
    if raw_yt:
        if raw_yt.lower() in ("off", "no", "0", "-"):
            config["youtube_chat_enabled"] = False
            changed = True
            print("  [OK] YouTube chat OFF")
        else:
            vid = _extract_yt_video_id(raw_yt)
            if vid:
                config["youtube_video_id"] = vid
                config["youtube_chat_enabled"] = True
                changed = True
                print(f"  [OK] YouTube -> {vid} (chat ON)")
            else:
                print(
                    "  [WARN] URL/ID tidak valid (ID YouTube = 11 karakter) — "
                    "YouTube TIDAK diubah, tetap pakai yang lama. Kalau stream "
                    "lama sudah mati, chat akan gagal terus."
                )

    raw_port = _wizard_input(
        f"  >> VTS port Arti (Enter={vts_port}): ",
        "",
    )
    if raw_port.isdigit():
        new_port = int(raw_port)
        if new_port != vts_port:
            config["vts_api_port"] = new_port
            vts_port = new_port
            changed = True
            print(f"  [OK] VTS port -> {new_port}")

    raw_sub = _wizard_input(
        f"  >> Subtitle port (Enter={sub_port}): ",
        "",
    )
    if raw_sub.isdigit():
        new_sub = int(raw_sub)
        if new_sub != sub_port:
            config["subtitle_port"] = new_sub
            sub_port = new_sub
            changed = True
            print(f"  [OK] Subtitle port -> {new_sub}")

    if sys.stdin.isatty():
        if bridge_health.prompt_mic_selection(
            config,
            resolve_mic_fn=resolve_asr_input_device,
            ask_input=lambda p: _wizard_input(p, ""),
        ):
            changed = True

    return changed


def startup_wizard(
    *,
    config,
    script_path,
    arti_vault_rag,
    prompt_live_session_setup,
    socket,
    sys,
    input,
    _supertone_preflight,
    arti_vts_auth,
    save_live_session,
    print,
):
    """Interactive pre-flight checklist sebelum bridge start.
    Detect missing config, prompt user untuk input, validate.
    Bohan tinggal jawab pertanyaan — ga perlu edit file."""
    
    print("\n" + "="*60)
    print("  ARTI BRIDGE — Startup Checklist")
    print("="*60)
    # Bangunkan model embedding SEKARANG, di latar — cold load LM Studio
    # (~10-16 dtk) kebayar selagi Bohan menjawab checklist, bukan di
    # panggilan RAG pertama (yang timeout-nya cuma 8 dtk).
    arti_vault_rag.prewarm_embedding(config)
    needs_save = False

    if prompt_live_session_setup():
        needs_save = True

    # VTS port probe (setelah user confirm port)
    vts_port = config.get("vts_api_port", 8002)
    vts_ok = False
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        result = sock.connect_ex(('localhost', vts_port))
        sock.close()
        if result == 0:
            print(f"\n  [OK] VTS terdeteksi di port {vts_port}")
            vts_ok = True
    except Exception:
        pass
    
    if not vts_ok:
        print(f"\n  [WARN] VTS di port {vts_port} tidak terdeteksi!")
        print("  Tips: Dua instance VTS? Biasanya port 8001 (instance pertama) dan 8002 (instance kedua).")
        print(f"  [INFO] Jalankan VTS + Start API di port {vts_port}, atau restart wizard.")

    youtube_id = config.get("youtube_video_id", "")
    if config.get("youtube_chat_enabled") and youtube_id:
        print(f"\n  [OK] YouTube Video ID: {youtube_id}")
    elif not config.get("youtube_chat_enabled"):
        print("\n  [INFO] YouTube chat disabled untuk sesi ini.")
    else:
        print("\n  [WARN] YouTube chat ON tapi video ID kosong.")

    # 2.5 Subtitle Port — cek bentrok, kalau conflict prompt manual
    sub_port = config.get("subtitle_port", 9988)
    sub_port_ok = False
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        import subtitle_server  # the probe checks the address the listener will bind (owner 2026-11-19)
        result = sock.bind((subtitle_server.bind_host(config), sub_port))
        sock.close()
        sub_port_ok = True
    except OSError:
        sock.close()
    if not sub_port_ok:
        print(f"\n  [WARN] Port subtitle {sub_port} bentrok (TIME_WAIT atau dipakai proses lain).")
        if not sys.stdin.isatty():
            new_sub_port = sub_port + 1
        else:
            try:
                raw_sub = input(f"  >> Ketik port subtitle baru (Enter = tetap {sub_port}): ").strip()
                new_sub_port = int(raw_sub) if raw_sub.isdigit() else sub_port + 1
            except (EOFError, KeyboardInterrupt):
                new_sub_port = sub_port + 1
        config["subtitle_port"] = new_sub_port
        sub_port = new_sub_port
        needs_save = True
        print(f"  [OK] Subtitle port diganti ke {sub_port}")
    else:
        print(f"\n  [OK] Subtitle port {sub_port} tersedia.")

    if config.get("tts_engine") == "supertone":
        _supertone_preflight()

    # 3. TTS voice check
    voice = config.get("tts_voice", "")
    if not voice:
        print("\n  [WARN] TTS voice belum diset.")
        config["tts_voice"] = "id-ID-GadisNeural"
        needs_save = True
        print("  [OK] Default voice: id-ID-GadisNeural")
    else:
        print(f"\n  [OK] TTS voice: {voice}")

    # 4. Virtual cable check
    print("\n  [INFO] Virtual cable akan dicari otomatis saat TTS init.")

    # 5. Token VTS check
    token_file = arti_vts_auth.token_path(script_path, config["vts_api_port"])
    if not arti_vts_auth.read_token(script_path, config["vts_api_port"], main_port=config["vts_api_port"]):
        print("\n  [INFO] VTS token tidak ditemukan — akan minta ALLOW saat connect.")
    else:
        print(f"\n  [OK] VTS token ditemukan ({token_file}).")

    # 6. API Provider quick check
    provider = config.get("api_provider", "groq").lower()
    print(f"\n  [OK] API provider: {provider}")
    if provider == "groq":
        groq_key = config.get("groq_api_key", "")
        if groq_key and groq_key.startswith("gsk_"):
            print("  [OK] Groq API key terdeteksi")
        else:
            print("  [WARN] Groq API key belum valid!")
    elif provider == "gemini":
        gemini_key = config.get("gemini_api_key", "")
        if gemini_key and not gemini_key.startswith("YOUR_"):
            print("  [OK] Gemini API key terdeteksi")
        else:
            print("  [WARN] Gemini API key belum valid!")

    # 7. OpenRouter key status (informational only)
    openrouter_key = config.get("openrouter_api_key") or ""
    if openrouter_key:
        print(f"\n  [INFO] OpenRouter key: {'SET' if openrouter_key else 'NOT SET'}")

    print("\n" + "="*60)
    print("  Checklist selesai! Bridge siap start.")
    print("="*60 + "\n")
    if sys.stdin.isatty():
        save_live_session()
    return needs_save


def _extract_yt_video_id(
    text,
    *,
    re,
):
    """Extract YouTube video ID dari URL atau raw input."""
    text = text.strip()
    # Raw ID (11 karakter alphanumeric + dash + underscore)
    if re.match(r'^[a-zA-Z0-9_-]{11}$', text):
        return text
    # Full URL
    patterns = [
        r'(?:youtube\.com|youtu\.be)/(?:watch\?v=|embed/|shorts/|live/)?([a-zA-Z0-9_-]{11})',
        r'youtube\.com/watch\?.*v=([a-zA-Z0-9_-]{11})',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return None


# Nilai per-mesin yang boleh dipersistenkan wizard (semuanya ADA di CONFIG
# sebagai default generik; nilai nyatanya milik mesin uji).
_WIZARD_PERSIST_KEYS = (
    "youtube_video_id",
    "youtube_chat_enabled",
    "vts_api_port",
    "subtitle_port",
)


def _save_config_to_file(
    *,
    config,
    root_path,
    _WIZARD_PERSIST_KEYS,
    os,
    json,
    open,
    print,
):
    """Simpan nilai per-mesin dari wizard ke config_local.json (gitignored).

    DULU fungsi ini MENULIS ULANG source hermes_vtuber_bridge.py sendiri —
    dua cacat sekaligus:
    1. Nilai sesi live ikut ter-commit ke repo publik: subtitle_port 9992
       (subtitle.html default 9991 -> Browser Source OBS diam total di
       checkout bersih) dan youtube_chat_enabled True + video ID basi.
    2. PERCUMA: _load_local_config() dijalankan SETELAH CONFIG didefinisikan
       dan MENANG (CONFIG.update). Karena youtube_video_id/chat_enabled ada
       di config_local, "default" yang ditulis ke source selalu ditelan
       overlay lokal di sesi berikutnya — Bohan bisa tekan Enter ("keep")
       lalu bridge polling video ID LAMA sepanjang sesi.
    Menulis ke config_local.json membereskan keduanya: gitignored, dan
    di atas overlay source.

    Catatan urutan lapisan yang JUJUR: saat startup,
    load_live_session() jalan SETELAH overlay lokal dan menimpa keempat key
    ini dari live_session.json (yang di-save wizard TANPA peduli jawaban
    y/N). Jadi yang menang sehari-hari = live_session; config_local di sini
    berperan sebagai cadangan tahan-lama (live_session hilang/di-reset) —
    bukan lapisan pemenang. Mengedit youtube_video_id manual di config_local
    TIDAK cukup selama live_session.json masih memuat nilai lama.
    """
    path = os.path.join(root_path, "config_local.json")
    try:
        data: dict = {}
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        for key in _WIZARD_PERSIST_KEYS:
            if key in config:
                data[key] = config[key]
        # Tulis via file sementara: config_local.json memegang setelan mesin
        # Bohan — crash di tengah tulis tidak boleh menyisakan JSON rusak.
        # Nama tmp per-PID: dua bridge bisa kebuka bersamaan —
        # tmp fixed bisa saling truncate sebelum os.replace.
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
        print(f"[CONFIG] Tersimpan ke config_local.json ({len(_WIZARD_PERSIST_KEYS)} key per-mesin)")
    except Exception as e:
        print(f"[WARN] Gagal save config: {e}")
