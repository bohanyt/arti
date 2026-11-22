"""Startup soul delivery and legacy RAG exclusion, using synthetic text only."""
import ast
import asyncio
import inspect
import textwrap

import pytest

import arti_bridge as B
import arti_bridge_testmode as T
import arti_vault_rag as rag
from arti_runtime.soul_block import SOUL_MARKER, assemble_startup_prompt, build_soul_block


@pytest.mark.parametrize("line", [
    'Pakai nama, bukan "kamu" atau "Anda".',
    'Hindari "ANDA" maupun "Kamu".',
    'JANGAN jawab lebih dari 3 kalimat.',
    '- jangan JAWAB lebih dari 12 kalimat!',
    'Aku katasaring.',
    'Catatan katasaring di sini.',
])
def test_drops_only_conflicting_line_kinds(line):
    kept = '## Sintetis\nAku suka memancing.\n'
    block, dropped = build_soul_block(kept + line + '\nTetap santai.', {})
    assert dropped == 1
    assert line not in block
    assert kept in block and block.endswith('\nTetap santai.')


@pytest.mark.parametrize("line", [
    'Boleh bilang "kamu".', 'Sapaan "Anda" terdengar formal.',
    'Pandang kebunmu.', 'Kamu lihat tanda itu.',
    'Jawab lebih dari 3 kalimat kalau perlu.',
    'JANGAN jawab lebih dari beberapa kalimat.',
    'Jawab sesuai rencana adaptif.', 'Aku memilih mengalihkan topik agama.',
    '## Selera sintetis', '', '  - spasi dan tanda baca tetap!  ',
    'Anda di satu baris.\nKamu di baris lain.',
])
def test_preserves_other_lines_verbatim(line):
    text = 'Awal sintetis.\r\n' + line + '\r\nAkhir.'
    block, dropped = build_soul_block(text, {})
    assert block.endswith(text) and dropped == 0


def test_empty_disabled_and_all_filtered_have_no_block():
    assert build_soul_block('', {}) == ('', 0)
    assert build_soul_block(' \n', {}) == ('', 0)
    assert build_soul_block('JANGAN jawab lebih dari 2 kalimat.', {}) == ('', 1)
    assert build_soul_block('Aku sintetis.\nAku katasaring.', {'soul_prompt_enabled': False}) == ('', 0)


def test_header_priority_and_presence_fence():
    block, _ = build_soul_block('Aku suka kebun sintetis.', {})
    assert block.startswith(SOUL_MARKER)
    assert 'aturan di atas' in block and 'konflik' in block
    assert 'belum tentu hadir sekarang' in block


def test_startup_log_contains_counts_only(capsys):
    tree = ast.parse(textwrap.dedent(inspect.getsource(B.main_loop)))
    logs = [n for n in ast.walk(tree) if isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name)
            and n.value.func.id == 'print' and '[Soul] chars injected=' in ast.unparse(n)]
    assert len(logs) == 1
    block, dropped = build_soul_block('Synthetic private persona.\nAku katasaring.', {})
    exec(compile(ast.Module(body=logs, type_ignores=[]), '<startup log>', 'exec'),
         {'soul_block': block, 'soul_lines_dropped': dropped})
    assert capsys.readouterr().out == f'[Soul] chars injected={len(block)}, lines dropped=1\n'


def test_marker_and_full_soul_survive_optional_trimming_at_default_cap():
    assert B.CONFIG['llm_system_prompt_max_chars'] == 30000
    assert B.CONFIG['soul_prompt_enabled'] is True
    assert SOUL_MARKER in B._SYSTEM_PROMPT_KEEP_MARKERS
    soul, _ = build_soul_block(('Selera sintetis.\n' * 535).rstrip(), {})
    self_block = '\n\n[DIRIKU]\nAku sintetis.'
    memory = '\n\n[MEMORI TERBARU (hari ini):]\n' + 'Memori sintetis.\n' * 2000
    prompt = assemble_startup_prompt(B._SYSTEM_PROMPT_BASE, '', self_block, soul, memory)
    trimmed = B.trim_system_prompt_for_llm(prompt, B.CONFIG)
    assert len(prompt) > 30000 and len(trimmed) <= 30000
    assert soul in trimmed and self_block in trimmed
    assert 'Memori sintetis.' not in trimmed
    # BASE contains an optional-memory marker: it must stop before the soul
    # even when [DIRIKU] is disabled.
    prompt = assemble_startup_prompt(B._SYSTEM_PROMPT_BASE, '', '', soul, memory)
    assert soul in B.trim_system_prompt_for_llm(prompt, B.CONFIG)


