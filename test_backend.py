import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import backend as b

SID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
TURN = "33333333-3333-4333-8333-333333333333"


def event(kind, turn=TURN, command="echo example"):
    return {"session_id": SID, "turn_id": turn, "hook_event_name": kind,
            "tool_name": "Bash", "tool_input": {"command": command}}


class Tests(unittest.TestCase):
    def test_states_require_lifecycle_and_live_process(self):
        self.assertEqual(b.state_for("inProgress", TURN, 100, None, {}, True)[0], "running")
        self.assertEqual(b.state_for("inProgress", TURN, 100, None, {}, False)[0], "stopped")
        self.assertEqual(b.state_for("inProgress", TURN, 100, None, {}, None)[0], "unknown")
        self.assertEqual(b.state_for(None, TURN, 100, None, {}, True)[0], "unknown")
        for state in ("completed", "interrupted", "failed"):
            self.assertEqual(b.state_for(state, TURN, 100, 200, {}, True)[0], "stopped")

    def test_approval_and_resolution(self):
        h = b.hook_update({}, event("PermissionRequest"), 110)
        self.assertEqual(b.state_for("inProgress", TURN, 100, None, h, True)[0], "waiting")
        h = b.hook_update(h, event("PostToolUse"), 120)
        self.assertEqual(b.state_for("inProgress", TURN, 100, None, h, True)[0], "running")

    def test_parallel_approvals_do_not_clear_each_other(self):
        h = b.hook_update({}, event("PermissionRequest", command="one"), 110)
        h = b.hook_update(h, event("PermissionRequest", command="two"), 111)
        h = b.hook_update(h, event("PostToolUse", command="one"), 112)
        self.assertEqual(b.state_for("inProgress", TURN, 100, None, h, True)[0], "waiting")
        h = b.hook_update(h, event("Stop"), 113)
        self.assertEqual(b.state_for("inProgress", TURN, 100, None, h, True)[0], "stopped")

    def test_identical_parallel_approvals_are_counted(self):
        h = b.hook_update({}, event("PermissionRequest"), 110)
        h = b.hook_update(h, event("PermissionRequest"), 111)
        h = b.hook_update(h, event("PostToolUse"), 112)
        self.assertEqual(b.state_for("inProgress", TURN, 100, None, h, True)[0], "waiting")

    def test_delayed_and_stale_hooks(self):
        h = b.hook_update({}, event("PermissionRequest"), 110)
        h = b.hook_update(h, event("Stop", turn=OTHER), 120)
        h = b.hook_update(h, event("PermissionRequest", turn=OTHER), 121)
        self.assertEqual(b.state_for("inProgress", TURN, 100, None, h, True)[0], "waiting")
        self.assertEqual(b.state_for("inProgress", TURN, 200, None, h, True)[0], "running")
        self.assertEqual(b.state_for("completed", TURN, 100, 130, h, True)[0], "stopped")

    def test_no_secrets_retained_and_permission_description_does_not_break_matching(self):
        e = event("PermissionRequest", command="SECRET")
        e["tool_input"]["description"] = "approval rationale"
        e["prompt"] = "USER SECRET"
        h = b.hook_update({}, e, 110)
        self.assertNotIn("SECRET", json.dumps(h))
        h = b.hook_update(h, event("PostToolUse", command="SECRET"), 120)
        self.assertEqual(b.state_for("inProgress", TURN, 100, None, h, True)[0], "running")

    def test_hook_config_preserves_others_and_is_idempotent(self):
        config = {"other": True, "hooks": {"SessionStart": [{"hooks": [{"command": "time-tracker"}]}],
                  "Stop": [{"hooks": [{"command": "another-app"}, {"command": b.MARKER + " old"}]}]}}
        before = json.dumps(config)
        first = b.merged_hooks(config, b.MARKER + " new")
        self.assertEqual(json.dumps(config), before)
        self.assertEqual(first["hooks"]["SessionStart"], config["hooks"]["SessionStart"])
        self.assertEqual(first["hooks"]["Stop"][0]["hooks"], [{"command": "another-app"}])
        self.assertEqual(first, b.merged_hooks(first, b.MARKER + " new"))

    def test_explicit_setup_backup_and_invalid_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = home / "hooks.json"
            original = b'{"hooks": {}, "custom": 42}\n'
            path.write_bytes(original)
            result = b.install_hooks(home)
            self.assertEqual(Path(result["backup"]).read_bytes(), original)
            self.assertEqual(json.loads(path.read_bytes())["custom"], 42)
            path.write_text("invalid")
            with self.assertRaises(ValueError): b.install_hooks(home)
            self.assertEqual(path.read_text(), "invalid")

    def test_partial_jsonl_and_old_turn_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "rollout.jsonl"
            started = {"type": "event_msg", "payload": {"type": "task_started", "turn_id": TURN, "started_at": 100}}
            completed = {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": TURN, "completed_at": 120}}
            p.write_text(json.dumps(started) + "\n" + json.dumps(completed)[:25])
            m = b.Monitor(Path(tmp))
            self.assertEqual(m.legacy(p)["status"], "inProgress")
            with p.open("a") as f: f.write(json.dumps(completed)[25:] + "\n")
            self.assertEqual(m.legacy(p)["status"], "completed")
            with p.open("a") as f:
                started["payload"]["turn_id"] = OTHER
                f.write(json.dumps(started) + "\n" + json.dumps(completed) + "\n")
            self.assertEqual(m.legacy(p)["status"], "inProgress")
            p.write_text(json.dumps(completed) + "\n")
            self.assertEqual(m.legacy(p)["status"], "completed")

    def test_snapshot_readonly_excludes_subagents_and_selects_logs(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CODEX_MENU_DATA": tmp + "/support"}):
            home = Path(tmp)
            (home / "sessions").mkdir()
            p = home / "sessions" / "one.jsonl"
            p.write_text(json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": TURN, "started_at": 100}}) + "\n" +
                         json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "live message"}]}}) + "\n")
            dbpath = home / "state_5.sqlite"
            with sqlite3.connect(dbpath) as db:
                db.execute("create table threads (id text,title text,cwd text,rollout_path text,updated_at integer,source text,archived integer)")
                db.executemany("insert into threads values (?,?,?,?,?,?,?)", [(SID, "First", "/project", str(p), 100, "vscode", 0),
                   (OTHER, "Guardian", "/project", str(p), 101, '{"subagent":{}}', 0)])
            original = dbpath.read_bytes()
            b.atomic_json(home / "support/selection.json", {"id": SID})
            snap = b.Monitor(home).snapshot(live={SID}, check_live=False)
            self.assertEqual(len(snap["sessions"]), 1)
            self.assertEqual(snap["sessions"][0]["state"], "running")
            self.assertEqual(snap["sessions"][0]["logs"][-1]["text"], "live message")
            self.assertEqual(dbpath.read_bytes(), original)
            with b.readonly_db(dbpath) as db:
                with self.assertRaises(sqlite3.OperationalError): db.execute("delete from threads")

    def test_hook_process_has_no_output_or_approval_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, CODEX_MENU_DATA=tmp)
            proc = subprocess.run([sys_executable(), str(Path(b.__file__)), "--hook"],
                                  input=json.dumps(event("PermissionRequest")), text=True, capture_output=True, env=env)
            self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
            self.assertTrue((Path(tmp) / "events" / (SID + ".json")).exists())
            proc = subprocess.run([sys_executable(), str(Path(b.__file__)), "--hook"],
                                  input='{"session_id":"../../escape"}', text=True, capture_output=True, env=env)
            self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))

    def test_multiple_running_threads_have_independent_logs_and_lifecycles(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CODEX_MENU_DATA": tmp + "/support"}):
            home = Path(tmp)
            (home / "sessions").mkdir()
            paths = []
            for sid, text in ((SID, "first thread"), (OTHER, "second thread")):
                path = home / "sessions" / (sid + ".jsonl")
                path.write_text(json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": TURN, "started_at": 100}}) + "\n" +
                                json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}}) + "\n")
                paths.append(path)
            with sqlite3.connect(home / "state_5.sqlite") as db:
                db.execute("create table threads (id text,title text,cwd text,rollout_path text,updated_at integer,source text,archived integer)")
                db.executemany("insert into threads values (?,?,?,?,?,?,?)", [(SID, "First", "/first", str(paths[0]), 100, "vscode", 0),
                               (OTHER, "Second", "/second", str(paths[1]), 101, "vscode", 0)])
            monitor = b.Monitor(home)
            b.atomic_json(home / "support/selection.json", {"id": SID})
            one = {s["id"]: s for s in monitor.snapshot(live={SID, OTHER}, check_live=False)["sessions"]}
            self.assertEqual([one[s]["state"] for s in (SID, OTHER)], ["running", "running"])
            self.assertEqual(one[SID]["logs"][-1]["text"], "first thread")
            self.assertEqual(one[OTHER]["logs"], [])
            b.atomic_json(home / "support/selection.json", {"id": OTHER})
            with paths[1].open("a") as f:
                f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": TURN, "completed_at": 120}}) + "\n")
            two = {s["id"]: s for s in monitor.snapshot(live={SID, OTHER}, check_live=False)["sessions"]}
            self.assertEqual([two[s]["state"] for s in (SID, OTHER)], ["running", "stopped"])
            self.assertEqual(two[OTHER]["logs"][-1]["text"], "second thread")
            self.assertEqual(two[SID]["logs"], [])

    def test_live_command_without_output(self):
        self.assertEqual(b.excerpt({"type": "commandExecution", "command": "pwd", "aggregatedOutput": None}), ("コマンド", "pwd\n"))

    def test_structured_tool_output_shows_text_instead_of_transport_json(self):
        blocks = [{"type": "input_text", "text": "Script completed"},
                  {"type": "input_text", "text": json.dumps({"chunk_id": "123", "output": "Tests passed\n"})}]
        self.assertEqual(b.tool_text(blocks), "Script completed\nTests passed\n")
        self.assertEqual(b.tool_text('{"custom":42}'), '{"custom":42}')
        self.assertEqual(b.tool_text("plain output"), "plain output")
        self.assertEqual(b.tool_text({"type": "image", "data": "binary-data"}), "[画像]")

    def test_projection_completion_is_not_downgraded_by_a_start_in_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "rollout.jsonl"
            p.write_text(json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": TURN, "started_at": 100}}) + "\n")
            m = b.Monitor(Path(tmp))
            result = m.legacy(p, {"turn": TURN, "status": "completed", "started": 100, "completed": 101})
            self.assertEqual(result["status"], "completed")

    def test_lsof_errors_do_not_mean_no_live_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "thread-writer-locks").mkdir()
            with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "Permission denied")):
                self.assertIsNone(b.live_sessions(home))
            with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "")):
                self.assertEqual(b.live_sessions(home), set())


def sys_executable():
    import sys
    return sys.executable


if __name__ == "__main__":
    unittest.main()
