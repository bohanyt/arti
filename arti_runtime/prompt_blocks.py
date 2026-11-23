"""Render the origin, date, and startup memory prompt blocks."""


def build_origin_context(config: dict) -> str:
    """Fakta kanon debut + pointer arsip — selalu inject (hemat token)."""
    label = config.get("arti_debut_label") or "debut date"
    return (
        f"\n\n[ASAL USUL ARTI]\n"
        f"fakta contoh live: {label} ({config.get('arti_debut_date') or 'YYYY-MM-DD'}).\n"
        f"Kalau ditanya sejak kapan Arti ada: jawab {label}, bukan tanggal sesi hari ini."
    )


def build_today_block(config: dict, *, time) -> str:
    """Tanggal hari ini + umur karier — Arti selama ini TIDAK tahu tanggal.

    Label [YYYY-MM-DD] di cuplikan RAG tak bisa dia hitung jadi "tiga hari lalu"
    tanpa jangkar ini (tanggal sesi tidak pernah disuntik ke
    prompt mana pun). Ditutup pengingat debut supaya tidak menabrak aturan
    origin block ("jawab tanggal debut, bukan tanggal sesi").
    """
    today = time.strftime("%Y-%m-%d")
    umur = ""
    try:
        debut_struct = time.strptime(str(config.get("arti_debut_date", "")), "%Y-%m-%d")
        days = max(0, int((time.time() - time.mktime(debut_struct)) // 86400))
        umur = f" Kamu sudah {days} hari jadi co-host."
    except (ValueError, OverflowError):
        pass
    return (
        f"\n\n[HARI INI]\n"
        f"Tanggal sesi sekarang: {today}.{umur} Ini alat hitung DI KEPALAMU saja: "
        f"cuplikan memori berlabel [YYYY-MM-DD] kamu terjemahkan jadi waktu relatif "
        f"('tiga hari lalu', 'minggu kemarin'). "
        f"JANGAN menyebut atau mengumumkan tanggal di jawaban kecuali memang "
        f"DITANYA tanggal/hari — tidak ada 'berdasarkan tanggal...', tidak ada "
        f"tanggal di akhir jawaban. "
        f"Tanggal debut tetap dijawab sesuai [ASAL USUL ARTI]."
    )


def build_startup_memory_block(memories: list[str], config: dict, *, arti_live_safety) -> str:
    """Cuplikan memori startup — sisanya lewat Vault RAG per query (hemat token Groq)."""
    import arti_memory_quality

    max_b = int(config.get("memory_startup_max_bullets", 0))
    if max_b <= 0:
        if config.get("vault_rag_enabled", True):
            return (
                "\n\n[MEMORI JANGKA PANJANG: catatan sesi lama dipanggil otomatis saat "
                "jawab. Jangan sebut nama teknis internal (database, RAG, vault, nama "
                "berkas, nama model). Pakai ingatan itu supaya nyambung dengan obrolan "
                "sekarang; cara kerjanya nggak perlu diceritakan. "
                "Urusan teknis ditangani Bohan; jangan membuat klaim pekerjaan internal.]"
            )
        return ""
    memories = [m for m in memories if not arti_live_safety.is_backstage(str(m))]
    today_memories = arti_memory_quality.filter_memories_for_startup(memories)
    if not today_memories:
        if config.get("vault_rag_enabled", True):
            return (
                "\n\n[MEMORI JANGKA PANJANG: catatan sesi lama dipanggil otomatis saat "
                "jawab. Jangan sebut nama teknis internal (database, RAG, vault, nama "
                "berkas, nama model). Pakai ingatan itu supaya nyambung dengan obrolan "
                "sekarang; cara kerjanya nggak perlu diceritakan. "
                "Urusan teknis ditangani Bohan; jangan membuat klaim pekerjaan internal.]"
            )
        return ""
    return "\n\n[MEMORI TERBARU (hari ini):]\n" + "\n".join(today_memories[-max_b:])
