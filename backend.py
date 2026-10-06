"""Local, read-only Codex monitor. Only explicit --install-hooks changes config."""
import argparse
import collections
from contextlib import closing
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

MARKER = "CODEX_MENU_HOOK_V1=1"
EVENTS = ("UserPromptSubmit", "PermissionRequest", "PostToolUse", "Stop", "Interrupt", "SessionEnd")
ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def codex_home():
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().resolve()


def support_home():
    return Path(os.environ.get("CODEX_MENU_DATA", str(Path.home() / "Library/Application Support/CodexMenu")))


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def hook_update(previous, payload, now):
    """No prompt, tool arguments or tool output is retained in hook state."""
    event = payload.get("hook_event_name")
    if event not in EVENTS or not ID.fullmatch(str(payload.get("session_id", ""))):
        raise ValueError("Invalid hook event/session")
    turn = payload.get("turn_id") or ""
    if turn and not ID.fullmatch(turn):
        raise ValueError("Invalid turn id")
    # Keep separate turns: delayed hooks from an older turn cannot clear a new approval.
    turns = dict(previous.get("turns", {}))
    current = turns.get(turn, {})
    pending = dict(current.get("pending", {}))
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict) and payload.get("tool_name") in ("Bash", "apply_patch"):
        tool_input = tool_input.get("command")
    tool_key = hashlib.sha256(json.dumps(
        [turn, payload.get("tool_name"), tool_input],
        sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()).hexdigest()
    if event == "PermissionRequest":
        pending[tool_key] = list(pending.get(tool_key, [])) + [now]
    elif event == "PostToolUse":
        values = list(pending.get(tool_key, []))[1:]
        if values:
            pending[tool_key] = values
        else:
            pending.pop(tool_key, None)
    else:
        pending = {}
    turns[turn] = {"event": event, "at": now, "pending": pending}
    turns = dict(sorted(turns.items(), key=lambda pair: pair[1]["at"])[-32:])
    return {"turns": turns, "at": now, "sessionEnded": now if event == "SessionEnd" else previous.get("sessionEnded", 0)}


def receive_hook():
    # Always return success without output: monitoring must not decide approvals.
    try:
        raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            return
        payload = json.loads(raw)
        sid = str(payload.get("session_id", ""))
        if not ID.fullmatch(sid):
            return
        root = support_home() / "events"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / (sid + ".json")
        with (root / (sid + ".lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                previous = json.loads(path.read_text()) if path.exists() else {}
            except (ValueError, OSError):
                previous = {}
            atomic_json(path, hook_update(previous, payload, time.time()))
    except Exception:
        pass


def merged_hooks(config, command):
    result = json.loads(json.dumps(config))
    hooks = result.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks must be an object")
    for event in EVENTS:
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise ValueError("Invalid hook groups")
        clean = []
        for group in groups:
            handlers = group.get("hooks", [])
            kept = [h for h in handlers if not str(h.get("command", "")).startswith(MARKER + " ")]
            if kept:
                clean.append(dict(group, hooks=kept))
        clean.append({"matcher": ".*", "hooks": [{"type": "command", "command": command, "timeout": 1}]})
        hooks[event] = clean
    return result


def install_hooks(home):
    target = home / "hooks.json"
    if target.is_symlink():
        raise ValueError("hooks.json is a symlink; refusing to replace it")
    old = target.read_bytes() if target.exists() else None
    config = json.loads(old) if old is not None else {}
    command = MARKER + " /usr/bin/python3 " + shlex.quote(str(Path(__file__).resolve())) + " --hook"
    merged = merged_hooks(config, command)
    home.mkdir(parents=True, exist_ok=True)
    backup = None
    if old is not None:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        backup = home / ("hooks.json.codex-menu-backup-" + stamp)
        with backup.open("xb") as f:
            os.chmod(backup, 0o600)
            f.write(old)
    current = target.read_bytes() if target.exists() else None
    if current != old:
        raise ValueError("hooks.json changed during setup; no replacement made")
    atomic_json(target, merged)
    return {"backup": str(backup) if backup else None, "events": list(EVENTS)}


def readonly_db(path):
    c = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    return c


def latest_db(home, prefix):
    paths = list(home.glob(prefix + "_*.sqlite"))
    return max(paths, key=lambda p: int(re.search(r"_(\d+)\.sqlite$", p.name)[1])) if paths else None


def live_sessions(home):
    directory = home / "thread-writer-locks"
    if not directory.is_dir():
        return None
    try:
        p = subprocess.run(["/usr/sbin/lsof", "-n", "-P", "-F", "n", "+D", str(directory)],
                           capture_output=True, text=True, timeout=3)
        if p.returncode not in (0, 1) or p.stderr.strip():
            return None
        return {Path(line[1:]).stem for line in p.stdout.splitlines()
                if line.startswith("n") and line.endswith(".lock")}
    except (OSError, subprocess.TimeoutExpired):
        return None


def state_for(status, turn, started, completed, hook, live):
    if status in ("completed", "interrupted", "failed"):
        return "stopped", "処理終了" if status == "completed" else ("中断" if status == "interrupted" else "エラー終了")
    if status != "inProgress":
        return "unknown", "処理状態の記録なし"
    if live is False:
        return "stopped", "セッションの実プロセスなし"
    if live is None:
        return "unknown", "セッションの実プロセスを確認できません"
    if hook.get("sessionEnded", 0) >= (started or 0) and hook.get("sessionEnded", 0) > 0:
        return "stopped", "セッション終了通知を受信"
    current = hook.get("turns", {}).get(turn, {})
    fresh = current.get("at", 0) >= (started or 0)
    if fresh and current.get("pending"):
        return "waiting", "PermissionRequest通知を受信"
    if fresh and current.get("event") in ("Stop", "Interrupt", "SessionEnd"):
        return "stopped", "終了通知を受信"
    return "running", "処理開始済み・終了記録なし"


def excerpt(item):
    kind = item.get("type")
    if kind == "agentMessage":
        return "Codex", (item.get("text") or "")[:4000]
    if kind == "userMessage":
        text = "\n".join(x.get("text", "") for x in item.get("content", []) if x.get("type") in ("text", "input_text"))
        return "ユーザー", text[:2500]
    if kind == "commandExecution":
        return "コマンド", ((item.get("command") or "") + "\n" + (item.get("aggregatedOutput") or ""))[:3000]
    if kind == "mcpToolCall":
        return "ツール", str(item.get("server", "")) + "/" + str(item.get("tool", "")) + " · " + str(item.get("status", ""))
    if kind == "fileChange":
        return "ファイル変更", str(item.get("status", ""))
    return None


def tool_text(value, depth=0):
    """Unwrap Codex content blocks without displaying the transport envelope."""
    if depth >= 4:
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "\n".join(tool_text(item, depth + 1) for item in value)
    if isinstance(value, dict):
        if value.get("type") in ("image", "input_image", "output_image"):
            return "[画像]"
        if value.get("type") in ("input_text", "output_text", "text"):
            return tool_text(value.get("text", ""), depth + 1)
        if isinstance(value.get("output"), str):
            return value["output"]
        if isinstance(value.get("content"), list):
            return tool_text(value["content"], depth + 1)
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return value
        if isinstance(decoded, dict) and (isinstance(decoded.get("output"), str) or isinstance(decoded.get("content"), list)):
            return tool_text(decoded, depth + 1)
        return value
    return str(value)


class Monitor:
    def __init__(self, home):
        self.home = home.resolve()
        self.cache = {}

    def legacy(self, path, initial=None):
        """Incrementally tail JSONL. Never infer state from time since last write."""
        key = str(path)
        stat = path.stat()
        cached = self.cache.get(key)
        if cached is None or stat.st_size < cached["offset"] or stat.st_ino != cached["inode"]:
            cached = {"offset": 0, "inode": stat.st_ino, "turn": "", "status": None, "started": None,
                      "completed": None, "logs": collections.deque(maxlen=50)}
            if initial:
                cached.update(initial)
                # SQLite supplies the lifecycle; only recent text needs an initial tail.
                cached["offset"] = max(0, stat.st_size - 256 * 1024)
                if cached["offset"]:
                    with path.open("rb") as f:
                        f.seek(cached["offset"])
                        f.readline()
                        cached["offset"] = f.tell()
            self.cache[key] = cached
        with path.open("rb") as f:
            f.seek(cached["offset"])
            while True:
                pos = f.tell()
                line = f.readline()
                if not line or not line.endswith(b"\n"):
                    cached["offset"] = pos
                    break
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                payload = obj.get("payload", {})
                typ = payload.get("type")
                if obj.get("type") == "event_msg":
                    if typ == "task_started":
                        if ((payload.get("started_at") or 0) > (cached["started"] or 0) or not cached["status"]
                            or (payload.get("turn_id") != cached["turn"] and (payload.get("started_at") or 0) >= (cached["started"] or 0))):
                            cached.update(status="inProgress", turn=payload.get("turn_id", ""), started=payload.get("started_at"), completed=None)
                    elif typ in ("task_complete", "turn_aborted") and (not cached["turn"] or not payload.get("turn_id") or payload.get("turn_id") == cached["turn"]):
                        cached.update(status="completed" if typ == "task_complete" else "interrupted", completed=payload.get("completed_at"))
                if obj.get("type") == "response_item" and typ == "message" and payload.get("role") in ("user", "assistant"):
                    text = "\n".join(x.get("text", "") for x in payload.get("content", []) if x.get("type") in ("input_text", "output_text", "text"))
                    if text:
                        cached["logs"].append({"role": "Codex" if payload["role"] == "assistant" else "ユーザー", "text": text[:4000]})
                elif obj.get("type") == "response_item" and typ in ("function_call", "custom_tool_call"):
                    cached["logs"].append({"role": "ツール", "text": str(payload.get("name", ""))})
                elif obj.get("type") == "response_item" and typ in ("function_call_output", "custom_tool_call_output"):
                    raw = payload.get("output", "")
                    text = tool_text(raw)
                    cached["logs"].append({"role": "ツール結果", "text": text[:2500]})
        return cached

    def snapshot(self, live=None, check_live=True):
        try:
            selected = json.loads((support_home() / "selection.json").read_text()).get("id")
        except (OSError, ValueError):
            selected = None
        state_path = latest_db(self.home, "state")
        if state_path is None:
            raise ValueError("CodexのローカルDBが見つかりません: " + str(self.home))
        with closing(readonly_db(state_path)) as db:
            columns = {r[1] for r in db.execute("PRAGMA table_info(threads)")}
            title = "COALESCE(NULLIF(name,''),title)" if "name" in columns else "title"
            rows = db.execute("SELECT id," + title + " AS title,cwd,rollout_path,updated_at,source FROM threads "
                              "WHERE archived=0 AND source NOT LIKE '%subagent%' ORDER BY updated_at DESC LIMIT 1000").fetchall()
        if check_live:
            live = live_sessions(self.home)
        history_path = latest_db(self.home, "thread_history")
        history = readonly_db(history_path) if history_path else None
        sessions = []
        last_hook = 0
        try:
            for row in rows:
                sid = row["id"]
                if not ID.fullmatch(sid):
                    continue
                path = Path(row["rollout_path"])
                # Do not let a corrupted source DB redirect the reader to arbitrary files.
                if not any(root in path.resolve().parents for root in (self.home / "sessions", self.home / "archived_sessions")):
                    continue
                status, turn, started, completed = None, "", None, None
                logs = []
                if history:
                    latest = history.execute("SELECT turn_id,status,started_at,completed_at FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1", (sid,)).fetchone()
                    if latest:
                        turn, status, started, completed = latest
                        items = history.execute("SELECT item_json FROM thread_items WHERE thread_id=? AND item_type IN ('agentMessage','userMessage','commandExecution','mcpToolCall','fileChange') ORDER BY rollout_ordinal DESC LIMIT 40", (sid,)).fetchall() if sid == selected else []
                        for (raw,) in reversed(items):
                            value = excerpt(json.loads(raw))
                            if value and value[1]:
                                logs.append({"role": value[0], "text": value[1]})
                # The projection can lag a live JSONL stream. Merge its newer lifecycle.
                if path.exists():
                    legacy = self.legacy(path, {"turn": turn, "status": status, "started": started, "completed": completed} if status else None)
                    if legacy["status"] and (not status or (legacy["started"] or 0) > (started or 0)
                                             or (legacy["turn"] != turn and (legacy["started"] or 0) >= (started or 0))
                                             or (legacy["turn"] == turn and legacy["status"] != "inProgress")):
                        turn, status, started, completed = (legacy[k] for k in ("turn", "status", "started", "completed"))
                    if legacy["logs"]:
                        logs = list(legacy["logs"])
                try:
                    hook = json.loads((support_home() / "events" / (sid + ".json")).read_text())
                except (OSError, ValueError):
                    hook = {}
                last_hook = max(last_hook, hook.get("at", 0))
                state, detail = state_for(status, turn, started, completed, hook, None if live is None else sid in live)
                sessions.append({"id": sid, "title": row["title"] or Path(row["cwd"]).name or sid[:8],
                                 "cwd": row["cwd"], "path": str(path), "updated": row["updated_at"],
                                 "state": state, "detail": detail, "logs": logs})
        finally:
            if history:
                history.close()
        priority = {"waiting": 0, "running": 1, "unknown": 2, "stopped": 3}
        sessions.sort(key=lambda s: (priority[s["state"]], -s["updated"]))
        allowed = {s["path"] for s in sessions}
        self.cache = {path: value for path, value in self.cache.items() if path in allowed}
        selected = selected or (sessions[0]["id"] if sessions else None)
        for session in sessions:
            if session["id"] != selected:
                session["logs"] = []
        try:
            configured = MARKER in (self.home / "hooks.json").read_text()
        except OSError:
            configured = False
        return {"sessions": sessions, "error": None, "hookConfigured": configured, "lastHook": last_hook,
                "observedAt": time.time(), "liveCheck": live is not None}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--hook", action="store_true")
    p.add_argument("--install-hooks", action="store_true")
    p.add_argument("--once", action="store_true")
    p.add_argument("--home", type=Path, default=codex_home())
    args = p.parse_args()
    if args.hook:
        receive_hook()
        return
    if args.install_hooks:
        print(json.dumps(install_hooks(args.home), ensure_ascii=False))
        return
    monitor = Monitor(args.home.resolve())
    while True:
        try:
            value = monitor.snapshot()
        except Exception as error:
            value = {"sessions": [], "error": str(error), "hookConfigured": False, "lastHook": 0, "observedAt": time.time(), "liveCheck": False}
        print(json.dumps(value, ensure_ascii=False), flush=True)
        if args.once:
            break
        time.sleep(2)


if __name__ == "__main__":
    main()