def _live_assembly(base, origin, self_block, soul_text, memory, config):
    """Execute the actual main_loop assembly and emotion statements without booting live."""
    body = ast.parse(textwrap.dedent(inspect.getsource(B.main_loop))).body[0].body
    statements = [n for n in body if isinstance(n, ast.Assign)
                  and any((isinstance(t, ast.Name) and t.id == 'dynamic_system_prompt')
                          or (isinstance(t, ast.Tuple) and any(isinstance(e, ast.Name) and e.id == 'soul_block'
                                                             for e in t.elts)) for t in n.targets)]
    assert len(statements) == 3
    namespace = dict(vars(B), _SYSTEM_PROMPT_BASE=base, origin_block=origin,
                     self_block=self_block, soul_context=soul_text, memory_block=memory, CONFIG=config)
    exec(compile(ast.Module(body=statements, type_ignores=[]), '<live assembly>', 'exec'), namespace)
    return namespace['dynamic_system_prompt']


@pytest.mark.parametrize('rules', [False, True])
@pytest.mark.parametrize('enabled,present', [(True, True), (False, True), (True, False)])
def test_live_and_testmode_assembly_and_legacy_bytes(tmp_path, monkeypatch, enabled, present, rules):
    cfg = dict(B.CONFIG, soul_prompt_enabled=enabled, arti_state_enabled=False, live_play_rules_enabled=rules)
    # No real soul is opened; present text is returned by a synthetic loader.
    monkeypatch.setattr(B, '_SCRIPT_DIR', str(tmp_path))
    load = B.load_soul_context
    if present:
        monkeypatch.setattr(B, 'load_soul_context', lambda: 'Aku suka hujan sintetis.\nAku katasaring.')
    else:
        assert load() == ''  # actual absent-file path
    monkeypatch.setattr(B, '_SYSTEM_PROMPT_BASE', 'Base sintetis.')
    monkeypatch.setattr(B, 'build_origin_context', lambda: '\nOrigin sintetis.')
    monkeypatch.setattr(B, 'load_long_term_memories', lambda: [])
    monkeypatch.setattr(B, 'build_startup_memory_block', lambda _: '\nMemory sintetis.')
    monkeypatch.setattr(B.arti_state, 'startup_self_block', lambda *a, **k: '\n\n[DIRIKU]\nSelf sintetis.')
    monkeypatch.setitem(B._discord_stories, 'block', '')
    async def inline(_lane, fn, *args, **kwargs):
        return fn(*args, **kwargs)
    monkeypatch.setattr(B.arti_executors, 'run', inline)
    mode = T.TestModeBridge(B, cfg, T.Transcript(tmp_path / 'synthetic-transcript.jsonl'))
    record = asyncio.run(mode.build_persona())
    text = B.load_soul_context()
    soul, _ = build_soul_block(text, cfg)
    base, origin = B._SYSTEM_PROMPT_BASE, B.build_origin_context()
    self_block = B.arti_state.startup_self_block()
    memory = B.build_startup_memory_block([])
    assert mode.dynamic_system_prompt == _live_assembly(base, origin, self_block, text, memory, cfg)
    assert record['soul_block_chars'] == len(soul)
    # Live play rules (mabar on): both assemblies carry them once, at the end; off: not at all.
    marker = '[SAAT MAIN: CARA NGOBROL]'
    assert mode.dynamic_system_prompt.count(marker) == (1 if rules else 0)
    if rules:
        assert mode.dynamic_system_prompt.rstrip().endswith(B.live_rules.LIVE_PLAY_RULES.rstrip())
    if enabled and present:
        assert mode.dynamic_system_prompt.index('[DIRIKU]') < mode.dynamic_system_prompt.index(SOUL_MARKER.strip())
        assert mode.dynamic_system_prompt.index(SOUL_MARKER.strip()) < mode.dynamic_system_prompt.index('Memory sintetis.')
        assert 'katasaring' not in mode.dynamic_system_prompt
    else:
        old = B.arti_expression_runtime.emotion_prompt_for_system(base + origin + self_block + memory, cfg)
        if rules:
            old = B.live_rules.apply(old, cfg)
        # Rules off: byte-identical to the assembly before the live rules existed.
        assert mode.dynamic_system_prompt.encode() == old.encode()


def test_existing_backstage_filter_still_precedes_soul_filter(tmp_path, monkeypatch):
    # A synthetic file in a temp root, never the real ARTI_SOUL.md (the shared
    # loader reads through pathlib, so the file object can no longer be mocked).
    monkeypatch.setattr(B, '_SCRIPT_DIR', str(tmp_path))
    (tmp_path / 'ARTI_SOUL.md').write_text(
        '## OPINI & PREFERENSI\n- Aku suka API model Luna.\n- Aku suka memancing.\n'
        '## ATURAN MUTLAK\nJANGAN jawab lebih dari 3 kalimat.\n', encoding='utf-8')
    block, dropped = build_soul_block(B.load_soul_context(), {})
    assert 'Aku suka API' not in block and 'Aku suka memancing.' in block
    assert 'JANGAN jawab' not in block and dropped == 1


