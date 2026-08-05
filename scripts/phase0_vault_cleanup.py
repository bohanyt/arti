"""One-shot Phase 0: merge Arti vault sessions (repo + hermes-vault) to 1 MD/day."""
from __future__ import annotations

import re
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HV_SESSIONS = Path(r"C:\Users\<user>\Documents\hermes-vault\sessions")
HV_LEARNINGS = Path(r"C:\Users\<user>\Documents\hermes-vault\concepts\arti_live_learnings.md")
REPO_SESSIONS = REPO / "vault" / "sessions"
REPO_LEARNINGS = REPO / "vault" / "concepts" / "arti_live_learnings.md"
RAW_ROOT = REPO / "archive" / "vault-sessions-raw"
IDE_ARCHIVE = REPO / "archive" / "hermes-vault-sessions"

ARTI_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})-arti-live-session-(?P<profile>[^-]+)-(?P<time>\d{6})\.md$"
)


def extract_restart_time(name: str) -> str:
    m = ARTI_RE.match(name)
    if not m:
        return "000000"
    t = m.group("time")
    return f"{t[0:2]}:{t[2:4]}:{t[4:6]}"


def merge_session_body(content: str) -> str:
    """Return body without duplicate top-level title if present."""
    lines = content.splitlines()
    if lines and lines[0].startswith("# Live Stream Session:"):
        # skip until first ## or end of header block
        i = 1
        while i < len(lines) and not lines[i].startswith("## "):
            i += 1
        return "\n".join(lines[i:]).strip()
    return content.strip()


def collect_arti_files() -> list[Path]:
    files: list[Path] = []
    for root in (REPO_SESSIONS, HV_SESSIONS):
        if not root.is_dir():
            continue
        for p in root.glob("*.md"):
            if p.name == "index.md":
                continue
            if ARTI_RE.match(p.name):
                files.append(p)
    return files


def group_by_day(files: list[Path]) -> dict[str, list[Path]]:
    groups: dict[str, list[Path]] = defaultdict(list)
    for p in files:
        m = ARTI_RE.match(p.name)
        if not m:
            continue
        key = f"{m.group('date')}-{m.group('profile')}"
        groups[key].append(p)
    for key in groups:
        groups[key].sort(key=lambda x: ARTI_RE.match(x.name).group("time"))  # type: ignore
    return groups


def archive_raw(files: list[Path]) -> None:
    for p in files:
        m = ARTI_RE.match(p.name)
        if not m:
            continue
        dest_dir = RAW_ROOT / m.group("date")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / p.name
        if not dest.exists():
            shutil.copy2(p, dest)


def archive_ide_sessions() -> int:
    IDE_ARCHIVE.mkdir(parents=True, exist_ok=True)
    count = 0
    if not HV_SESSIONS.is_dir():
        return 0
    for p in HV_SESSIONS.glob("*.md"):
        if ARTI_RE.match(p.name):
            continue
        dest = IDE_ARCHIVE / p.name
        if not dest.exists():
            shutil.copy2(p, dest)
        count += 1
    return count


def write_merged_day(day_key: str, paths: list[Path]) -> Path:
    date_str, profile = day_key.rsplit("-", 1)
    out = REPO_SESSIONS / f"{date_str}-{profile}.md"
    sections: list[str] = [
        f"# Live Stream Session: {date_str} (Profile: {profile})",
        "",
        f"> Merged {len(paths)} bridge restart(s) on {datetime.now().strftime('%Y-%m-%d %H:%M')}. "
        f"Raw files: `archive/vault-sessions-raw/{date_str}/`.",
        "",
    ]
    for p in paths:
        restart = extract_restart_time(p.name)
        body = merge_session_body(p.read_text(encoding="utf-8"))
        sections.append(f"## Restart {restart} (`{p.name}`)")
        sections.append("")
        sections.append(body)
        sections.append("")
    out.write_text("\n".join(sections).rstrip() + "\n", encoding="utf-8")
    return out


def parse_learning_bullets(text: str) -> list[str]:
    bullets: list[str] = []
    in_section = False
    for line in text.splitlines():
        if line.strip().startswith("## Memori Jangka Panjang"):
            in_section = True
            continue
        if in_section and line.strip().startswith("## "):
            break
        if in_section and line.strip().startswith("- "):
            bullets.append(line.strip())
    return bullets


