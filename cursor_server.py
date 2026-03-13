#!/usr/bin/env python3
"""
cursor_server.py — HTTP chat UI backed by Cursor ACP sessions.

This server creates its own Cursor sessions via `agent acp` and stores the
mapping between UI conversations and Cursor `sessionId`s. It does not depend on
any pre-existing tmux windows.

Usage:
    python cursor_server.py [--port 7860] [--host 0.0.0.0] [--cwd PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

from flask import Flask, jsonify, request


APP_ROOT = Path(__file__).parent.absolute()
DEFAULT_PORT = int(os.environ.get("CURSOR_SERVER_PORT", "7860"))
DEFAULT_AGENT = os.environ.get("CURSOR_AGENT_PATH", str(Path.home() / ".local/bin/agent"))
STORE_PATH = APP_ROOT / "cursor_sessions.json"
DEFAULT_CWD = str(APP_ROOT)
WORKDIR_ROOT = Path(os.environ.get("CURSOR_WORKDIR_ROOT", "/kmh-nfs-ssd-us-mount/code/siri")).resolve()
ZHH_SERVER_URL = os.environ.get("ZHH_SERVER_URL", "http://localhost:8080")

app = Flask(__name__)

store_lock = threading.Lock()
conversation_locks: dict[str, threading.Lock] = {}
SERVER_CWD = DEFAULT_CWD
AGENT_PATH = DEFAULT_AGENT


def utc_now() -> float:
    return time.time()


def normalize_workdir(workdir: str) -> Path:
  if not workdir:
    raise ValueError("workdir is required")

  candidate = Path(workdir).expanduser()
  if not candidate.is_absolute():
    candidate = (WORKDIR_ROOT / candidate).resolve()
  else:
    candidate = candidate.resolve()

  try:
    candidate.relative_to(WORKDIR_ROOT)
  except ValueError as exc:
    raise ValueError(f"workdir must be inside {WORKDIR_ROOT}") from exc

  if not candidate.exists():
    raise ValueError(f"workdir does not exist: {candidate}")
  if not candidate.is_dir():
    raise ValueError(f"workdir is not a directory: {candidate}")

  return candidate


def relative_workdir(path: Path) -> str:
  try:
    rel = path.relative_to(WORKDIR_ROOT)
  except ValueError:
    return str(path)
  return "." if str(rel) == "." else str(rel)


def workdir_base(cwd: str) -> str:
  try:
    p = Path(cwd)
    if p.name:
      return p.name
  except Exception:
    pass
  return cwd


def list_workdir_children(workdir: str | None) -> dict:
  current = normalize_workdir(workdir or str(WORKDIR_ROOT))
  children = []
  for child in sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
    if not child.is_dir() or child.name.startswith('.'):
      continue
    children.append({
      "name": child.name,
      "path": str(child),
      "relative_path": relative_workdir(child),
    })

  is_root = current == WORKDIR_ROOT
  parent_path = None if is_root else str(current.parent)
  parent_relative_path = None if is_root else relative_workdir(current.parent)

  return {
    "root": str(WORKDIR_ROOT),
    "current": str(current),
    "current_relative": relative_workdir(current),
    "parent": parent_path,
    "parent_relative": parent_relative_path,
    "children": children,
  }


def get_conversation_lock(conversation_id: str) -> threading.Lock:
    with store_lock:
        lock = conversation_locks.get(conversation_id)
        if lock is None:
            lock = threading.Lock()
            conversation_locks[conversation_id] = lock
        return lock


def default_store() -> dict:
    return {"conversations": {}}


def load_store() -> dict:
    if not STORE_PATH.exists():
        return default_store()
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default_store()
        data.setdefault("conversations", {})
        return data
    except Exception:
        return default_store()


def save_store(data: dict) -> None:
    tmp_path = STORE_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, STORE_PATH)


def list_conversations() -> list[dict]:
    with store_lock:
        data = load_store()
        items = list(data["conversations"].values())
    items.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
    return [conversation_summary(item) for item in items]


def get_conversation(conversation_id: str) -> dict | None:
    with store_lock:
        data = load_store()
        return data["conversations"].get(conversation_id)


def find_conversation_by_cwd(cwd: str) -> dict | None:
    with store_lock:
        data = load_store()
        for conv in data["conversations"].values():
            if conv.get("cwd") == cwd:
                return conv
    return None


def update_conversation(conversation_id: str, updater) -> dict:
    with store_lock:
        data = load_store()
        conv = data["conversations"].get(conversation_id)
        if conv is None:
            raise KeyError(conversation_id)
        updater(conv)
        conv["updated_at"] = utc_now()
        save_store(data)
        return conv


def delete_conversation(conversation_id: str) -> dict | None:
    with store_lock:
        data = load_store()
        conv = data["conversations"].pop(conversation_id, None)
        if conv is None:
            return None
        conversation_locks.pop(conversation_id, None)
        save_store(data)
        return conv


def create_conversation_record(title: str, cwd: str, mode: str, cursor_session_id: str | None) -> dict:
    conversation_id = str(uuid.uuid4())
    now = utc_now()
    record = {
        "id": conversation_id,
        "title": title,
        "cwd": cwd,
        "mode": mode,
        "cursor_session_id": cursor_session_id,
        "status": "idle",
        "created_at": now,
        "updated_at": now,
        "messages": [],
        "job_ids": [],
    }
    with store_lock:
        data = load_store()
        data["conversations"][conversation_id] = record
        save_store(data)
    return record


def conversation_summary(conv: dict) -> dict:
    messages = conv.get("messages", [])
    job_ids = conv.get("job_ids", [])
    last_message = messages[-1]["content"] if messages else ""
    return {
        "id": conv["id"],
        "title": conv.get("title") or "Untitled",
        "workdir_base": workdir_base(conv.get("cwd", "")),
        "cwd": conv.get("cwd"),
        "mode": conv.get("mode", "agent"),
        "status": conv.get("status", "idle"),
        "cursor_session_id": conv.get("cursor_session_id"),
        "created_at": conv.get("created_at"),
        "updated_at": conv.get("updated_at"),
        "message_count": len(messages),
        "task_count": len(job_ids),
        "last_message_preview": last_message[:120],
    }


def zhh_request(method: str, path: str, payload: dict | None = None, timeout: float = 20.0) -> tuple[int, dict]:
    url = f"{ZHH_SERVER_URL}{path}"
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib_request.Request(url, method=method, data=data, headers=headers)

    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
            raw = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw) if raw else {}
            if not isinstance(parsed, dict):
                parsed = {"data": parsed}
            return code, parsed
    except urllib_error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else {}
        except Exception:
            parsed = {"error": raw or str(e)}
        if not isinstance(parsed, dict):
            parsed = {"error": str(parsed)}
        return e.code, parsed
    except Exception as e:
        return 503, {"error": f"failed to reach zhh server at {ZHH_SERVER_URL}: {e}"}


def get_conversation_jobs(conversation: dict) -> list[dict]:
    job_ids = conversation.get("job_ids", []) or []
    if not job_ids:
        return []

    status_code, status_data = zhh_request("GET", "/status")
    if status_code != 200:
        raise RuntimeError(status_data.get("error", f"status code {status_code}"))

    jobs = status_data.get("jobs", [])
    by_id = {job.get("job_id"): job for job in jobs if isinstance(job, dict) and job.get("job_id")}

    ordered = []
    for job_id in reversed(job_ids):
        if job_id in by_id:
            ordered.append(by_id[job_id])
        else:
            ordered.append({"job_id": job_id, "status": "unknown", "missing": True})
    return ordered


def fetch_task_log_payload(job_id: str, lines: int = 400) -> tuple[int, dict]:
    return zhh_request("GET", f"/log/{job_id}?lines={lines}")


def build_prompt_with_task_refs(base_text: str, refs_payload: list[dict]) -> str:
    if not refs_payload:
        return base_text

    blocks = [
      text for text in (str(item.get("stdout", "")) for item in refs_payload)
      if text.strip()
    ]

    refs_text = "\n\n".join(blocks)
    if not refs_text.strip():
        return base_text

    return (
        f"{base_text}\n\n"
        "---\n"
      "The user referenced the following stdout output(s). Use only these outputs as additional context for this turn.\n"
        f"{refs_text}"
    )


class ACPClient:
    def __init__(self, agent_path: str, cwd: str):
        self.agent_path = agent_path
        self.cwd = cwd
        self.proc = None
        self.pending: dict[int, queue.Queue] = {}
        self.pending_lock = threading.Lock()
        self.updates: queue.Queue = queue.Queue()
        self._next_id = 1

    def start(self):
        self.proc = subprocess.Popen(
            [self.agent_path, "acp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=self.cwd,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def close(self):
        if not self.proc:
            return
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass

    def _read_stdout(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            if "id" in msg and ("result" in msg or "error" in msg):
                with self.pending_lock:
                    waiter = self.pending.pop(msg["id"], None)
                if waiter is not None:
                    waiter.put(msg)
                continue

            if msg.get("method") == "session/request_permission":
                self.respond(msg["id"], {
                    "outcome": {"outcome": "selected", "optionId": "allow-once"}
                })
                continue

            if msg.get("method") == "session/update":
                self.updates.put(msg)

    def _read_stderr(self):
        for _ in self.proc.stderr:
            pass

    def request(self, method: str, params: dict, timeout: float = 120.0) -> dict:
        request_id = self._next_id
        self._next_id += 1

        waiter: queue.Queue = queue.Queue(maxsize=1)
        with self.pending_lock:
            self.pending[request_id] = waiter

        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

        msg = waiter.get(timeout=timeout)
        if "error" in msg:
            raise RuntimeError(msg["error"])
        return msg["result"]

    def respond(self, request_id: int, result: dict) -> None:
        payload = {"jsonrpc": "2.0", "id": request_id, "result": result}
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()


def extract_chunk_text(update_message: dict) -> str:
    params = update_message.get("params") or {}
    update = params.get("update") or {}
    if update.get("sessionUpdate") != "agent_message_chunk":
        return ""
    content = update.get("content") or {}
    if isinstance(content, dict):
        return content.get("text") or ""
    return ""


def acp_initialize(client: ACPClient) -> None:
    client.request("initialize", {
        "protocolVersion": 1,
        "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False},
            "terminal": False,
        },
        "clientInfo": {"name": "cursor-server", "version": "0.1"},
    })
    client.request("authenticate", {"methodId": "cursor_login"})


def acp_prompt_session(cwd: str, mode: str, text: str, cursor_session_id: str | None = None, timeout: float = 300.0) -> dict:
    client = ACPClient(AGENT_PATH, cwd)
    try:
        client.start()
        acp_initialize(client)
        if cursor_session_id:
            client.request("session/load", {
                "sessionId": cursor_session_id,
                "cwd": cwd,
                "mcpServers": [],
                "mode": mode,
            })
            active_session_id = cursor_session_id
        else:
            new_session = client.request("session/new", {
                "cwd": cwd,
                "mcpServers": [],
                "mode": mode,
            })
            active_session_id = new_session["sessionId"]

        chunks: list[str] = []
        stop_flag = {"done": False}

        def pump_updates():
            while not stop_flag["done"]:
                try:
                    update = client.updates.get(timeout=0.2)
                except queue.Empty:
                    continue
                chunk = extract_chunk_text(update)
                if chunk:
                    chunks.append(chunk)

        pump_thread = threading.Thread(target=pump_updates, daemon=True)
        pump_thread.start()

        result = client.request("session/prompt", {
            "sessionId": active_session_id,
            "prompt": [{"type": "text", "text": text}],
        }, timeout=timeout)

        stop_flag["done"] = True
        pump_thread.join(timeout=1)

        return {
            "cursor_session_id": active_session_id,
            "text": "".join(chunks).strip(),
            "stop_reason": result.get("stopReason", "unknown"),
        }
    finally:
        client.close()


def maybe_autoname(conversation_id: str) -> None:
    conv = get_conversation(conversation_id)
    if not conv:
        return
    if conv.get("title") and conv["title"] != "New chat":
        return
    messages = conv.get("messages", [])
    user_messages = [m for m in messages if m.get("role") == "user"]
    if not user_messages:
        return
    title = user_messages[0].get("content", "New chat").strip().splitlines()[0][:48]
    if title:
        update_conversation(conversation_id, lambda c: c.update({"title": title}))


def append_message(conversation_id: str, role: str, content: str, extra: dict | None = None) -> dict:
  message = {
    "id": str(uuid.uuid4()),
    "role": role,
    "content": content,
    "created_at": utc_now(),
  }
  if extra:
    message.update(extra)
  return update_conversation(conversation_id, lambda c: c.setdefault("messages", []).append(message))


@app.route("/api/conversations", methods=["GET"])
def api_list_conversations():
    return jsonify({"conversations": list_conversations()})


@app.route("/api/conversations", methods=["POST"])
def api_create_conversation():
    data = request.get_json(force=True, silent=True) or {}
    workdir = data.get("workdir")
    mode = data.get("mode") or "agent"

    if mode not in {"agent", "ask", "plan"}:
        return jsonify({"error": "invalid mode"}), 400

    try:
        cwd = str(normalize_workdir(workdir))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    existing = find_conversation_by_cwd(cwd)
    if existing is not None:
      return jsonify({"conversation": conversation_summary(existing), "detail": existing, "reused": True})

    title = workdir_base(cwd)
    record = create_conversation_record(title, cwd, mode, None)
    return jsonify({"conversation": conversation_summary(record), "detail": record, "reused": False})


@app.route("/api/workdirs", methods=["GET"])
def api_workdirs():
    try:
        data = list_workdir_children(request.args.get("path"))
        return jsonify(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/conversations/<conversation_id>", methods=["GET"])
def api_get_conversation(conversation_id: str):
    conv = get_conversation(conversation_id)
    if not conv:
        return jsonify({"error": "not found"}), 404
    return jsonify({"conversation": conv, "summary": conversation_summary(conv)})


@app.route("/api/conversations/<conversation_id>", methods=["DELETE"])
def api_delete_conversation(conversation_id: str):
  conv = delete_conversation(conversation_id)
  if not conv:
    return jsonify({"error": "not found"}), 404
  return jsonify({"deleted": True, "conversation": conversation_summary(conv)})


@app.route("/api/conversations/<conversation_id>/tasks", methods=["GET"])
def api_list_tasks(conversation_id: str):
  conv = get_conversation(conversation_id)
  if not conv:
    return jsonify({"error": "not found"}), 404
  try:
    jobs = get_conversation_jobs(conv)
    return jsonify({"conversation_id": conversation_id, "count": len(jobs), "jobs": jobs})
  except Exception as e:
    return jsonify({"error": str(e)}), 502


@app.route("/api/conversations/<conversation_id>/tasks/run", methods=["POST"])
def api_run_task(conversation_id: str):
  conv = get_conversation(conversation_id)
  if not conv:
    return jsonify({"error": "not found"}), 404

  data = request.get_json(force=True, silent=True) or {}
  zhh_args = (data.get("args") or "").strip()

  status_code, run_data = zhh_request("POST", "/run", {"cwd": conv["cwd"], "args": zhh_args})
  if status_code != 200:
    return jsonify({"error": run_data.get("error", f"/run failed with {status_code}"), "detail": run_data}), status_code

  job_id = run_data.get("job_id")
  if job_id:
    def add_job(c: dict):
      job_ids = c.setdefault("job_ids", [])
      if job_id not in job_ids:
        job_ids.append(job_id)

    update_conversation(conversation_id, add_job)

  return jsonify({"conversation_id": conversation_id, "job": run_data}), 200


@app.route("/api/conversations/<conversation_id>/tasks/<job_id>/cancel", methods=["POST"])
def api_cancel_task(conversation_id: str, job_id: str):
  conv = get_conversation(conversation_id)
  if not conv:
    return jsonify({"error": "not found"}), 404

  job_ids = conv.get("job_ids", []) or []
  if job_id not in job_ids:
    return jsonify({"error": "job does not belong to this conversation"}), 404

  status_code, payload = zhh_request("POST", f"/cancel/{job_id}")
  return jsonify(payload), status_code


@app.route("/api/conversations/<conversation_id>/tasks/<job_id>/log", methods=["GET"])
def api_task_log(conversation_id: str, job_id: str):
  conv = get_conversation(conversation_id)
  if not conv:
    return jsonify({"error": "not found"}), 404

  job_ids = conv.get("job_ids", []) or []
  if job_id not in job_ids:
    return jsonify({"error": "job does not belong to this conversation"}), 404

  lines = request.args.get("lines", "500")
  upstream_path = f"/log/{job_id}?lines={lines}"
  status_code, payload = zhh_request("GET", upstream_path)

  if status_code != 200:
    detail = payload.get("error") if isinstance(payload, dict) else str(payload)
    return jsonify({
      "error": f"upstream log request failed: GET {ZHH_SERVER_URL}{upstream_path}",
      "upstream_status": status_code,
      "upstream_detail": detail,
    }), status_code

  if not isinstance(payload, dict) or ("log" not in payload):
    return jsonify({
      "error": f"upstream returned unexpected payload: GET {ZHH_SERVER_URL}{upstream_path}",
      "upstream_status": status_code,
      "upstream_payload": payload,
    }), 502

  if not str(payload.get("log", "")).strip():
    return jsonify({
      "error": f"upstream returned empty log: GET {ZHH_SERVER_URL}{upstream_path}",
      "upstream_status": status_code,
      "upstream_payload": payload,
    }), 502

  return jsonify(payload), status_code


@app.route("/api/conversations/<conversation_id>/messages", methods=["POST"])
def api_send_message(conversation_id: str):
  data = request.get_json(force=True, silent=True) or {}
  text = (data.get("text") or "").strip()
  if not text:
    return jsonify({"error": "empty text"}), 400

  task_refs = data.get("task_refs") or []
  if not isinstance(task_refs, list):
    return jsonify({"error": "task_refs must be a list"}), 400

  normalized_refs = []
  for ref in task_refs:
    if isinstance(ref, str) and ref.strip():
      normalized_refs.append(ref.strip())
  normalized_refs = list(dict.fromkeys(normalized_refs))

  conv = get_conversation(conversation_id)
  if not conv:
    return jsonify({"error": "not found"}), 404

  conv_job_ids = set(conv.get("job_ids", []) or [])
  invalid_refs = [ref for ref in normalized_refs if ref not in conv_job_ids]
  if invalid_refs:
    return jsonify({"error": "some task_refs do not belong to this conversation", "invalid_task_refs": invalid_refs}), 400

  lock = get_conversation_lock(conversation_id)
  if not lock.acquire(blocking=False):
    return jsonify({"error": "conversation busy"}), 409

  try:
    update_conversation(conversation_id, lambda c: c.update({"status": "running"}))

    refs_payload: list[dict] = []
    for job_id in normalized_refs:
      status_code, payload = fetch_task_log_payload(job_id, lines=400)
      if status_code == 200 and isinstance(payload, dict) and "log" in payload:
        refs_payload.append({
          "stdout": str(payload.get("log", "")),
        })
      else:
        refs_payload.append({
          "stdout": "",
        })

    prompt_text = build_prompt_with_task_refs(text, refs_payload)
    append_message(conversation_id, "user", text, {
      "task_refs": normalized_refs,
    })

    result = acp_prompt_session(
      cwd=conv["cwd"],
      mode=conv.get("mode", "agent"),
      text=prompt_text,
      cursor_session_id=conv.get("cursor_session_id"),
    )

    if not conv.get("cursor_session_id"):
      update_conversation(conversation_id, lambda c: c.update({"cursor_session_id": result["cursor_session_id"]}))

    assistant_text = result["text"] or f"[No text returned; stopReason={result['stop_reason']}]"
    append_message(conversation_id, "assistant", assistant_text)
    maybe_autoname(conversation_id)
    updated = update_conversation(conversation_id, lambda c: c.update({"status": "idle"}))
    return jsonify({
      "conversation": updated,
      "assistant": assistant_text,
      "stop_reason": result["stop_reason"],
    })
  except Exception as e:
    update_conversation(conversation_id, lambda c: c.update({"status": "error", "last_error": str(e)}))
    return jsonify({"error": str(e)}), 500
  finally:
    lock.release()


HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Cursor ACP Chat</title>
<style>
* { box-sizing: border-box; }
body {
  margin: 0;
  background: #111318;
  color: #d9dde7;
  font-family: Inter, system-ui, -apple-system, sans-serif;
  height: 100vh;
  display: grid;
  grid-template-columns: 280px 1fr;
}
#sidebar {
  border-right: 1px solid #262b36;
  background: #171a21;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
#sidebar .head {
  padding: 16px;
  border-bottom: 1px solid #262b36;
}
#sidebar .head h1 {
  margin: 0 0 10px;
  font-size: 16px;
}
#new-btn {
  width: 100%;
  border: 0;
  border-radius: 10px;
  padding: 10px 12px;
  cursor: pointer;
  background: #4da3ff;
  color: #09111f;
  font-weight: 700;
}
#conv-list {
  list-style: none;
  padding: 8px;
  margin: 0;
  overflow-y: auto;
  flex: 1;
}
#conv-list li {
  padding: 10px 12px;
  border: 1px solid transparent;
  border-radius: 10px;
  margin-bottom: 8px;
  cursor: pointer;
  background: #1b1f28;
}
#conv-list li.active {
  border-color: #4da3ff;
  background: #1b2433;
}
.conv-title {
  font-size: 13px;
  font-weight: 700;
  margin-bottom: 4px;
}
.conv-meta {
  font-size: 11px;
  color: #8992a3;
}
.conv-path {
  margin-top: 4px;
  font-size: 11px;
  color: #6e7890;
  word-break: break-all;
}
#main {
  display: flex;
  flex-direction: column;
  min-height: 0;
}
#view-tabs {
  display: flex;
  gap: 8px;
  padding: 10px 18px;
  border-bottom: 1px solid #262b36;
  background: #141822;
}
.tab-btn {
  border: 1px solid #2b3240;
  background: #1b1f28;
  color: #c8d0df;
  border-radius: 999px;
  padding: 7px 14px;
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
}
.tab-btn.active {
  border-color: #4da3ff;
  color: #9cc9ff;
  background: #1b2433;
}
#topbar {
  padding: 14px 18px;
  border-bottom: 1px solid #262b36;
  background: #171a21;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
#title {
  font-size: 15px;
  font-weight: 700;
}
#meta {
  margin-top: 4px;
  font-size: 12px;
  color: #8992a3;
}
#messages {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
}
.empty {
  color: #8992a3;
  text-align: center;
  margin-top: 100px;
}
.msg {
  max-width: 900px;
  margin: 0 auto 14px;
}
.msg .role {
  font-size: 11px;
  color: #8992a3;
  margin-bottom: 6px;
  text-transform: uppercase;
  letter-spacing: .08em;
}
.bubble {
  white-space: pre-wrap;
  line-height: 1.5;
  padding: 12px 14px;
  border-radius: 12px;
}
.msg.user .bubble {
  background: #22324a;
}
.msg.assistant .bubble {
  background: #1b1f28;
}
#composer-wrap {
  border-top: 1px solid #262b36;
  padding: 14px 18px 18px;
  background: #171a21;
}
#composer-wrap.busy {
  background: #241f12;
  box-shadow: inset 0 1px 0 rgba(255, 196, 64, .18);
}
#composer {
  display: flex;
  gap: 10px;
}
#composer-ref-row {
  margin-top: 8px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.ref-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: #d9dde7;
  border: 1px solid #2b3240;
  border-radius: 999px;
  background: #1b1f28;
  padding: 4px 8px;
}
.ref-chip button {
  border: 0;
  background: transparent;
  color: #9aa7bf;
  cursor: pointer;
  font-size: 12px;
  line-height: 1;
}
#input {
  flex: 1;
  min-height: 78px;
  max-height: 220px;
  resize: vertical;
  border-radius: 12px;
  border: 1px solid #2b3240;
  background: #0f1218;
  color: #d9dde7;
  padding: 12px 14px;
  font: inherit;
}
#send-btn {
  width: 110px;
  border: 0;
  border-radius: 12px;
  background: #4da3ff;
  color: #09111f;
  font-weight: 700;
  cursor: pointer;
}
#send-btn:disabled, #new-btn:disabled {
  opacity: .55;
  cursor: default;
}
#status {
  margin-top: 8px;
  font-size: 12px;
  color: #8992a3;
  min-height: 18px;
  transition: color .15s ease, background .15s ease, border-color .15s ease;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 0;
}
#status[data-kind="busy"] {
  color: #ffd166;
  background: rgba(255, 209, 102, .12);
  border: 1px solid rgba(255, 209, 102, .28);
  border-radius: 999px;
  padding: 6px 10px;
  font-weight: 700;
}
#status[data-kind="busy"]::before {
  content: "●";
  animation: busyPulse 1s infinite;
}
#delete-btn {
  border: 1px solid rgba(255, 107, 107, .25);
  background: rgba(255, 107, 107, .10);
  color: #ff9c9c;
  border-radius: 10px;
  padding: 10px 12px;
  cursor: pointer;
  font-weight: 700;
  flex-shrink: 0;
}
#delete-btn:disabled {
  opacity: .5;
  cursor: default;
}
#status[data-kind="error"] {
  color: #ff8f8f;
  background: rgba(255, 107, 107, .10);
  border: 1px solid rgba(255, 107, 107, .24);
  border-radius: 999px;
  padding: 6px 10px;
}
#status[data-kind="success"] {
  color: #8ce99a;
  background: rgba(105, 219, 124, .10);
  border: 1px solid rgba(105, 219, 124, .22);
  border-radius: 999px;
  padding: 6px 10px;
}
@keyframes busyPulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: .45; transform: scale(.85); }
}
#modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, .55);
  display: none;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
#modal-backdrop.show {
  display: flex;
}
#modal {
  width: min(760px, 100%);
  max-height: min(680px, calc(100vh - 48px));
  display: flex;
  flex-direction: column;
  background: #171a21;
  border: 1px solid #2b3240;
  border-radius: 16px;
  overflow: hidden;
}
#modal-head {
  padding: 16px 18px;
  border-bottom: 1px solid #262b36;
}
#modal-head h2 {
  margin: 0;
  font-size: 16px;
}
#modal-sub {
  margin-top: 6px;
  font-size: 12px;
  color: #8992a3;
  word-break: break-all;
}
#modal-body {
  padding: 16px 18px;
  overflow: auto;
}
#picker-toolbar {
  display: flex;
  gap: 10px;
  margin-bottom: 12px;
}
#picker-current {
  flex: 1;
  padding: 10px 12px;
  border-radius: 10px;
  border: 1px solid #2b3240;
  background: #0f1218;
  color: #d9dde7;
  font-size: 12px;
  word-break: break-all;
}
.secondary-btn {
  border: 1px solid #2b3240;
  background: #1b1f28;
  color: #d9dde7;
  border-radius: 10px;
  padding: 10px 12px;
  cursor: pointer;
}
#dir-list {
  display: grid;
  gap: 8px;
}
.dir-item {
  padding: 11px 12px;
  border-radius: 10px;
  border: 1px solid #2b3240;
  background: #111318;
  cursor: pointer;
}
.dir-item:hover {
  border-color: #4da3ff;
}
.dir-name {
  font-size: 13px;
  font-weight: 700;
}
.dir-path {
  margin-top: 4px;
  font-size: 11px;
  color: #8992a3;
}
#modal-foot {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 16px 18px;
  border-top: 1px solid #262b36;
}
#selected-workdir {
  font-size: 12px;
  color: #8992a3;
  word-break: break-all;
}
#panel-chat, #panel-tasks {
  display: flex;
  flex-direction: column;
  min-height: 0;
  flex: 1;
}
#panel-tasks {
  display: none;
}
#tasks-toolbar {
  padding: 14px 18px;
  border-bottom: 1px solid #262b36;
  background: #171a21;
  display: flex;
  align-items: center;
  gap: 10px;
}
#task-args {
  width: 220px;
  border-radius: 10px;
  border: 1px solid #2b3240;
  background: #0f1218;
  color: #d9dde7;
  padding: 10px 12px;
}
#run-task-btn {
  border: 0;
  border-radius: 10px;
  background: #4da3ff;
  color: #09111f;
  font-weight: 700;
  padding: 10px 14px;
  cursor: pointer;
}
#refresh-task-btn {
  border: 1px solid #2b3240;
  border-radius: 10px;
  background: #1b1f28;
  color: #d9dde7;
  padding: 10px 12px;
  cursor: pointer;
}
#tasks-list {
  padding: 14px 18px 22px;
  overflow: auto;
  flex: 1;
}
.task-item {
  border: 1px solid #2b3240;
  border-radius: 12px;
  padding: 12px;
  background: #161a23;
  margin-bottom: 10px;
}
.task-top {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: center;
}
.task-id {
  font-size: 12px;
  color: #9fb2ce;
  word-break: break-all;
}
.task-status {
  font-size: 11px;
  font-weight: 700;
  border-radius: 999px;
  padding: 4px 10px;
  border: 1px solid #2b3240;
  background: #111318;
}
.task-meta {
  margin-top: 8px;
  font-size: 12px;
  color: #8b95aa;
}
.task-actions {
  margin-top: 10px;
  display: flex;
  gap: 8px;
}
.task-actions button {
  border: 1px solid #2b3240;
  background: #1b1f28;
  color: #d9dde7;
  border-radius: 8px;
  padding: 6px 10px;
  cursor: pointer;
}
</style>
</head>
<body>
  <aside id="sidebar">
    <div class="head">
      <h1>Cursor ACP</h1>
      <button id="new-btn" onclick="createConversation()">+ New conversation</button>
    </div>
    <ul id="conv-list"></ul>
  </aside>

  <main id="main">
    <div id="topbar">
      <div>
        <div id="title">No conversation selected</div>
        <div id="meta">Create a new session first.</div>
      </div>
      <button id="delete-btn" onclick="deleteCurrentConversation()" disabled>Delete session</button>
    </div>
    <div id="view-tabs">
      <button id="tab-chat" class="tab-btn active" onclick="switchTab('chat')">Chat</button>
      <button id="tab-tasks" class="tab-btn" onclick="switchTab('tasks')">Tasks</button>
    </div>

    <section id="panel-chat">
      <div id="messages"><div class="empty">No conversation selected.</div></div>
      <div id="composer-wrap">
        <div id="composer">
          <textarea id="input" placeholder="Type a message..." onkeydown="handleKey(event)"></textarea>
          <button id="send-btn" onclick="sendMessage()" disabled>Send</button>
        </div>
        <div id="composer-ref-row"></div>
      </div>
    </section>

    <section id="panel-tasks">
      <div id="tasks-toolbar">
        <input id="task-args" placeholder="Optional args, e.g. s / q / rr" />
        <button id="run-task-btn" onclick="runTaskForCurrentConversation()" disabled>Run</button>
        <button id="refresh-task-btn" onclick="loadTasksForCurrentConversation()" disabled>Refresh</button>
      </div>
      <div id="tasks-list"><div class="empty">No conversation selected.</div></div>
    </section>

    <div style="padding: 0 18px 12px; background:#171a21; border-top:1px solid #262b36;">
      <div id="status">Ready.</div>
    </div>
  </main>

  <div id="modal-backdrop" onclick="handleModalBackdrop(event)">
    <div id="modal">
      <div id="modal-head">
        <h2>Choose workdir</h2>
        <div id="modal-sub">Root: __WORKDIR_ROOT__</div>
      </div>
      <div id="modal-body">
        <div id="picker-toolbar">
          <button class="secondary-btn" onclick="goParentDir()">Up</button>
          <div id="picker-current">Loading...</div>
        </div>
        <div id="dir-list"></div>
      </div>
      <div id="modal-foot">
        <div id="selected-workdir">Selected: —</div>
        <div style="display:flex; gap:10px;">
          <button class="secondary-btn" onclick="closeCreateModal()">Cancel</button>
          <button id="confirm-create-btn" onclick="createConversationConfirmed()">Create</button>
        </div>
      </div>
    </div>
  </div>

<script>
let currentId = null;
let currentConversation = null;
let currentView = 'chat';
let pendingTaskRefs = [];
const WORKDIR_ROOT = '__WORKDIR_ROOT__';
let pickerCurrentPath = WORKDIR_ROOT;
let pickerSelectedPath = WORKDIR_ROOT;

function setStatus(text, kind = 'normal') {
  const el = document.getElementById('status');
  el.textContent = text;
  el.dataset.kind = kind;
  document.getElementById('composer-wrap').classList.toggle('busy', kind === 'busy');
}

async function api(url, options) {
  const res = await fetch(url, options);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}

async function loadConversations(selectId = null) {
  const data = await api('/api/conversations');
  renderConversationList(data.conversations || []);
  if (selectId) {
    await openConversation(selectId, false);
  } else if (currentId) {
    const found = (data.conversations || []).find(x => x.id === currentId);
    if (found) await openConversation(currentId, false);
  }
}

function switchTab(view) {
  currentView = view;
  const chatPanel = document.getElementById('panel-chat');
  const taskPanel = document.getElementById('panel-tasks');
  const chatTab = document.getElementById('tab-chat');
  const taskTab = document.getElementById('tab-tasks');
  if (view === 'tasks') {
    chatPanel.style.display = 'none';
    taskPanel.style.display = 'flex';
    chatTab.classList.remove('active');
    taskTab.classList.add('active');
    loadTasksForCurrentConversation();
  } else {
    chatPanel.style.display = 'flex';
    taskPanel.style.display = 'none';
    taskTab.classList.remove('active');
    chatTab.classList.add('active');
  }
}

function renderConversationList(items) {
  const list = document.getElementById('conv-list');
  list.innerHTML = '';
  for (const item of items) {
    const li = document.createElement('li');
    if (item.id === currentId) li.classList.add('active');
    li.onclick = () => openConversation(item.id);
    li.innerHTML = `
      <div class="conv-title">${escapeHtml(item.workdir_base || basenameFromPath(item.cwd) || item.title || 'Untitled')}</div>
      <div class="conv-meta">${escapeHtml(item.mode)} · ${escapeHtml(item.status)} · ${item.message_count} msgs · ${item.task_count || 0} tasks</div>
      <div class="conv-path">${escapeHtml(item.cwd || '')}</div>
    `;
    list.appendChild(li);
  }
}

async function createConversation() {
  pickerSelectedPath = WORKDIR_ROOT;
  document.getElementById('selected-workdir').textContent = 'Selected: ' + pickerSelectedPath;
  document.getElementById('modal-backdrop').classList.add('show');
  await browseDir(WORKDIR_ROOT);
}

async function browseDir(path) {
  setStatus('Loading directories...', 'busy');
  const data = await api('/api/workdirs?path=' + encodeURIComponent(path));
  pickerCurrentPath = data.current;
  if (!pickerSelectedPath) pickerSelectedPath = data.current;
  document.getElementById('picker-current').textContent = data.current;
  document.getElementById('selected-workdir').textContent = 'Selected: ' + pickerSelectedPath;

  const list = document.getElementById('dir-list');
  list.innerHTML = '';

  const currentItem = document.createElement('div');
  currentItem.className = 'dir-item';
  currentItem.onclick = () => selectWorkdir(data.current);
  currentItem.innerHTML = `
    <div class="dir-name">📁 Select this directory</div>
    <div class="dir-path">${escapeHtml(data.current_relative)} </div>
  `;
  list.appendChild(currentItem);

  for (const dir of data.children) {
    const item = document.createElement('div');
    item.className = 'dir-item';
    item.innerHTML = `
      <div class="dir-name">📂 ${escapeHtml(dir.name)}</div>
      <div class="dir-path">${escapeHtml(dir.relative_path)}</div>
    `;
    item.onclick = () => browseDir(dir.path);
    list.appendChild(item);
  }

  document.getElementById('confirm-create-btn').disabled = !pickerSelectedPath;
  setStatus('Ready.');
}

function selectWorkdir(path) {
  pickerSelectedPath = path;
  document.getElementById('selected-workdir').textContent = 'Selected: ' + pickerSelectedPath;
  document.getElementById('confirm-create-btn').disabled = false;
}

async function goParentDir() {
  try {
    const data = await api('/api/workdirs?path=' + encodeURIComponent(pickerCurrentPath));
    if (data.parent) {
      await browseDir(data.parent);
    }
  } catch (err) {
    setStatus('Browse failed: ' + err.message, 'error');
  }
}

function closeCreateModal() {
  document.getElementById('modal-backdrop').classList.remove('show');
}

function handleModalBackdrop(event) {
  if (event.target.id === 'modal-backdrop') {
    closeCreateModal();
  }
}

async function createConversationConfirmed() {
  if (!pickerSelectedPath) {
    setStatus('Please choose a workdir.', 'error');
    return;
  }
  try {
    document.getElementById('new-btn').disabled = true;
    document.getElementById('confirm-create-btn').disabled = true;
    setStatus('Creating Cursor session...', 'busy');
    const data = await api('/api/conversations', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ title: 'New chat', workdir: pickerSelectedPath })
    });
    currentId = data.conversation.id;
    closeCreateModal();
    await loadConversations(currentId);
    document.getElementById('send-btn').disabled = false;
    if (data.reused) {
      setStatus('Existing session opened for this workdir.', 'success');
    } else {
      setStatus('Conversation created.', 'success');
    }
  } catch (err) {
    setStatus('Create failed: ' + err.message, 'error');
  } finally {
    document.getElementById('new-btn').disabled = false;
    document.getElementById('confirm-create-btn').disabled = false;
  }
}

async function openConversation(id, updateList = true) {
  currentId = id;
  const data = await api('/api/conversations/' + id);
  currentConversation = data.conversation;
  renderConversation(currentConversation);
  document.getElementById('send-btn').disabled = currentConversation.status === 'running';
  document.getElementById('delete-btn').disabled = currentConversation.status === 'running';
  document.getElementById('run-task-btn').disabled = false;
  document.getElementById('refresh-task-btn').disabled = false;
  if (currentView === 'tasks') {
    await loadTasksForCurrentConversation();
  }
  if (updateList) await loadConversations();
}

function renderConversation(conv) {
  document.getElementById('title').textContent = basenameFromPath(conv.cwd) || conv.title || 'Untitled';
  document.getElementById('meta').textContent = `${conv.mode} · ${conv.status} · ${conv.cwd}`;
  const messages = document.getElementById('messages');
  messages.innerHTML = '';
  if (!conv.messages || !conv.messages.length) {
    messages.innerHTML = '<div class="empty">No messages yet.</div>';
  } else {
    for (const m of conv.messages) {
      const wrap = document.createElement('div');
      wrap.className = 'msg ' + m.role;
      const refs = (m.task_refs || []).map(jobId => `<span class="ref-chip">🔗 ${escapeHtml(shortJobId(jobId))}</span>`).join(' ');
      wrap.innerHTML = `
        <div class="role">${escapeHtml(m.role)}</div>
        <div class="bubble">${escapeHtml(m.content)}</div>
        ${refs ? `<div style="margin-top:8px; display:flex; gap:6px; flex-wrap:wrap;">${refs}</div>` : ''}
      `;
      messages.appendChild(wrap);
    }
  }
  messages.scrollTop = messages.scrollHeight;
}

function renderPendingTaskRefs() {
  const row = document.getElementById('composer-ref-row');
  if (!pendingTaskRefs.length) {
    row.innerHTML = '';
    return;
  }
  row.innerHTML = pendingTaskRefs.map(jobId => `
    <span class="ref-chip">🔗 ${escapeHtml(shortJobId(jobId))}
      <button onclick="removeTaskReference('${escapeHtml(jobId)}')">×</button>
    </span>
  `).join('');
}

function addTaskReference(jobId) {
  if (!jobId) return;
  if (!pendingTaskRefs.includes(jobId)) {
    pendingTaskRefs.push(jobId);
    renderPendingTaskRefs();
    setStatus(`Referenced task ${shortJobId(jobId)}.`, 'success');
  }
}

function removeTaskReference(jobId) {
  pendingTaskRefs = pendingTaskRefs.filter(x => x !== jobId);
  renderPendingTaskRefs();
}

function basenameFromPath(path) {
  if (!path) return '';
  const cleaned = String(path).replace(/\/+$/, '');
  const parts = cleaned.split('/');
  return parts[parts.length - 1] || cleaned;
}

function resetConversationView() {
  currentId = null;
  currentConversation = null;
  document.getElementById('title').textContent = 'No conversation selected';
  document.getElementById('meta').textContent = 'Create a new session first.';
  document.getElementById('messages').innerHTML = '<div class="empty">No conversation selected.</div>';
  document.getElementById('tasks-list').innerHTML = '<div class="empty">No conversation selected.</div>';
  document.getElementById('send-btn').disabled = true;
  document.getElementById('delete-btn').disabled = true;
  document.getElementById('run-task-btn').disabled = true;
  document.getElementById('refresh-task-btn').disabled = true;
  pendingTaskRefs = [];
  renderPendingTaskRefs();
}

async function deleteCurrentConversation() {
  if (!currentId) return;
  if (!confirm('Delete this managed session from the server list?')) return;

  const deletingId = currentId;
  try {
    document.getElementById('delete-btn').disabled = true;
    setStatus('Deleting session...', 'busy');
    await api(`/api/conversations/${deletingId}`, { method: 'DELETE' });
    resetConversationView();
    await loadConversations();
    setStatus('Session deleted.', 'success');
  } catch (err) {
    if (currentConversation) {
      document.getElementById('delete-btn').disabled = currentConversation.status === 'running';
    }
    setStatus('Delete failed: ' + err.message, 'error');
  }
}

async function sendMessage() {
  const input = document.getElementById('input');
  const text = input.value.trim();
  if (!currentId || !text) return;
  const refs = [...pendingTaskRefs];
  try {
    document.getElementById('send-btn').disabled = true;
    setStatus('Waiting for Cursor...', 'busy');
    const data = await api(`/api/conversations/${currentId}/messages`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ text, task_refs: refs })
    });
    input.value = '';
    pendingTaskRefs = [];
    renderPendingTaskRefs();
    currentConversation = data.conversation;
    renderConversation(currentConversation);
    await loadConversations();
    setStatus('Done.', 'success');
  } catch (err) {
    setStatus('Send failed: ' + err.message, 'error');
  } finally {
    document.getElementById('send-btn').disabled = false;
    if (currentConversation) {
      document.getElementById('delete-btn').disabled = currentConversation.status === 'running';
    }
  }
}

function renderTasks(items) {
  const root = document.getElementById('tasks-list');
  if (!items || !items.length) {
    root.innerHTML = '<div class="empty">No tasks in this conversation yet.</div>';
    return;
  }

  root.innerHTML = '';
  for (const job of items) {
    const div = document.createElement('div');
    div.className = 'task-item';
    const status = job.status || 'unknown';
    const args = job.zhh_args || '';
    const createdAt = job.created_at || '-';
    const runBtnDisabled = !currentId ? 'disabled' : '';
    const canCancel = status === 'running' || status === 'starting';
    div.innerHTML = `
      <div class="task-top">
        <div class="task-id">${escapeHtml(job.job_id || 'unknown')}</div>
        <div class="task-status">${escapeHtml(status)}</div>
      </div>
      <div class="task-meta">args: ${escapeHtml(args)} · created: ${escapeHtml(createdAt)}</div>
      <div class="task-actions">
        <button ${runBtnDisabled} onclick="addTaskReference('${escapeHtml(job.job_id || '')}')">Reference</button>
        <button ${runBtnDisabled} onclick="viewTaskLog('${escapeHtml(job.job_id || '')}')">Log</button>
        ${canCancel ? `<button ${runBtnDisabled} onclick="cancelTask('${escapeHtml(job.job_id || '')}')">Cancel</button>` : ''}
      </div>
    `;
    root.appendChild(div);
  }
}

async function loadTasksForCurrentConversation() {
  if (!currentId) {
    document.getElementById('tasks-list').innerHTML = '<div class="empty">No conversation selected.</div>';
    return;
  }
  try {
    setStatus('Loading tasks...', 'busy');
    const data = await api(`/api/conversations/${currentId}/tasks`);
    renderTasks(data.jobs || []);
    setStatus('Tasks refreshed.', 'success');
  } catch (err) {
    setStatus('Load tasks failed: ' + err.message, 'error');
  }
}

async function runTaskForCurrentConversation() {
  if (!currentId || !currentConversation) return;
  try {
    const args = document.getElementById('task-args').value.trim();
    document.getElementById('run-task-btn').disabled = true;
    setStatus('Creating task in /run...', 'busy');
    await api(`/api/conversations/${currentId}/tasks/run`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ args })
    });
    await loadConversations(currentId);
    await loadTasksForCurrentConversation();
    setStatus('Task created.', 'success');
  } catch (err) {
    setStatus('Run task failed: ' + err.message, 'error');
  } finally {
    document.getElementById('run-task-btn').disabled = !currentId;
  }
}

async function cancelTask(jobId) {
  if (!currentId || !jobId) return;
  if (!confirm(`Cancel task ${jobId}?`)) return;
  try {
    setStatus('Cancelling task...', 'busy');
    await api(`/api/conversations/${currentId}/tasks/${jobId}/cancel`, { method: 'POST' });
    await loadTasksForCurrentConversation();
    setStatus('Task cancelled.', 'success');
  } catch (err) {
    setStatus('Cancel failed: ' + err.message, 'error');
  }
}

async function viewTaskLog(jobId) {
  if (!currentId || !jobId) return;
  try {
    setStatus('Fetching task log...', 'busy');
    const data = await api(`/api/conversations/${currentId}/tasks/${jobId}/log?lines=400`);
    const logText = data.log || '[no log output]';
    alert(logText.slice(-12000));
    setStatus('Log fetched.', 'success');
  } catch (err) {
    setStatus('Fetch log failed: ' + err.message, 'error');
  }
}

function handleKey(event) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    sendMessage();
  }
}

function escapeHtml(s) {
  return String(s)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;');
}

function shortJobId(jobId) {
  return String(jobId || '').slice(0, 8);
}

loadConversations();
</script>
</body>
</html>
"""


@app.route("/")
def index():
  html = HTML.replace("__WORKDIR_ROOT__", str(WORKDIR_ROOT))
  return html, 200, {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
      "Pragma": "no-cache",
      "Expires": "0",
  }


def main():
    global SERVER_CWD, AGENT_PATH

    parser = argparse.ArgumentParser(description="Cursor ACP conversation server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--cwd", default=DEFAULT_CWD, help="Default working directory for new sessions")
    parser.add_argument("--agent-path", default=DEFAULT_AGENT)
    args = parser.parse_args()

    SERVER_CWD = str(Path(args.cwd).expanduser())
    AGENT_PATH = str(Path(args.agent_path).expanduser())

    print("Cursor ACP server")
    print(f"agent path : {AGENT_PATH}")
    print(f"default cwd: {SERVER_CWD}")
    print(f"store file : {STORE_PATH}")
    print(f"url        : http://{args.host}:{args.port}")

    if not Path(AGENT_PATH).exists():
        raise SystemExit(f"agent not found: {AGENT_PATH}")
    if not Path(SERVER_CWD).exists():
        raise SystemExit(f"cwd does not exist: {SERVER_CWD}")

    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
