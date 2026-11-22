"""Safety/accounting tests. Use the rig venv; these are not live game witnesses."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("free_play_soak", Path(__file__).resolve().parents[1] / "scripts/free_play/soak.py")
soak = importlib.util.module_from_spec(spec)
spec.loader.exec_module(soak)

class HarnessTests(unittest.TestCase):
    def test_staged_mod_preflight_rejects_missing_manifest_or_unpinned_dll(self):
        with tempfile.TemporaryDirectory() as temp:
            rows = []
            for name in ("ArtiStardewBridge", "ArtiHostHelper"):
                folder = Path(temp) / name; folder.mkdir()
                dll = folder / (name + ".dll"); dll.write_bytes(b"pinned build")
                rows.append((folder, dll.name, soak.sha(dll)))
            for folder, dll, _ in rows:
                with self.assertRaisesRegex(RuntimeError, "mod preflight.*manifest.json"):
                    soak.staged_mod_preflight(rows)
                (folder / "manifest.json").write_text(json.dumps(dict(EntryDll=dll)))
            soak.staged_mod_preflight(rows)
            for folder, dll, _ in rows:
                path = folder / dll
                path.write_bytes(b"wrong build")
                with self.assertRaisesRegex(RuntimeError, "mod preflight.*pinned DLL"):
                    soak.staged_mod_preflight(rows)
                path.unlink()
                with self.assertRaisesRegex(RuntimeError, "mod preflight.*pinned DLL"):
                    soak.staged_mod_preflight(rows)
                path.write_bytes(b"pinned build")
                (folder / "manifest.json").write_text(json.dumps(dict(EntryDll="other.dll")))
                with self.assertRaisesRegex(RuntimeError, "mod preflight.*EntryDll"):
                    soak.staged_mod_preflight(rows)
                (folder / "manifest.json").write_text(json.dumps(dict(EntryDll=dll)))
            soak.staged_mod_preflight(rows)

    @unittest.skipUnless(sys.platform == "win32", "patches os.name to 'nt'; pathlib then needs WindowsPath")
    def test_configured_rig_requires_exact_host_and_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            rig = Path(temp)
            cfg = dict(root=str(rig), hostname="LAB-RIG")
            with patch.object(soak, "rig_config", return_value=cfg), \
                    patch.object(soak.os, "name", "nt"), \
                    patch.object(soak.socket, "gethostname", return_value="lab-rig"), \
                    patch.object(soak, "ROOT", rig / "rig-checkout"):
                self.assertEqual(soak.checked_rig(), (cfg, rig.resolve()))
                with patch.object(soak.socket, "gethostname", return_value="OTHER"):
                    with self.assertRaisesRegex(RuntimeError, "configured rig"):
                        soak.checked_rig()
                with patch.object(soak, "ROOT", rig / "unrelated"):
                    with self.assertRaisesRegex(RuntimeError, "configured rig"):
                        soak.checked_rig()

    def test_owner_guard_failure_blocks_before_lock_or_staging(self):
        with patch.object(soak, "checked_rig", return_value=({}, Path("unused"))), \
                patch.object(soak, "owner_idle", side_effect=RuntimeError("owner busy")), \
                patch.object(soak, "_run_soak") as launch:
            with self.assertRaisesRegex(RuntimeError, "owner busy"):
                soak.run_soak(None)
            launch.assert_not_called()

    def test_test_operator_rearm_only_fresh_startup_uses_real_p2_token(self):
        from tools import arti_rearm
        from tests.contracts_support import entries_digest
        # R0-03 §2: a persisted native HALT fact, not just its token key.
        halt = dict(episode_id="startup", halt_seq=1, entries_digest="a" * 64,
                    scope="all", since_ms=1, persisted=True,
                    entries=[dict(n=1, stop_id="startup", stop_sha256="b" * 64,
                        cause="native_unclean_exit", source="native", scope="all",
                        writer="native", latched_ms=1, file_present=False)])
        halt["entries_digest"] = entries_digest(halt["entries"])
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); folder=root/"command"; folder.mkdir(); output=root/"run"; output.mkdir()
            session=folder/"session.json"
            session.write_text(json.dumps(dict(halt=halt)))
            before=soak.manifest(folder)
            with patch.object(soak,"_test_rearm_rig",return_value=True):
                for fresh, current in ((False,halt), (True,dict(halt,entries=[dict(cause="H5",source="native")])),
                                       (True,dict(halt,entries=halt["entries"]+[dict(cause="H1",source="stop")]))):
                    with self.assertRaisesRegex(RuntimeError,"requires fresh"):
                        soak._test_operator_startup_rearm(folder,output,current,fresh_start=fresh)
                    self.assertEqual(soak.manifest(folder),before)
                (output/"enroll.log").touch()
                with self.assertRaisesRegex(RuntimeError,"requires fresh"):
                    soak._test_operator_startup_rearm(folder,output,halt,fresh_start=True)
                (output/"enroll.log").unlink()
            with patch.object(soak,"_test_rearm_rig",return_value=False):
                with self.assertRaisesRegex(RuntimeError,"requires fresh"):
                    soak._test_operator_startup_rearm(folder,output,halt,fresh_start=True)
            tokens=[]
            def native_tick(_):
                token=json.loads((folder/"rearm.json").read_text()); tokens.append(token)
                self.assertEqual(token["actor"],"rig_operator")
                self.assertEqual(arti_rearm._key(token),arti_rearm._key(halt))
                witness=dict(episode_id="startup",halt_seq=1,stop_ids=[],ms=123)
                session.write_text(json.dumps(dict(halt=None,rearmed=witness)))
                (folder/"halt.json").write_text(json.dumps(dict(state="rearmed",rearmed=witness)))
            real_rearm=arti_rearm.rearm
            with patch.object(soak,"_test_rearm_rig",return_value=True), patch.object(arti_rearm,"rearm",
                    side_effect=lambda *a,**k:real_rearm(*a,**k,sleep=native_tick)):
                soak.operator_rearm_before_enrollment(folder,output,test_operator=True,clock=lambda:0,sleep=lambda _:None)
            self.assertEqual(len(tokens),1)
            setup=json.loads((output/"OPERATOR_REARM_SETUP.json").read_text())
            self.assertEqual(setup["causes"],["native_unclean_exit"])
            self.assertEqual(setup["token_id"]["entries_digest"],halt["entries_digest"])
            self.assertEqual(setup["rearmed"]["episode_id"],"startup")
            self.assertTrue((output/"OPERATOR_REARM_COMPLETED.json").exists())

    def test_operator_preflight_requires_matching_native_rearm_and_never_writes_control(self):
        halt = dict(episode_id="episode", halt_seq=3)
        for after, message in ((dict(halt=None, rearmed=dict(episode_id="episode", halt_seq=3)), None),
                               (dict(halt=None, rearmed={}), "without matching"),
                               (dict(halt=dict(episode_id="episode", halt_seq=4)), "HALT changed")):
            with tempfile.TemporaryDirectory() as temp:
                root=Path(temp); folder=root/"command"; folder.mkdir(); output=root/"run"; output.mkdir()
                (folder/"stop.json").write_bytes(b"retained")
                with patch.object(soak,"read",side_effect=[dict(halt=halt),after]), patch.object(soak,"call") as native:
                    if message:
                        with self.assertRaisesRegex(RuntimeError,message):
                            soak.operator_rearm_before_enrollment(folder,output,clock=lambda:0,sleep=lambda _:None)
                    else:
                        soak.operator_rearm_before_enrollment(folder,output,clock=lambda:0,sleep=lambda _:None)
                    native.assert_not_called()
                self.assertEqual((folder/"stop.json").read_bytes(),b"retained")
                self.assertEqual(set(p.name for p in folder.iterdir()),{"stop.json"})
                self.assertEqual((output/"OPERATOR_REARM_COMPLETED.json").exists(),message is None)
                self.assertTrue(json.loads((output/"OPERATOR_REARM_REQUIRED.json").read_text())["before_enrollment"])

    def test_operator_preflight_is_bounded_and_idle_needs_no_operator_step(self):
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)
            with patch.object(soak,"read",return_value=dict(halt=None)):
                soak.operator_rearm_before_enrollment(output,output)
            self.assertEqual(list(output.iterdir()),[])
            with patch.object(soak,"read",return_value=dict(halt=dict(episode_id="e",halt_seq=1))):
                ticks=iter([0,181])
                with self.assertRaisesRegex(RuntimeError,"timed out before enrollment"):
                    soak.operator_rearm_before_enrollment(output,output,clock=lambda:next(ticks))
            self.assertFalse((output/"OPERATOR_REARM_COMPLETED.json").exists())

    def test_candidate_dependencies_are_pinned_and_restore_absence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "candidate"; source.mkdir()
            dll = source / "ArtiStardewBridge.dll"; dll.write_bytes(b"bridge")
            dependency = source / "ArtiCore.Pure.dll"; dependency.write_bytes(b"pure")
            provenance = {"dependencies": {dependency.name: soak.sha(dependency)}}
            self.assertEqual(soak.candidate_dependencies(dll, provenance), {dependency.name: dependency})
            target = root / "installed"; target.mkdir(); saves = root / "saves"; saves.mkdir()
            baseline = soak.Baseline(root / "run", {"dependency-pure.dll": target / dependency.name}, saves)
            baseline.output.mkdir(); baseline.capture()
            soak.shutil.copy2(dependency, target / dependency.name)
            self.assertTrue(baseline.restore()["files_identical"])
            self.assertFalse((target / dependency.name).exists())
            self.assertEqual((baseline.output / "created-dependency-pure.dll").read_bytes(), b"pure")
            dependency.write_bytes(b"changed")
            with self.assertRaises(RuntimeError): soak.candidate_dependencies(dll, provenance)
            for name in ("../outside.dll", "..\\outside.dll", dll.name, "config.json"):
                with self.assertRaises(ValueError): soak.candidate_dependencies(dll, {"dependencies": {name: "ignored"}})

    def test_native_save_wait_allows_bounded_summary_safety_without_claiming_progress(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); heartbeat = root / "heartbeat"; barrier = root / "barrier.json"
            heartbeat.write_text("native heartbeat"); os.utime(heartbeat, (1000, 1000))
            barrier.write_text(json.dumps(dict(saving=True, stopped=False))); os.utime(barrier, (1000, 1000))
            self.assertFalse(soak.host_telemetry_lost(root, 1060))
            self.assertFalse(soak.host_telemetry_lost(root, 1000 + soak.DAY_BOUNDARY_QUIET_MAX_S - 1))
            self.assertTrue(soak.host_telemetry_lost(root, 1000 + soak.DAY_BOUNDARY_QUIET_MAX_S + 1))
            (root / "HOST_STOP").touch()
            self.assertTrue(soak.host_telemetry_lost(root, 1001))

    def test_stale_missing_or_unproven_host_barrier_does_not_extend_heartbeat(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); heartbeat = root / "heartbeat"; barrier = root / "barrier.json"
            self.assertTrue(soak.host_telemetry_lost(root, 1000))
            heartbeat.write_text("native heartbeat"); os.utime(heartbeat, (1000, 1000))
            self.assertFalse(soak.host_telemetry_lost(root, 1015))
            self.assertTrue(soak.host_telemetry_lost(root, 1016))
            for state in (dict(saving=False, stopped=False), dict(saving=True, stopped=True), dict(saving=True), {}):
                barrier.write_text(json.dumps(state)); os.utime(barrier, (1000, 1000))
                self.assertTrue(soak.host_telemetry_lost(root, 1020))
            barrier.write_text(json.dumps(dict(saving=True, stopped=False))); os.utime(barrier, (990, 990))
            self.assertTrue(soak.host_telemetry_lost(root, 1020))

    def test_a9_night11_save_menu_is_a_day_boundary_only_after_sleep_acceptance(self):
        # Attempt 9 (R/host/host.jsonl:6979-6994): the HOST accepted sleep, then entered SaveGameMenu 0.7 s
        # after its last heartbeat and UpdateTicked stopped. fix11b: the menu row alone is no evidence; the
        # HOST's own sleep acceptance opens the one day-boundary window (one hard bound).
        accept = dict(ev="host_answer", at=995.0, facts=dict(answer="Yes", gift=False))
        def rows(*extra, opened=True):
            return [*([accept] if opened else []), dict(ev="native_heartbeat", at=1000.0),
                    dict(ev="host_menu", at=1000.7, facts=dict(type="SaveGameMenu")), *extra]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); heartbeat = root / "heartbeat"; barrier = root / "barrier.json"
            heartbeat.write_text("native heartbeat"); os.utime(heartbeat, (1000, 1000))
            barrier.write_text(json.dumps(dict(saving=False, stopped=False))); os.utime(barrier, (1000, 1000))
            log = root / "host.jsonl"
            self.assertTrue(soak.host_telemetry_lost(root, 1017))  # no host.jsonl: real loss still stops
            log.write_text("\n".join(json.dumps(r) for r in rows()) + "\n")
            self.assertFalse(soak.host_telemetry_lost(root, 1017))
            self.assertFalse(soak.host_telemetry_lost(root, 1000.7 + soak.DAY_BOUNDARY_QUIET_MAX_S - 1))
            self.assertTrue(soak.host_telemetry_lost(root, 1000.7 + soak.DAY_BOUNDARY_QUIET_MAX_S + 1))
            for bad in (rows(opened=False), [accept | dict(at=900.0), dict(ev="host_menu", at=901.0, facts=dict(type="SaveGameMenu"))],
                        rows(dict(ev="HOST_SAFETY_STOP", at=1001.0)),
                        rows(dict(ev="native_saved", at=1001.0), dict(ev="native_heartbeat", at=1001.5))):
                log.write_text("\n".join(json.dumps(r) for r in bad) + "\n")
                if bad[-1]["ev"] == "native_heartbeat": os.utime(heartbeat, (1001.5, 1001.5))
                self.assertTrue(soak.host_telemetry_lost(root, 1017.6), bad[-1])
                os.utime(heartbeat, (1000, 1000))
            log.write_text("\n".join(json.dumps(r) for r in rows()) + "\n{torn")
            self.assertFalse(soak.host_telemetry_lost(root, 1017))  # a torn tail line earns nothing, takes nothing
            (root / "HOST_STOP").touch()
            self.assertTrue(soak.host_telemetry_lost(root, 1001))

    def test_restore_exact_file_set_and_preserve_test_world(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); saves = root / "Saves"; (saves / "real").mkdir(parents=True)
            (saves / "real/save").write_bytes(b"original")
            cfg = root / "config"; cfg.write_bytes(b"private baseline")
            out = root / "out"; out.mkdir()
            b = soak.Baseline(out, {"config":cfg, "missing":root/"missing"}, saves, owned_saves=('real', 'test')); b.capture()
            cfg.write_bytes(b"candidate"); (root/"missing").write_bytes(b"created")
            (saves / "real/save").write_bytes(b"changed"); (saves/"real/extra").write_bytes(b"extra")
            (saves/"test").mkdir(); (saves/"test/save").write_bytes(b"test world")
            self.assertTrue(b.restore()["saves_identical"])
            self.assertEqual((saves/"real/save").read_bytes(), b"original")
            self.assertFalse((saves/"real/extra").exists())
            self.assertFalse((root/"missing").exists())
            self.assertEqual((out/"preserved-saves/test/save").read_bytes(), b"test world")

    def test_restore_keeps_the_clean_exit_witness_paired_with_the_retained_occurrence_ring(self):
        # PM-1 3b F#10: the restore reverted mod_exit.json to absent while the occurrence ring kept naming the
        # exited runtime, so native StartHalt latched native_unclean_exit on every clean launch.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); saves = root / "Saves"; saves.mkdir(); inbox = root / "inbox"; inbox.mkdir()
            out = root / "out"; out.mkdir()
            files = {"n1-" + name: inbox / name for name in soak.N1_RESTORED}
            b = soak.Baseline(out, files, saves); b.capture()
            (inbox / "occurrences.1.7.json").write_text(json.dumps(dict(runtime_id="rt-a", save_id="1", player_id="7")))
            (inbox / "mod_exit.json").write_text(json.dumps(dict(runtime_id="rt-a", ms=1)))  # ProcessExit witness
            (inbox / "halt.json").write_text("{}")
            self.assertTrue(b.restore()["files_identical"])
            self.assertNotIn("mod_exit.json", soak.N1_RESTORED)
            self.assertFalse((inbox / "halt.json").exists())
            ring = json.loads((inbox / "occurrences.1.7.json").read_text())
            self.assertEqual(json.loads((inbox / "mod_exit.json").read_text())["runtime_id"], ring["runtime_id"])

    def test_changed_backup_refuses_before_target_write(self):
        with tempfile.TemporaryDirectory() as temp:
            r=Path(temp); (r/"saves").mkdir(); (r/"out").mkdir()
            c=r/"config"; c.write_bytes(b"baseline")
            b=soak.Baseline(r/"out", {"config":c}, r/"saves"); b.capture()
            (b.base/"config").write_bytes(b"tampered"); c.write_bytes(b"candidate")
            with self.assertRaises(RuntimeError): b.restore()
            self.assertEqual(c.read_bytes(), b"candidate")

    def test_metrics_never_promote_verified_without_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            run=Path(temp); j=run/"runtime/live7/c1/day-0001-attempt-000/journal/g1_journal.jsonl"; j.parent.mkdir(parents=True)
            data=[dict(ev="part_live_row", skill="siram", outcome_class="VERIFIED", verified=True),
                  dict(ev="part_live_row", skill="siram", outcome_class="VERIFIED", evidence=dict(cleanup=dict(input_released=True,menu=None,free=True))),
                  dict(ev="part_live_row", skill="siram", outcome_class="CLEAN_FAIL", evidence=dict(cleanup=dict(input_released=True,menu=None,free=True))),
                  dict(ev="part_live_row", skill="new_unknown", outcome_class="SAFETY_STOP")]
            j.write_text("\n".join(json.dumps(x) for x in data))
            m=soak.metrics(run)
            self.assertEqual(m["families"]["tools_world"],dict(attempts=3,verified_effects=1,clean_refusals=1,uncertain=1,safety_stops=0,recovery_faults=0))
            self.assertEqual(m["families"]["unmapped"]["safety_stops"],1)
            self.assertIsNone(m["longest_no_progress_gap_s"])

    def test_wrong_map_and_original_collision_refuse_without_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            r=Path(temp); t=r/"World_123"; t.mkdir(); saves=r/"saves"; saves.mkdir()
            (t/t.name).write_text('<SaveGame><whichFarm>4</whichFarm></SaveGame>')
            before=soak.manifest(saves)
            with self.assertRaises(ValueError): soak.stage_template(t,saves,"Standard",r)
            self.assertEqual(soak.manifest(saves),before)

    def test_valid_staging_changes_only_declared_host_fields_and_cheats(self):
        with tempfile.TemporaryDirectory() as temp:
            r=Path(temp); template=r/"World_99"; template.mkdir(); saves=r/"saves"; saves.mkdir()
            raw=b'<SaveGame><whichFarm>0</whichFarm><uniqueIDForThisGame>99</uniqueIDForThisGame><allowChatCheats>false</allowChatCheats><player><UniqueMultiplayerID>11</UniqueMultiplayerID><lastSleepLocation>Cabin</lastSleepLocation><disconnectLocation>Cabin</disconnectLocation></player><farmhands><Farmer><UniqueMultiplayerID>22</UniqueMultiplayerID><name>Arti</name><lastSleepLocation>Cabin</lastSleepLocation><items><waterLeft>0</waterLeft></items></Farmer></farmhands></SaveGame>'
            (template/template.name).write_bytes(raw)
            result=soak.stage_template(template,saves,"Standard",r)
            staged=soak.ET.parse(saves/template.name/template.name).getroot()
            original=soak.ET.fromstring(raw)
            self.assertEqual(result["farmhand_id"],22)
            self.assertEqual(staged.findtext("allowChatCheats"),"true")
            self.assertEqual(staged.findtext("player/lastSleepLocation"),"")
            self.assertEqual(soak.ET.tostring(staged.find("farmhands")),soak.ET.tostring(original.find("farmhands")))
            self.assertEqual((template/template.name).read_bytes(),raw)
            before=soak.manifest(saves)
            with self.assertRaises(ValueError): soak.stage_template(template,saves,"Standard",r)
            self.assertEqual(soak.manifest(saves),before)

    def test_native_save_qname_namespace_survives_staging(self):
        with tempfile.TemporaryDirectory() as temp:
            r=Path(temp); template=r/'World_99'; template.mkdir(); saves=r/'saves'; saves.mkdir()
            raw=b'<SaveGame xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema"><whichFarm>0</whichFarm><uniqueIDForThisGame>99</uniqueIDForThisGame><player><UniqueMultiplayerID>11</UniqueMultiplayerID><value xsi:type="xsd:int">7</value></player><farmhands><Farmer><UniqueMultiplayerID>22</UniqueMultiplayerID><name>Arti</name></Farmer></farmhands></SaveGame>'
            (template/template.name).write_bytes(raw)
            soak.stage_template(template,saves,'Standard',r)
            staged=saves/template.name/template.name
            declarations=dict(value for _,value in soak.ET.iterparse(staged,events=('start-ns',)))
            self.assertEqual(declarations.get('xsd'),'http://www.w3.org/2001/XMLSchema')
            self.assertEqual(soak.ET.parse(staged).find('player/value').attrib['{http://www.w3.org/2001/XMLSchema-instance}type'],'xsd:int')
            self.assertEqual((template/template.name).read_bytes(),raw)
    def test_containment_rejects_root_and_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            r=Path(temp)
            for target in (r,r/"../outside"):
                with self.assertRaises(ValueError): soak.contained(target,r)

    def test_recorder_waits_for_complete_lines_and_ignores_clock_noise(self):
        with tempfile.TemporaryDirectory() as temp:
            run=Path(temp); j=run/"runtime/live7/c1/day-0001-attempt-000/journal/g1_journal.jsonl"; j.parent.mkdir(parents=True)
            recorder=soak.JournalRecorder(run)
            raw=json.dumps(dict(ev="action_result",action=dict(world_effect_status="observed_change",cleanup_confirmed=True)))
            j.write_text(raw[:12]); recorder.poll()
            self.assertFalse((run/"timeline.jsonl").exists())
            with j.open("a") as f: f.write(raw[12:]+"\n"+json.dumps(dict(ev="heartbeat",time_of_day=1700))+"\n")
            recorder.poll(); recorder.poll()
            timeline=[r for _,r in soak.rows(run/"timeline.jsonl")]
            self.assertEqual(len(timeline),2)
            self.assertTrue(timeline[0]["semantic_progress"])
            self.assertFalse(timeline[1]["semantic_progress"])

    def test_replay_refusal_does_not_modify_an_existing_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            run=Path(temp); (run/"REPORT.md").write_bytes(b"original completed receipt")
            argv=["soak.py","soak","0.5h","Wilderness","--output",str(run)]
            for name in ("dll","helper","template","host-template","trial-config","provenance"):
                argv += ["--"+name,"unused"]
            for name in ("code-sha","dll-sha256","helper-sha256"):
                argv += ["--"+name,"unused"]
            with patch.object(soak.sys,"argv",argv),patch.object(soak,"run_soak",side_effect=RuntimeError("output already exists")),patch.object(soak.sys,"stderr"):
                self.assertEqual(soak.main(),2)
            self.assertEqual(soak.manifest(run),{"REPORT.md":soak.sha(run/"REPORT.md")})
            self.assertEqual((run/"REPORT.md").read_bytes(),b"original completed receipt")

if __name__ == "__main__": unittest.main()