def merge_learnings() -> None:
    repo_bullets = parse_learning_bullets(REPO_LEARNINGS.read_text(encoding="utf-8")) if REPO_LEARNINGS.exists() else []
    hv_bullets: list[str] = []
    if HV_LEARNINGS.exists():
        hv_bullets = parse_learning_bullets(HV_LEARNINGS.read_text(encoding="utf-8"))
        bak = IDE_ARCHIVE / "arti_live_learnings.hermes-vault.bak.md"
        IDE_ARCHIVE.mkdir(parents=True, exist_ok=True)
        shutil.copy2(HV_LEARNINGS, bak)

    seen: set[str] = set()
    merged: list[str] = []
    for b in repo_bullets + hv_bullets:
        norm = re.sub(r"\s+", " ", b.lower())
        if norm in seen:
            continue
        seen.add(norm)
        merged.append(b)

    def sort_key(b: str) -> tuple:
        m = re.match(r"- \[(\d{4}-\d{2}-\d{2})\]", b)
        return (m.group(1) if m else "0000-00-00", b)

    merged.sort(key=sort_key, reverse=True)

    header = """# Arti Live Learnings (Default Profile)

Ini adalah catatan pengetahuan jangka panjang yang dipelajari Arti (VTuber Co-Host) secara otomatis selama sesi live stream untuk profil **default**.

## Memori Jangka Panjang

"""
    body = "\n\n".join(merged) + "\n" if merged else "- (belum ada)\n"
    REPO_LEARNINGS.parent.mkdir(parents=True, exist_ok=True)
    REPO_LEARNINGS.write_text(header + body, encoding="utf-8")


def write_index(groups: dict[str, list[Path]], day_out: dict[str, Path]) -> None:
    rows = ["| Tanggal | File | Restarts |", "|---------|------|----------|"]
    for key in sorted(day_out.keys()):
        date_str, prof = key.rsplit("-", 1)
        rel = f"vault/sessions/{date_str}-{prof}.md"
        n_raw = len(groups.get(key, []))
        rows.append(f"| {date_str} | `{rel}` | {n_raw} |")
    index = REPO_SESSIONS / "index.md"
    content = """# Arti Vault Sessions Index

Satu file per hari kalender (`YYYY-MM-DD-{profile}.md`). File per-restart bridge ada di `archive/vault-sessions-raw/`.

""" + "\n".join(rows) + "\n"
    index.write_text(content, encoding="utf-8")


def main() -> None:
    (REPO / "transcripts").mkdir(exist_ok=True)
    (REPO / "data").mkdir(exist_ok=True)
    (REPO / "transcripts" / ".gitkeep").write_text("", encoding="utf-8")
    (REPO / "data" / ".gitkeep").write_text("", encoding="utf-8")

    ide_count = archive_ide_sessions()
    print(f"Archived {ide_count} Hermes IDE session files -> {IDE_ARCHIVE}")

    all_arti = collect_arti_files()
    archive_raw(all_arti)
    print(f"Raw backup: {len(all_arti)} Arti files -> {RAW_ROOT}")

    groups = group_by_day(all_arti)
    day_out: dict[str, Path] = {}
    for day_key, paths in sorted(groups.items()):
        out = write_merged_day(day_key, paths)
        day_out[day_key] = out
        print(f"Merged {len(paths)} -> {out.name}")

    # Remove per-restart from repo vault/sessions only
    for p in list(REPO_SESSIONS.glob("*-arti-live-session-*.md")):
        p.unlink()
        print(f"Removed old: {p.name}")

    merge_learnings()
    print(f"Merged learnings -> {REPO_LEARNINGS}")

    write_index(groups, day_out)
    print(f"Wrote {REPO_SESSIONS / 'index.md'}")

    # Rename external vault sessions folder
    if HV_SESSIONS.is_dir():
        migrated = HV_SESSIONS.parent / "sessions.migrated"
        if migrated.exists():
            print(f"Skip rename: {migrated} already exists")
        else:
            HV_SESSIONS.rename(migrated)
            print(f"Renamed {HV_SESSIONS} -> {migrated}")


if __name__ == "__main__":
    main()