@pytest.mark.parametrize('enabled', [True, False])
def test_index_exclusion_is_configurable_without_reading_sources(tmp_path, monkeypatch, enabled):
    monkeypatch.setattr(rag, '_ROOT', tmp_path)
    names = ['ARTI_SOUL.md', 'ARTI_VIEWERS.md', 'ARTI_SOUL.example.md']
    # Empty stand-ins only, no soul content.
    for name in names:
        (tmp_path / name).touch()
    files = rag.iter_index_files({'soul_prompt_enabled': enabled, 'vault_rag_index_globs': ['ARTI_*.md']})
    assert {p.name for p in files} == ({'ARTI_VIEWERS.md'} if enabled else {'ARTI_SOUL.md', 'ARTI_VIEWERS.md'})


@pytest.mark.parametrize('enabled', [None, True, False])
@pytest.mark.parametrize('route', ['semantic', 'lexical', 'mismatch', 'no_fts'])
def test_existing_db_soul_chunks_not_returned_or_deleted(tmp_path, monkeypatch, enabled, route):
    cfg = dict(rag.DEFAULT_CONFIG, vault_rag_db_path=str(tmp_path / 'synthetic.db'),
               vault_rag_min_score=0, vault_rag_min_score_lexical=-1, vault_rag_max_per_source=0)
    if enabled is not None:
        cfg['soul_prompt_enabled'] = enabled
    rag.init_db(cfg)
    sources = ['ARTI_SOUL.md', r'C:\synthetic\ARTI_SOUL.md', 'notes/arti_soul.MD', 'ARTI_VIEWERS.md',
               'vault/watch-parties/synthetic/ARTI_SOUL.md', 'vault/watch-parties/synthetic/memory.md']
    ordinary = {'ARTI_VIEWERS.md', 'vault/watch-parties/synthetic/memory.md'}
    with rag._connect(cfg) as conn:
        for i, source in enumerate(sources):
            cur = conn.execute('''INSERT INTO chunks
                (source_path, source_type, folder, chunk_index, heading, content, content_hash, mtime, char_count, created_at)
                VALUES (?, 'note', '', 0, '01:00', ?, ?, 1, 40, 1)''',
                (source, 'Kebun sintetis untuk memancing.', f'hash{i}'))
            cid = cur.lastrowid
            conn.execute('INSERT INTO embeddings (chunk_id, model, dim, vector) VALUES (?, ?, 2, ?)',
                         (cid, cfg['lmstudio_embedding_model'], rag._pack_vector([1., 0.])))
            rag._sync_fts(conn, cid)
    def embed(*a, **k):
        if route == 'lexical':
            raise RuntimeError('synthetic offline')
        return [1., 0., 0.] if route == 'mismatch' else [1., 0.]
    monkeypatch.setattr(rag, 'embed_query_cached', embed)
    def rerank(combined, *a, **k):
        if enabled is not False:
            assert {item['source_path'] for _, item in combined} <= ordinary
        return combined
    monkeypatch.setattr(rag, '_rerank_candidates', rerank)
    if route == 'no_fts':
        monkeypatch.setattr(rag, '_fts_scores', lambda *a, **k: {})
    hits = rag.search('Kebun sintetis', cfg, top_k=10)
    assert {h['source_path'] for h in hits} == (set(sources) if enabled is False else ordinary)
    time_hits = rag.search_by_timecode('synthetic', '01:00', cfg)
    expected = {'vault/watch-parties/synthetic/memory.md'}
    if enabled is False:
        expected.add('vault/watch-parties/synthetic/ARTI_SOUL.md')
    assert {h['source_path'] for h in time_hits} == expected
    with rag._connect(cfg) as conn:
        assert conn.execute('SELECT count(*) FROM chunks').fetchone()[0] == len(sources)
        assert conn.execute('SELECT count(*) FROM embeddings').fetchone()[0] == len(sources)


def test_removing_an_optional_block_keeps_every_later_header():
    """a51, 315 of 315 turns: dropping [HARI INI] also deleted [STARDEW] and [NATIVE_FACTS] behind it."""
    import arti_bridge as bridge
    prompt = ("B" * 500 + "\n\n[MOOD SAAT INI: x]\n\n[HARI INI]\ntoday line\n- more today\n\n"
              "[STARDEW] kamu main karaktermu sendiri\n[STARDEW STATUS] status\n[NIATKU] niat\n"
              "[KINI] kini\n[NATIVE_FACTS] {\"a\": 1}\n[PLAN_DATES] d")
    out = bridge.trim_system_prompt_for_llm(prompt, {"llm_system_prompt_max_chars": len(prompt) - 5})
    assert "today line" not in out and "more today" not in out
    for header in ("[STARDEW] kamu", "[STARDEW STATUS]", "[NIATKU]", "[KINI]", "[NATIVE_FACTS]", "[PLAN_DATES]"):
        assert header in out, header
    assert "(system dipangkas)" not in out


def test_default_cap_fits_soul_and_the_largest_a51_turn_without_a_hard_cut():
    import arti_bridge as bridge
    base, soul, facts, reserved = 6928, 8381, 10114, 2927  # a51 maxima; soul is the real block size
    assert bridge.CONFIG["llm_system_prompt_max_chars"] >= base + soul + facts + reserved + 1500  # RAG, today
