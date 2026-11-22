"""OBS-2B1 Tier-3: kolektor bukti live READ-ONLY di atas jalur produksi.

Yang dikerjakan modul ini: menerima event yang SUDAH lolos `decode_event`
(dari `StardewReceiver`), meneruskannya ke `StardewStateStore` (MOTOR_STATE)
dan `RichObservationStore` (BRAIN_OBSERVATION) persis seperti produksi, lalu
mencatat bukti ringkas yang bisa disalin ke handoff Tier-3:

  - ringkasan snapshot/delta/session/revision per (player, domain, session)
  - ID upsert/remove tiap object delta yang diterima
  - hasil `RichObservationStore.apply` (accepted/ignored/rejected + alasan)
  - keadaan akhir scene: stale / requires_resync / complete
  - kesehatan MOTOR_STATE (rate, celah antar-state maksimum, umur terakhir)
  - bukti pemisahan MOTOR_STATE vs BRAIN_OBSERVATION
  - error callback (dicatat, tidak mematikan receiver)

Yang TIDAK dikerjakan: tidak mengirim input, tidak menyentuh dunia game,
tidak menulis balik ke socket, tidak menyimpan banjir state 10 Hz mentah
(state hanya menaikkan penghitung kesehatan). Menjalankan kolektor ini di
laptop TIDAK otomatis = LOCAL_VERIFIED; label itu tetap menuntut checklist
Tier-3 di docs/stardew/OBS-2B1-TIER3-RUNBOOK.md dinilai manusia.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from arti_stardew import StardewStateStore
from arti_stardew_observation import (
    KIND_DELTA,
    OBSERVATION_EVENT,
    ApplyResult,
    RichObservationStore,
)
from arti_stardew_scene import SCENE_DOMAIN

# Batas memori bukti. Delta objek digerakkan event (bukan 10 Hz), jadi batas
# ini longgar untuk soak berjam-jam; kalau terlampaui, baris TERLAMA dibuang
# dan `bukti_terbuang` menghitungnya — bukti struktural (proof pertama,
# histogram, sesi) disimpan terpisah dan tidak ikut terbuang.
MAX_BARIS_BUKTI = 500
MAX_CELAH_PER_SESI = 20
MAX_ERROR_CALLBACK = 20
MAX_ID_DITAMPILKAN = 4


def _format_ids(ids: tuple[str, ...], batas: int = MAX_ID_DITAMPILKAN) -> str:
    tampil = ",".join(ids[:batas])
    if len(ids) > batas:
        tampil += f",+{len(ids) - batas}"
    return f"[{tampil}]"


@dataclass(frozen=True)
class BuktiDeltaObjek:
    """Delta objek PERTAMA yang diterima store dengan isi non-kosong."""

    player_id: int
    session_id: str
    seq: int
    domain_revision: int
    upsert_ids: tuple[str, ...]
    remove_ids: tuple[str, ...]


@dataclass
class _SesiObservasi:
    player_id: int
    domain: str
    session_id: str
    seq_pertama: int
    revisi_pertama: int
    revisi_terakhir: int
    diterima_per_kind: dict[str, int] = field(default_factory=dict)
    # (dari_revisi, ke_revisi, kind) untuk tiap revisi diterima yang BUKAN
    # revisi_terakhir + 1. Snapshot boleh lompat (revision di-reserve saat
    # warp race); delta yang lompat = cacat — store menolaknya, jadi lompatan
    # delta di daftar ini seharusnya mustahil.
    celah: list[tuple[int, int, str]] = field(default_factory=list)
    celah_terbuang: int = 0

    @property
    def berurutan(self) -> bool:
        return not self.celah and self.celah_terbuang == 0


@dataclass
class _KesehatanState:
    events: int = 0
    pertama: float | None = None
    terakhir: float | None = None
    celah_maks: float = 0.0


class KolektorBuktiTier3:
    """Kolektor bukti read-only. Aman dipanggil dari thread receiver."""

    def __init__(
        self,
        *,
        jam: Callable[[], float] = time.monotonic,
        max_baris_bukti: int = MAX_BARIS_BUKTI,
        on_baris: Callable[[str], None] | None = None,
    ) -> None:
        self._jam = jam
        self._on_baris = on_baris
        # Lock milik store masing-masing melindungi isi store; penghitung
        # kolektor sendiri hanya disentuh dari on_event, yang dipanggil
        # berurutan per koneksi oleh handler receiver.
        self.motor = StardewStateStore(jam=jam)
        self.brain = RichObservationStore()

        self._bukti: deque[str] = deque(maxlen=max(1, int(max_baris_bukti)))
        self.bukti_terbuang = 0

        self.n_state = 0
        self.n_hello = 0
        self.n_observation = 0
        self.n_lain = 0

        self._kesehatan: dict[int, _KesehatanState] = {}

        self.apply_accepted = 0
        self.apply_ignored = 0
        self.apply_rejected = 0
        self.apply_alasan: dict[str, int] = {}

        self._sesi: dict[tuple[int, str, str], _SesiObservasi] = {}
        self._urutan_sesi: list[tuple[int, str, str]] = []

        self.delta_scene_diterima = 0
        self.delta_scene_kosong = 0
        self.bukti_delta_objek: BuktiDeltaObjek | None = None

        # Pemisahan MOTOR_STATE vs BRAIN_OBSERVATION:
        # - event non-observation yang masuk brain wajib ignored not_observation
        # - event observation TIDAK boleh mengubah snapshot motor
        self.brain_abaikan_nonobservasi = 0
        self.motor_berubah_saat_observation = 0

        self.callback_error: list[str] = []
        self.callback_error_total = 0

    # --- masuk -----------------------------------------------------------
    def on_event(self, event: dict) -> None:
        """Tidak pernah melempar: error dicatat sebagai bukti, bukan crash."""
        try:
            self._proses(event)
        except Exception as exc:  # noqa: BLE001 — receiver harus tetap hidup
            self.callback_error_total += 1
            if len(self.callback_error) < MAX_ERROR_CALLBACK:
                self.callback_error.append(f"{type(exc).__name__}: {exc}")

    def _proses(self, event: dict) -> None:
        jenis = event.get("ev")
        pid = event.get("player_id")

        motor_sebelum = (
            self.motor.latest(pid) if jenis == OBSERVATION_EVENT else None
        )

        # Jalur produksi: SEMUA event lewat kedua store. Masing-masing
        # mengabaikan jenis yang bukan miliknya — dan pengabaian itulah
        # yang dibuktikan penghitung pemisahan di bawah.
        self.motor.catat(event)
        hasil = self.brain.apply(event)

        if jenis == "hello":
            self.n_hello += 1
            self._catat_baris(
                f"[hello] pid={pid} screen={event.get('screen_id')} "
                f"nama={event.get('player_name')} "
                f"utama={event.get('is_main_player')} seq={event.get('seq')}"
            )
        elif jenis == "state":
            self.n_state += 1
            self._catat_kesehatan(pid)
        elif jenis == OBSERVATION_EVENT:
            self.n_observation += 1
            self._catat_observasi(event, hasil)
            if self.motor.latest(pid) is not motor_sebelum:
                self.motor_berubah_saat_observation += 1
        else:
            self.n_lain += 1

        if jenis != OBSERVATION_EVENT:
            if hasil.ignored and hasil.reason == "not_observation":
                self.brain_abaikan_nonobservasi += 1

    def _catat_kesehatan(self, pid: int) -> None:
        sekarang = self._jam()
        k = self._kesehatan.setdefault(pid, _KesehatanState())
        k.events += 1
        if k.pertama is None:
            k.pertama = sekarang
        if k.terakhir is not None:
            k.celah_maks = max(k.celah_maks, sekarang - k.terakhir)
        k.terakhir = sekarang

    def _catat_observasi(self, event: dict, hasil: ApplyResult) -> None:
        if hasil.accepted:
            self.apply_accepted += 1
        elif hasil.ignored:
            self.apply_ignored += 1
        elif hasil.rejected:
            self.apply_rejected += 1
        if hasil.reason:
            self.apply_alasan[hasil.reason] = (
                self.apply_alasan.get(hasil.reason, 0) + 1
            )

        payload = event.get("payload") or {}
        pid = event.get("player_id")
        seq = event.get("seq")
        kind = payload.get("kind")
        domain = payload.get("domain")
        sesi = payload.get("session_id")
        revisi = payload.get("domain_revision")

        upsert_ids: tuple[str, ...] = ()
        remove_ids: tuple[str, ...] = ()
        if kind == KIND_DELTA:
            body = payload.get("body") or {}
            if isinstance(body, dict):
                upserts = body.get("upserts")
                if isinstance(upserts, Mapping):
                    upsert_ids = tuple(sorted(upserts))
                removes = body.get("removes")
                if isinstance(removes, list):
                    remove_ids = tuple(sorted(str(r) for r in removes))

        if hasil.accepted:
            self._catat_sesi(pid, domain, sesi, seq, revisi, kind)
            if kind == KIND_DELTA and domain == SCENE_DOMAIN:
                if upsert_ids or remove_ids:
                    self.delta_scene_diterima += 1
                    if self.bukti_delta_objek is None:
                        self.bukti_delta_objek = BuktiDeltaObjek(
                            player_id=pid,
                            session_id=str(sesi),
                            seq=int(seq),
                            domain_revision=int(revisi),
                            upsert_ids=upsert_ids,
                            remove_ids=remove_ids,
                        )
                else:
                    self.delta_scene_kosong += 1

        if hasil.accepted:
            vonis = "TERIMA" + (" resync" if hasil.requires_resync else "")
            if hasil.reason:
                vonis += f" ({hasil.reason})"
        else:
            macam = "ABAIKAN" if hasil.ignored else "TOLAK"
            vonis = f"{macam} ({hasil.reason}" + (
                ", resync)" if hasil.requires_resync else ")"
            )
        baris = (
            f"[obs] seq={seq} pid={pid} ses={sesi} {domain} "
            f"{kind} rev={revisi} -> {vonis}"
        )
        if kind == KIND_DELTA:
            baris += (
                f" upserts={len(upsert_ids)}{_format_ids(upsert_ids)}"
                f" removes={len(remove_ids)}{_format_ids(remove_ids)}"
            )
        self._catat_baris(baris)

    def _catat_sesi(
        self,
        pid: int,
        domain: str,
        session_id: str,
        seq: int,
        revisi: int,
        kind: str,
    ) -> None:
        kunci = (pid, str(domain), str(session_id))
        sesi = self._sesi.get(kunci)
        if sesi is None:
            sesi = _SesiObservasi(
                player_id=pid,
                domain=str(domain),
                session_id=str(session_id),
                seq_pertama=int(seq),
                revisi_pertama=int(revisi),
                revisi_terakhir=int(revisi),
            )
            self._sesi[kunci] = sesi
            self._urutan_sesi.append(kunci)
        else:
            if revisi != sesi.revisi_terakhir + 1:
                if len(sesi.celah) < MAX_CELAH_PER_SESI:
                    sesi.celah.append(
                        (sesi.revisi_terakhir, int(revisi), str(kind))
                    )
                else:
                    sesi.celah_terbuang += 1
            sesi.revisi_terakhir = int(revisi)
        sesi.diterima_per_kind[str(kind)] = (
            sesi.diterima_per_kind.get(str(kind), 0) + 1
        )

    def _catat_baris(self, baris: str) -> None:
        if len(self._bukti) == self._bukti.maxlen:
            self.bukti_terbuang += 1
        self._bukti.append(baris)
        if self._on_baris is not None:
            self._on_baris(baris)

    # --- keluar ----------------------------------------------------------
    @property
    def bukti(self) -> tuple[str, ...]:
        return tuple(self._bukti)

    def ringkasan(self) -> dict:
        """Ringkasan deterministik: input sama + jam sama = dict sama."""
        sekarang = self._jam()

        motor_state: dict[str, dict] = {}
        for pid in sorted(self._kesehatan):
            k = self._kesehatan[pid]
            rate = None
            if (
                k.events >= 2
                and k.pertama is not None
                and k.terakhir is not None
                and k.terakhir > k.pertama
            ):
                rate = round((k.events - 1) / (k.terakhir - k.pertama), 3)
            motor_state[str(pid)] = {
                "events": k.events,
                "rate_hz": rate,
                "celah_maks_s": round(k.celah_maks, 3),
                "umur_terakhir_s": (
                    None if k.terakhir is None
                    else round(sekarang - k.terakhir, 3)
                ),
            }

        sesi_list = []
        semua_berurutan = True
        for kunci in self._urutan_sesi:
            s = self._sesi[kunci]
            semua_berurutan = semua_berurutan and s.berurutan
            sesi_list.append({
                "player_id": str(s.player_id),
                "domain": s.domain,
                "session_id": s.session_id,
                "seq_pertama": s.seq_pertama,
                "revisi_pertama": s.revisi_pertama,
                "revisi_terakhir": s.revisi_terakhir,
                "diterima_per_kind": {
                    k: s.diterima_per_kind[k]
                    for k in sorted(s.diterima_per_kind)
                },
                "celah": [list(c) for c in s.celah],
                "celah_terbuang": s.celah_terbuang,
                "berurutan": s.berurutan,
            })

        scene_akhir: dict[str, dict] = {}
        pids = sorted({k[0] for k in self._urutan_sesi} | set(self._kesehatan))
        for pid in pids:
            rec = self.brain.get(pid, SCENE_DOMAIN)
            if rec is None:
                continue
            scene_akhir[str(pid)] = {
                "session_id": rec.session_id,
                "kind": rec.kind,
                "domain_revision": rec.domain_revision,
                "available": rec.available,
                "stale": rec.stale,
                "requires_resync": rec.requires_resync,
                "complete": rec.is_complete,
                "jumlah_fakta": len(rec.facts),
            }

        n_nonobservasi = self.n_state + self.n_hello + self.n_lain
        pemisahan_bersih = (
            self.brain_abaikan_nonobservasi == n_nonobservasi
            and self.motor_berubah_saat_observation == 0
        )

        proof = None
        if self.bukti_delta_objek is not None:
            b = self.bukti_delta_objek
            proof = {
                "player_id": str(b.player_id),
                "session_id": b.session_id,
                "seq": b.seq,
                "domain_revision": b.domain_revision,
                "upsert_ids": list(b.upsert_ids),
                "remove_ids": list(b.remove_ids),
            }

        return {
            "protokol": {
                "state": self.n_state,
                "hello": self.n_hello,
                "observation": self.n_observation,
                "lain": self.n_lain,
                "baris_bukti": len(self._bukti),
                "bukti_terbuang": self.bukti_terbuang,
            },
            "callback_error": {
                "jumlah": self.callback_error_total,
                "contoh": list(self.callback_error),
            },
            "motor_state": motor_state,
            "pemisahan": {
                "nonobservasi_total": n_nonobservasi,
                "brain_abaikan_nonobservasi": self.brain_abaikan_nonobservasi,
                "motor_berubah_saat_observation":
                    self.motor_berubah_saat_observation,
                "bersih": pemisahan_bersih,
            },
            "apply": {
                "accepted": self.apply_accepted,
                "ignored": self.apply_ignored,
                "rejected": self.apply_rejected,
                "alasan": {
                    k: self.apply_alasan[k] for k in sorted(self.apply_alasan)
                },
            },
            "sesi": sesi_list,
            "revisi_berurutan": semua_berurutan,
            "delta_scene_diterima": self.delta_scene_diterima,
            "delta_scene_kosong": self.delta_scene_kosong,
            "bukti_delta_objek": proof,
            "scene_akhir": scene_akhir,
        }

    def baris_ringkas(self) -> list[str]:
        """Ringkasan teks pendek dan deterministik untuk disalin ke handoff."""
        r = self.ringkasan()
        p = r["protokol"]
        baris = [
            "=== BUKTI OBS-2B1 TIER-3 (read-only) ===",
            (
                f"event: state={p['state']} hello={p['hello']} "
                f"observation={p['observation']} lain={p['lain']} "
                f"bukti_terbuang={p['bukti_terbuang']}"
            ),
            (
                f"apply: accepted={r['apply']['accepted']} "
                f"ignored={r['apply']['ignored']} "
                f"rejected={r['apply']['rejected']} "
                f"alasan={r['apply']['alasan']}"
            ),
            (
                f"callback_error: {r['callback_error']['jumlah']}"
                + (
                    f" {r['callback_error']['contoh']}"
                    if r["callback_error"]["contoh"] else ""
                )
            ),
        ]
        for pid, k in r["motor_state"].items():
            baris.append(
                f"MOTOR_STATE pid={pid}: events={k['events']} "
                f"rate={k['rate_hz']}Hz celah_maks={k['celah_maks_s']}s "
                f"umur_terakhir={k['umur_terakhir_s']}s"
            )
        pem = r["pemisahan"]
        baris.append(
            f"pemisahan MOTOR vs BRAIN: "
            f"{'BERSIH' if pem['bersih'] else 'PELANGGARAN'} "
            f"(brain abaikan {pem['brain_abaikan_nonobservasi']}/"
            f"{pem['nonobservasi_total']} non-observasi, "
            f"motor berubah saat observation="
            f"{pem['motor_berubah_saat_observation']})"
        )
        for s in r["sesi"]:
            urut = "BERURUTAN" if s["berurutan"] else f"CELAH {s['celah']}"
            baris.append(
                f"sesi {s['session_id']} ({s['domain']} pid={s['player_id']}): "
                f"rev {s['revisi_pertama']}->{s['revisi_terakhir']} "
                f"kind={s['diterima_per_kind']} {urut}"
            )
        baris.append(
            "revisi berurutan (semua sesi): "
            + ("YA" if r["revisi_berurutan"] else "TIDAK")
        )
        if r["bukti_delta_objek"] is None:
            baris.append(
                "bukti delta objek diterima: BELUM ADA "
                f"(delta scene non-kosong diterima={r['delta_scene_diterima']})"
            )
        else:
            b = r["bukti_delta_objek"]
            baris.append(
                f"bukti delta objek diterima: seq={b['seq']} "
                f"ses={b['session_id']} rev={b['domain_revision']} "
                f"upserts={_format_ids(tuple(b['upsert_ids']))} "
                f"removes={_format_ids(tuple(b['remove_ids']))} "
                f"(total non-kosong={r['delta_scene_diterima']}, "
                f"kosong={r['delta_scene_kosong']})"
            )
        for pid, sc in r["scene_akhir"].items():
            baris.append(
                f"scene akhir pid={pid}: kind={sc['kind']} "
                f"rev={sc['domain_revision']} stale={sc['stale']} "
                f"resync={sc['requires_resync']} complete={sc['complete']} "
                f"fakta={sc['jumlah_fakta']} ses={sc['session_id']}"
            )
        return baris
