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
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, request

from acp_runtime import acp_prompt_session
from conversation_store import (
  conversation_summary as conversation_summary_impl,
  create_conversation_record as create_conversation_record_impl,
  delete_conversation as delete_conversation_impl,
  find_conversation_by_cwd as find_conversation_by_cwd_impl,
  get_conversation as get_conversation_impl,
  list_conversations as list_conversations_impl,
  update_conversation as update_conversation_impl,
)
from tasks_runtime import (
  build_prompt_with_task_refs,
  fetch_task_log_payload,
  fetch_task_reference_payload,
  get_conversation_jobs,
  zhh_request,
)
from yaml_editor_api import register_ka_editor_routes, register_yaml_editor_routes


APP_ROOT = Path(__file__).parent.absolute()
CONFIG_PATH = APP_ROOT / "config.json"


def load_ui_config() -> dict:
  defaults = {
    "host": "0.0.0.0",
    "port": 7860,
    "workdir_root": str(APP_ROOT.parent),
    "default_cwd": str(APP_ROOT),
    "agent_path": str(Path.home() / ".local/bin/agent"),
    "store_file": "cursor_sessions.json",
  }

  if not CONFIG_PATH.exists():
    return defaults

  try:
    payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
  except Exception:
    return defaults

  if not isinstance(payload, dict):
    return defaults

  ui = payload.get("ui_server")
  if not isinstance(ui, dict):
    return defaults

  merged = dict(defaults)
  merged.update({k: v for k, v in ui.items() if v is not None})
  return merged


UI_CONFIG = load_ui_config()


def config_path_value(value: str | Path, fallback: Path) -> Path:
  raw = Path(str(value or fallback)).expanduser()
  if not raw.is_absolute():
    raw = (APP_ROOT / raw).resolve()
  else:
    raw = raw.resolve()
  return raw


DEFAULT_HOST = str(UI_CONFIG.get("host") or "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("CURSOR_SERVER_PORT", str(UI_CONFIG.get("port") or 7860)))
DEFAULT_AGENT = os.environ.get("CURSOR_AGENT_PATH", str(UI_CONFIG.get("agent_path") or (Path.home() / ".local/bin/agent")))
store_file = Path(str(UI_CONFIG.get("store_file") or "cursor_sessions.json"))
STORE_PATH = store_file if store_file.is_absolute() else (APP_ROOT / store_file)
DEFAULT_CWD = str(config_path_value(UI_CONFIG.get("default_cwd") or APP_ROOT, APP_ROOT))
WORKDIR_ROOT = config_path_value(UI_CONFIG.get("workdir_root") or APP_ROOT.parent, APP_ROOT.parent)
ZHH_SERVER_URL = "http://localhost:8080"
UI_TEMPLATE_PATH = APP_ROOT / "cursor_server_ui.html"

try:
  AUTO_ITERATE_TASK_TIMEOUT_SECONDS = int(os.environ.get("AUTO_ITERATE_TASK_TIMEOUT_SECONDS", "0"))
except Exception:
  AUTO_ITERATE_TASK_TIMEOUT_SECONDS = 0

app = Flask(__name__)

store_lock = threading.Lock()
conversation_locks: dict[str, threading.Lock] = {}
auto_iterate_threads: dict[str, threading.Thread] = {}
auto_iterate_controls: dict[str, dict] = {}
SERVER_CWD = DEFAULT_CWD
AGENT_PATH = DEFAULT_AGENT


class AutoIterateStopRequested(Exception):
  pass


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


def normalize_task_status(status: str | None) -> str:
  return str(status or "unknown").strip().lower()


def is_running_like_task_status(status: str | None) -> bool:
  return normalize_task_status(status) in {"running", "starting", "queued", "pending"}


def is_terminal_task_status(status: str | None) -> bool:
  return not is_running_like_task_status(status)


def is_failed_task_status(status: str | None) -> bool:
  return normalize_task_status(status) in {"failed", "error", "timeout", "aborted"}


def task_alert_kind_for_status(status: str | None) -> str:
  return "failed" if is_failed_task_status(status) else "done"


def has_error_signature_in_log(log_text: str) -> bool:
  text = str(log_text or "")
  if not text.strip():
    return False
  lower = text.lower()
  return (
    "traceback (most recent call last)" in lower
    or "\ntraceback" in lower
    or " exited with code" in lower
    or "exited with code" in lower
  )


def diagnose_completed_jobs_once(conversation_id: str, conv: dict, jobs: list[dict]) -> tuple[dict, list[dict]]:
  task_meta = conv.get("task_meta", {}) or {}
  if not isinstance(task_meta, dict):
    task_meta = {}

  next_meta: dict[str, dict] = {}
  changed = False
  updated_jobs: list[dict] = []

  for job in jobs:
    if not isinstance(job, dict):
      continue

    job_id = str(job.get("job_id") or "").strip()
    if not job_id:
      continue

    entry = task_meta.get(job_id, {})
    if not isinstance(entry, dict):
      entry = {}
    else:
      entry = dict(entry)

    status = normalize_task_status(job.get("status"))
    checked = bool(entry.get("completion_log_checked", False))

    if status == "completed" and not checked:
      status_code, payload = fetch_task_log_payload(ZHH_SERVER_URL, job_id, lines=1200)
      log_text = ""
      if status_code == 200 and isinstance(payload, dict):
        log_text = str(payload.get("log") or "")

      has_error = has_error_signature_in_log(log_text)
      entry["completion_log_checked"] = True
      entry["completion_log_checked_at"] = utc_now()
      entry["completion_log_diagnosis"] = "error" if has_error else "ok"

      if has_error:
        entry["unread"] = True
        entry["alert_kind"] = "failed"

      changed = True

    diagnosis = str(entry.get("completion_log_diagnosis") or "").lower()
    enriched = dict(job)
    if status == "completed" and diagnosis == "error":
      enriched["status"] = "error"
      enriched["diagnosed_error"] = True

    updated_jobs.append(enriched)
    next_meta[job_id] = entry

  for key, value in task_meta.items():
    if key not in next_meta and isinstance(value, dict):
      next_meta[key] = value

  if changed or next_meta != task_meta:
    def save_meta(c: dict):
      c["task_meta"] = next_meta

    conv = update_conversation(conversation_id, save_meta)

  return conv, updated_jobs


def update_task_alert_state(conversation_id: str, conv: dict, jobs: list[dict]) -> tuple[dict, list[dict]]:
  task_meta = conv.get("task_meta", {}) or {}
  if not isinstance(task_meta, dict):
    task_meta = {}

  prev_meta = {job_id: meta for job_id, meta in task_meta.items() if isinstance(meta, dict)}
  next_meta: dict[str, dict] = {}
  updated_jobs: list[dict] = []

  for job in jobs:
    if not isinstance(job, dict):
      continue

    job_id = str(job.get("job_id") or "").strip()
    if not job_id:
      continue

    now_status = normalize_task_status(job.get("status"))
    prev_entry = prev_meta.get(job_id, {})

    nickname = str(job.get("nickname") or prev_entry.get("nickname") or "").strip()
    old_status = normalize_task_status(prev_entry.get("last_status")) if prev_entry.get("last_status") is not None else ""
    unread = bool(prev_entry.get("unread", False))
    alert_kind = str(prev_entry.get("alert_kind") or "").strip().lower()

    should_mark_unread = False
    if old_status:
      if is_running_like_task_status(old_status) and is_terminal_task_status(now_status):
        should_mark_unread = True
    elif is_terminal_task_status(now_status):
      should_mark_unread = True

    if should_mark_unread:
      unread = True
      alert_kind = task_alert_kind_for_status(now_status)
    elif unread:
      if is_failed_task_status(now_status):
        alert_kind = "failed"
      elif alert_kind != "failed":
        alert_kind = "done"
    else:
      alert_kind = ""

    next_entry: dict[str, object] = dict(prev_entry)
    next_entry["last_status"] = now_status
    if nickname:
      next_entry["nickname"] = nickname
    else:
      next_entry.pop("nickname", None)
    if unread:
      next_entry["unread"] = True
      next_entry["alert_kind"] = alert_kind or task_alert_kind_for_status(now_status)
    else:
      next_entry.pop("unread", None)
      next_entry.pop("alert_kind", None)

    if isinstance(prev_entry.get("updated_at"), (int, float)) and "updated_at" not in next_entry:
      next_entry["updated_at"] = prev_entry.get("updated_at")

    next_meta[job_id] = next_entry

    enriched = dict(job)
    enriched["nickname"] = nickname
    enriched["unread"] = bool(next_entry.get("unread", False))
    enriched["alert_kind"] = str(next_entry.get("alert_kind") or "")
    updated_jobs.append(enriched)

  if prev_meta != next_meta:
    def set_task_meta(c: dict):
      c["task_meta"] = next_meta

    conv = update_conversation(conversation_id, set_task_meta)

  return conv, updated_jobs


def clear_task_unread_alert(conversation_id: str, job_id: str) -> None:
  def clear_unread(c: dict):
    task_meta = c.get("task_meta")
    if not isinstance(task_meta, dict):
      return
    entry = task_meta.get(job_id)
    if not isinstance(entry, dict):
      return
    entry.pop("unread", None)
    entry.pop("alert_kind", None)
    entry["updated_at"] = utc_now()

  update_conversation(conversation_id, clear_unread)


def _safe_positive_int(text: str | None, default: int) -> int:
  try:
    value = int(str(text or default))
  except Exception:
    return default
  return value if value > 0 else default


def _tail_text_file(path: Path, lines: int = 500, max_chars: int = 120_000) -> str:
  try:
    raw = path.read_text(encoding="utf-8", errors="replace")
  except Exception:
    return ""
  rows = raw.splitlines()
  tail = "\n".join(rows[-lines:]) if lines > 0 else raw
  if len(tail) <= max_chars:
    return tail
  return tail[-max_chars:]


def _local_task_log_payload(conv: dict, job_id: str, lines: int = 500, prefer_pane: bool = False) -> dict | None:
  task_meta = conv.get("task_meta")
  if not isinstance(task_meta, dict):
    return None
  entry = task_meta.get(job_id)
  if not isinstance(entry, dict):
    return None

  key_order = ("pane_log_file", "final_log_file") if prefer_pane else ("final_log_file", "pane_log_file")
  for key in key_order:
    candidate = str(entry.get(key) or "").strip()
    if not candidate:
      continue
    path = Path(candidate)
    if not path.exists() or not path.is_file():
      continue
    text = _tail_text_file(path, lines=lines)
    if not text.strip():
      continue
    return {
      "job_id": job_id,
      "lines": lines,
      "log": text,
      "source": "local_file",
      "log_path": str(path),
    }

  return None


def _resolve_job_status(conv: dict, job_id: str) -> str:
  try:
    jobs = get_conversation_jobs(ZHH_SERVER_URL, conv)
    for job in jobs:
      if isinstance(job, dict) and str(job.get("job_id") or "") == job_id:
        return normalize_task_status(job.get("status"))
  except Exception:
    pass

  task_meta = conv.get("task_meta")
  if isinstance(task_meta, dict):
    entry = task_meta.get(job_id)
    if isinstance(entry, dict):
      return normalize_task_status(entry.get("last_status"))
  return "unknown"


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


def mark_task_status(conversation_id: str, job_id: str, status: str) -> None:
  normalized = normalize_task_status(status)

  def updater(c: dict):
    task_meta = c.setdefault("task_meta", {})
    entry = task_meta.get(job_id)
    if not isinstance(entry, dict):
      entry = {}
    else:
      entry = dict(entry)
    entry["last_status"] = normalized
    entry["updated_at"] = utc_now()
    task_meta[job_id] = entry

  update_conversation(conversation_id, updater)


def _get_or_create_auto_iterate_control(conversation_id: str) -> dict:
  with store_lock:
    control = auto_iterate_controls.get(conversation_id)
    if isinstance(control, dict):
      if not isinstance(control.get("stop_event"), threading.Event):
        control["stop_event"] = threading.Event()
      return control
    control = {
      "stop_event": threading.Event(),
      "phase": "idle",
      "current_job_id": None,
      "cursor_cancel": None,
    }
    auto_iterate_controls[conversation_id] = control
    return control


def set_auto_iterate_phase(
  conversation_id: str,
  *,
  phase: str,
  current_job_id: str | None = None,
  cursor_cancel=None,
) -> None:
  control = _get_or_create_auto_iterate_control(conversation_id)
  with store_lock:
    control["phase"] = str(phase or "idle")
    control["current_job_id"] = current_job_id
    control["cursor_cancel"] = cursor_cancel


def is_auto_iterate_worker_alive(conversation_id: str) -> bool:
  with store_lock:
    thread = auto_iterate_threads.get(conversation_id)
  return bool(thread and thread.is_alive())


def request_stop_auto_iterate(conversation_id: str) -> dict:
  control = _get_or_create_auto_iterate_control(conversation_id)
  stop_event = control.get("stop_event")
  if isinstance(stop_event, threading.Event):
    stop_event.set()

  phase = str(control.get("phase") or "unknown")
  action = "signal"
  job_id = str(control.get("current_job_id") or "").strip()

  if phase == "cursor":
    cancel_fn = control.get("cursor_cancel")
    if callable(cancel_fn):
      try:
        cancel_fn()
      except Exception:
        pass
    action = "cursor"
  elif phase == "task":
    if job_id:
      mark_task_status(conversation_id, job_id, "canceled")
      status_code, _ = zhh_request(ZHH_SERVER_URL, "POST", f"/cancel/{job_id}")
      if status_code not in {200, 404}:
        pass
    action = "task"

  return {
    "phase": phase,
    "action": action,
    "job_id": job_id or None,
  }


def cancel_latest_running_job_best_effort(conversation_id: str) -> str | None:
  conv = get_conversation(conversation_id)
  if not conv:
    return None
  try:
    jobs = get_conversation_jobs(ZHH_SERVER_URL, conv)
  except Exception:
    jobs = []
  for job in jobs:
    if not isinstance(job, dict):
      continue
    job_id = str(job.get("job_id") or "").strip()
    status = normalize_task_status(job.get("status"))
    if not job_id or not is_running_like_task_status(status):
      continue
    mark_task_status(conversation_id, job_id, "canceled")
    zhh_request(ZHH_SERVER_URL, "POST", f"/cancel/{job_id}")
    return job_id
  return None


def list_conversations() -> list[dict]:
  return list_conversations_impl(STORE_PATH, store_lock, conversation_summary)


def get_conversation(conversation_id: str) -> dict | None:
  return get_conversation_impl(STORE_PATH, store_lock, conversation_id)


def find_conversation_by_cwd(cwd: str) -> dict | None:
  return find_conversation_by_cwd_impl(STORE_PATH, store_lock, cwd)


def update_conversation(conversation_id: str, updater) -> dict:
  return update_conversation_impl(STORE_PATH, store_lock, conversation_id, updater, utc_now)


def delete_conversation(conversation_id: str) -> dict | None:
  return delete_conversation_impl(STORE_PATH, store_lock, conversation_locks, conversation_id)


def create_conversation_record(title: str, cwd: str, mode: str, cursor_session_id: str | None) -> dict:
  return create_conversation_record_impl(
    STORE_PATH,
    store_lock,
    title,
    cwd,
    mode,
    cursor_session_id,
    utc_now,
  )


def conversation_summary(conv: dict) -> dict:
  return conversation_summary_impl(conv, workdir_base)


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


def should_continue_auto_iteration(status: str | None) -> bool:
  value = normalize_task_status(status)
  return value in {"failed", "error", "timeout", "aborted", "canceled", "cancelled", "unknown"}


def update_auto_iterate_state(conversation_id: str, *, enabled: bool, status: str | None = None, error: str | None = None, round_no: int | None = None) -> None:
  def updater(c: dict):
    c["auto_iterating"] = enabled
    if status is not None:
      c["status"] = status
    if enabled:
      c["auto_iterate_started_at"] = c.get("auto_iterate_started_at") or utc_now()
      c["auto_iterate_updated_at"] = utc_now()
      if round_no is not None:
        c["auto_iterate_round"] = round_no
      if error:
        c["auto_iterate_last_error"] = error
      else:
        c.pop("auto_iterate_last_error", None)
      c.pop("auto_iterate_finished_at", None)
    else:
      c["auto_iterate_updated_at"] = utc_now()
      c["auto_iterate_finished_at"] = utc_now()
      if round_no is not None:
        c["auto_iterate_round"] = round_no
      if error:
        c["auto_iterate_last_error"] = error

  update_conversation(conversation_id, updater)


def auto_iterate_agent_turn(
  conversation_id: str,
  text: str,
  task_refs: list[str],
  *,
  stop_event: threading.Event | None = None,
) -> None:
  conv = get_conversation(conversation_id)
  if not conv:
    raise RuntimeError("conversation not found")

  normalized_refs = [str(ref).strip() for ref in (task_refs or []) if str(ref).strip()]
  normalized_refs = list(dict.fromkeys(normalized_refs))

  conv_job_ids = set(conv.get("job_ids", []) or [])
  invalid_refs = [ref for ref in normalized_refs if ref not in conv_job_ids]
  if invalid_refs:
    raise RuntimeError(f"invalid task refs for auto iterate: {invalid_refs}")

  refs_payload: list[dict] = []
  ref_sources: dict[str, str] = {}
  for job_id in normalized_refs:
    status_code, payload = fetch_task_reference_payload(ZHH_SERVER_URL, job_id, lines=400)
    if status_code == 200 and isinstance(payload, dict) and "stdout" in payload:
      source = str(payload.get("stdout_source") or "").strip()
      full_log_path = str(payload.get("full_log_path") or "").strip()
      if source and source != "zhh_log":
        ref_sources[job_id] = source
      else:
        ref_sources[job_id] = "tmux out"
      stdout_text = str(payload.get("stdout", ""))
      if full_log_path:
        stdout_text = (
          f"{stdout_text}\n\n"
          f"[Full log path]\n{full_log_path}"
        )
      refs_payload.append({"stdout": stdout_text})
    else:
      ref_sources[job_id] = "tmux out"
      refs_payload.append({"stdout": ""})

  prompt_text = build_prompt_with_task_refs(text, refs_payload)
  append_message(conversation_id, "user", text, {
    "task_refs": normalized_refs,
    "task_ref_sources": ref_sources,
    "auto_iterate": True,
  })

  lock = get_conversation_lock(conversation_id)
  with lock:
    if stop_event is not None and stop_event.is_set():
      raise AutoIterateStopRequested("auto iterate stopped by user")
    latest = get_conversation(conversation_id)
    if not latest:
      raise RuntimeError("conversation disappeared")

    def on_client_ready(client):
      set_auto_iterate_phase(
        conversation_id,
        phase="cursor",
        current_job_id=None,
        cursor_cancel=client.close,
      )

    try:
      result = acp_prompt_session(
        agent_path=AGENT_PATH,
        cwd=latest["cwd"],
        mode=latest.get("mode", "agent"),
        text=prompt_text,
        cursor_session_id=latest.get("cursor_session_id"),
        cancel_event=stop_event,
        on_client_ready=on_client_ready,
      )
    except InterruptedError as exc:
      raise AutoIterateStopRequested("auto iterate stopped by user") from exc

    set_auto_iterate_phase(conversation_id, phase="iterating", current_job_id=None, cursor_cancel=None)

    if not latest.get("cursor_session_id"):
      update_conversation(conversation_id, lambda c: c.update({"cursor_session_id": result["cursor_session_id"]}))

  assistant_text = result["text"] or f"[No text returned; stopReason={result['stop_reason']}]"
  append_message(conversation_id, "assistant", assistant_text, {"auto_iterate": True})
  maybe_autoname(conversation_id)


def run_auto_iterate_worker(conversation_id: str, first_text: str, max_rounds: int) -> None:
  final_status = "idle"
  final_error = None
  rounds_done = 0
  control = _get_or_create_auto_iterate_control(conversation_id)
  stop_event = control.get("stop_event")

  try:
    append_message(conversation_id, "system", "Auto iterate started.", {
      "system_event": "auto_iterate_start",
    })

    auto_iterate_agent_turn(conversation_id, first_text, [], stop_event=stop_event)

    for round_no in range(1, max_rounds + 1):
      if stop_event is not None and stop_event.is_set():
        raise AutoIterateStopRequested("auto iterate stopped by user")
      rounds_done = round_no
      update_auto_iterate_state(conversation_id, enabled=True, status="iterating", round_no=round_no)

      conv = get_conversation(conversation_id)
      if not conv:
        raise RuntimeError("conversation not found")

      status_code, run_data = zhh_request(ZHH_SERVER_URL, "POST", "/run", {"cwd": conv["cwd"], "args": ""})
      if status_code != 200:
        raise RuntimeError(run_data.get("error", f"/run failed with {status_code}"))

      job_id = str(run_data.get("job_id") or "").strip()
      if not job_id:
        raise RuntimeError("run returned empty job_id")

      set_auto_iterate_phase(conversation_id, phase="task", current_job_id=job_id, cursor_cancel=None)

      def add_job(c: dict):
        job_ids = c.setdefault("job_ids", [])
        if job_id not in job_ids:
          job_ids.append(job_id)
        task_meta = c.setdefault("task_meta", {})
        entry = task_meta.get(job_id)
        if not isinstance(entry, dict):
          entry = {}
        else:
          entry = dict(entry)
        entry["last_status"] = normalize_task_status(run_data.get("status") or "starting")
        entry["updated_at"] = utc_now()
        for key in ("zhh_args", "created_at", "final_log_file", "pane_log_file", "command", "cwd"):
          value = run_data.get(key)
          if value is not None and value != "":
            entry[key] = value
        task_meta[job_id] = entry

      update_conversation(conversation_id, add_job)

      append_message(conversation_id, "system", f"Runned job {job_id}", {
        "system_event": "task_run",
        "job_id": job_id,
        "job_status": str(run_data.get("status") or "starting"),
        "zhh_args": str(run_data.get("zhh_args") or ""),
        "auto_iterate": True,
      })

      terminal_job = None
      deadline = (utc_now() + AUTO_ITERATE_TASK_TIMEOUT_SECONDS) if AUTO_ITERATE_TASK_TIMEOUT_SECONDS > 0 else None
      while True:
        if stop_event is not None and stop_event.is_set():
          mark_task_status(conversation_id, job_id, "canceled")
          status_code, _ = zhh_request(ZHH_SERVER_URL, "POST", f"/cancel/{job_id}")
          if status_code not in {200, 404}:
            pass
          raise AutoIterateStopRequested("auto iterate stopped by user")
        if deadline is not None and utc_now() >= deadline:
          break
        conv = get_conversation(conversation_id)
        if not conv:
          raise RuntimeError("conversation not found")
        jobs = get_conversation_jobs(ZHH_SERVER_URL, conv)
        conv, jobs = diagnose_completed_jobs_once(conversation_id, conv, jobs)
        conv, jobs = update_task_alert_state(conversation_id, conv, jobs)
        match = None
        for job in jobs:
          if isinstance(job, dict) and str(job.get("job_id") or "") == job_id:
            match = job
            break
        if match is None:
          terminal_job = {"job_id": job_id, "status": "canceled", "missing": True}
          break
        if is_terminal_task_status(match.get("status")):
          terminal_job = match
          break
        time.sleep(5)

      if terminal_job is None:
        if AUTO_ITERATE_TASK_TIMEOUT_SECONDS > 0:
          raise RuntimeError(
            f"timeout waiting task {job_id} "
            f"(>{AUTO_ITERATE_TASK_TIMEOUT_SECONDS}s without terminal status)"
          )
        raise RuntimeError(f"task {job_id} did not reach terminal status")

      terminal_status = normalize_task_status(terminal_job.get("status"))
      if not should_continue_auto_iteration(terminal_status):
        append_message(conversation_id, "system", f"Auto iterate finished: status={terminal_status}.", {
          "system_event": "auto_iterate_done",
          "job_id": job_id,
          "auto_iterate": True,
        })
        final_status = "idle"
        return

      feedback_text = (
        f"Experiment {job_id} finished with status '{terminal_status}'. "
        "Please analyze the referenced experiment log, fix the issue, and prepare for the next run."
      )
      auto_iterate_agent_turn(conversation_id, feedback_text, [job_id], stop_event=stop_event)

    append_message(conversation_id, "system", f"Auto iterate stopped: reached max rounds ({max_rounds}).", {
      "system_event": "auto_iterate_max_rounds",
      "auto_iterate": True,
    })
    final_status = "idle"
  except AutoIterateStopRequested:
    final_status = "idle"
    append_message(conversation_id, "system", "Auto iterate stopped by user.", {
      "system_event": "auto_iterate_stopped",
      "auto_iterate": True,
    })
  except Exception as e:
    final_error = str(e)
    final_status = "error"
    append_message(conversation_id, "system", f"Auto iterate failed: {final_error}", {
      "system_event": "auto_iterate_error",
      "auto_iterate": True,
    })
  finally:
    try:
      update_auto_iterate_state(
        conversation_id,
        enabled=False,
        status=final_status,
        error=final_error,
        round_no=rounds_done,
      )
    finally:
      with store_lock:
        auto_iterate_threads.pop(conversation_id, None)
        auto_iterate_controls.pop(conversation_id, None)


def start_auto_iterate(conversation_id: str, first_text: str, max_rounds: int) -> None:
  with store_lock:
    existing = auto_iterate_threads.get(conversation_id)
    if existing and existing.is_alive():
      raise RuntimeError("auto iterate already running")

  control = _get_or_create_auto_iterate_control(conversation_id)
  stop_event = control.get("stop_event")
  if isinstance(stop_event, threading.Event):
    stop_event.clear()
  set_auto_iterate_phase(conversation_id, phase="iterating", current_job_id=None, cursor_cancel=None)

  update_auto_iterate_state(conversation_id, enabled=True, status="iterating", round_no=0)
  thread = threading.Thread(
    target=run_auto_iterate_worker,
    args=(conversation_id, first_text, max_rounds),
    daemon=True,
    name=f"auto-iterate-{conversation_id[:8]}",
  )
  with store_lock:
    auto_iterate_threads[conversation_id] = thread
  thread.start()


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
  conv = get_conversation(conversation_id)
  if not conv:
    return jsonify({"error": "not found"}), 404

  cleanup_results = []
  job_ids = list(dict.fromkeys(conv.get("job_ids", []) or []))
  for job_id in job_ids:
    status_code, payload = zhh_request(ZHH_SERVER_URL, "POST", f"/cancel/{job_id}")
    cleaned = status_code in {200, 404}
    cleanup_results.append({
      "job_id": job_id,
      "cleaned": cleaned,
      "upstream_status": status_code,
      "detail": payload,
    })

  deleted = delete_conversation(conversation_id)
  if not deleted:
    return jsonify({"error": "not found"}), 404

  return jsonify({
    "deleted": True,
    "conversation": conversation_summary(deleted),
    "task_cleanup": {
      "count": len(cleanup_results),
      "results": cleanup_results,
    },
  })


@app.route("/api/conversations/<conversation_id>/tasks", methods=["GET"])
def api_list_tasks(conversation_id: str):
  conv = get_conversation(conversation_id)
  if not conv:
    return jsonify({"error": "not found"}), 404
  try:
    jobs = get_conversation_jobs(ZHH_SERVER_URL, conv)
    conv, jobs = diagnose_completed_jobs_once(conversation_id, conv, jobs)
    conv, jobs = update_task_alert_state(conversation_id, conv, jobs)
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

  status_code, run_data = zhh_request(ZHH_SERVER_URL, "POST", "/run", {"cwd": conv["cwd"], "args": zhh_args})
  if status_code != 200:
    return jsonify({"error": run_data.get("error", f"/run failed with {status_code}"), "detail": run_data}), status_code

  job_id = run_data.get("job_id")
  if job_id:
    def add_job(c: dict):
      job_ids = c.setdefault("job_ids", [])
      if job_id not in job_ids:
        job_ids.append(job_id)
      task_meta = c.setdefault("task_meta", {})
      entry = task_meta.get(job_id)
      if not isinstance(entry, dict):
        entry = {}
      else:
        entry = dict(entry)
      entry["last_status"] = normalize_task_status(run_data.get("status") or "starting")
      entry["updated_at"] = utc_now()
      for key in ("zhh_args", "created_at", "final_log_file", "pane_log_file", "command", "cwd"):
        value = run_data.get(key)
        if value is not None and value != "":
          entry[key] = value
      task_meta[job_id] = entry

    update_conversation(conversation_id, add_job)
    append_message(conversation_id, "system", f"Runned job {job_id}", {
      "system_event": "task_run",
      "job_id": job_id,
      "job_status": str(run_data.get("status") or "starting"),
      "zhh_args": str(run_data.get("zhh_args") or ""),
    })

  return jsonify({"conversation_id": conversation_id, "job": run_data}), 200


@app.route("/api/conversations/<conversation_id>/auto-iterate/start", methods=["POST"])
def api_start_auto_iterate(conversation_id: str):
  conv = get_conversation(conversation_id)
  if not conv:
    return jsonify({"error": "not found"}), 404

  data = request.get_json(force=True, silent=True) or {}
  text = str(data.get("text") or "").strip()
  if not text:
    return jsonify({"error": "text is required"}), 400

  try:
    max_rounds = int(data.get("max_rounds") or 8)
  except Exception:
    max_rounds = 8
  if max_rounds <= 0:
    max_rounds = 8
  max_rounds = min(max_rounds, 20)

  if conv.get("auto_iterating"):
    if is_auto_iterate_worker_alive(conversation_id):
      return jsonify({"error": "auto iterate already running"}), 409
    update_auto_iterate_state(conversation_id, enabled=False, status="idle", error="stale auto iterate state cleared")

  try:
    start_auto_iterate(conversation_id, text, max_rounds)
  except RuntimeError as e:
    return jsonify({"error": str(e)}), 409

  latest = get_conversation(conversation_id) or conv
  return jsonify({
    "started": True,
    "conversation_id": conversation_id,
    "status": latest.get("status", "iterating"),
    "auto_iterating": bool(latest.get("auto_iterating", True)),
    "max_rounds": max_rounds,
  }), 200


@app.route("/api/conversations/<conversation_id>/auto-iterate/stop", methods=["POST"])
def api_stop_auto_iterate(conversation_id: str):
  conv = get_conversation(conversation_id)
  if not conv:
    return jsonify({"error": "not found"}), 404

  if not conv.get("auto_iterating"):
    return jsonify({"error": "auto iterate is not running"}), 409

  if not is_auto_iterate_worker_alive(conversation_id):
    canceled_job_id = cancel_latest_running_job_best_effort(conversation_id)
    update_auto_iterate_state(conversation_id, enabled=False, status="idle", error="stopped without live worker")
    append_message(conversation_id, "system", "Auto iterate stopped by user.", {
      "system_event": "auto_iterate_stopped",
      "auto_iterate": True,
    })
    detail = {
      "phase": "stale",
      "action": "cleanup",
      "job_id": canceled_job_id,
    }
    latest = get_conversation(conversation_id) or conv
    return jsonify({
      "stopped": True,
      "conversation_id": conversation_id,
      "detail": detail,
      "status": latest.get("status", "idle"),
      "auto_iterating": bool(latest.get("auto_iterating", False)),
    }), 200

  update_auto_iterate_state(conversation_id, enabled=True, status="stopping")
  detail = request_stop_auto_iterate(conversation_id)
  latest = get_conversation(conversation_id) or conv
  return jsonify({
    "stopped": True,
    "conversation_id": conversation_id,
    "detail": detail,
    "status": latest.get("status", "stopping"),
    "auto_iterating": bool(latest.get("auto_iterating", True)),
  }), 200


@app.route("/api/conversations/<conversation_id>/tasks/<job_id>/cancel", methods=["POST"])
def api_cancel_task(conversation_id: str, job_id: str):
  conv = get_conversation(conversation_id)
  if not conv:
    return jsonify({"error": "not found"}), 404

  job_ids = conv.get("job_ids", []) or []
  if job_id not in job_ids:
    return jsonify({"error": "job does not belong to this conversation"}), 404

  status_code, payload = zhh_request(ZHH_SERVER_URL, "POST", f"/cancel/{job_id}")
  if status_code == 200:
    mark_task_status(conversation_id, job_id, "canceled")
  return jsonify(payload), status_code


@app.route("/api/conversations/<conversation_id>/tasks/<job_id>", methods=["DELETE"])
def api_remove_task(conversation_id: str, job_id: str):
  conv = get_conversation(conversation_id)
  if not conv:
    return jsonify({"error": "not found"}), 404

  job_ids = conv.get("job_ids", []) or []
  if job_id not in job_ids:
    return jsonify({"error": "job does not belong to this conversation"}), 404

  status = "unknown"
  try:
    jobs = get_conversation_jobs(ZHH_SERVER_URL, conv)
    for job in jobs:
      if isinstance(job, dict) and job.get("job_id") == job_id:
        status = str(job.get("status") or "unknown").lower()
        break
  except Exception:
    pass

  if status not in {"completed", "unknown"}:
    return jsonify({"error": f"task status is {status}, only terminal tasks can be removed"}), 409

  def remove_job(c: dict):
    c["job_ids"] = [jid for jid in (c.get("job_ids", []) or []) if jid != job_id]
    task_meta = c.get("task_meta")
    if isinstance(task_meta, dict):
      task_meta.pop(job_id, None)

  updated = update_conversation(conversation_id, remove_job)
  return jsonify({
    "conversation_id": conversation_id,
    "removed": True,
    "job_id": job_id,
    "task_count": len(updated.get("job_ids", []) or []),
  }), 200


@app.route("/api/conversations/<conversation_id>/tasks/<job_id>/nickname", methods=["POST"])
def api_task_nickname(conversation_id: str, job_id: str):
  conv = get_conversation(conversation_id)
  if not conv:
    return jsonify({"error": "not found"}), 404

  job_ids = conv.get("job_ids", []) or []
  if job_id not in job_ids:
    return jsonify({"error": "job does not belong to this conversation"}), 404

  data = request.get_json(force=True, silent=True) or {}
  raw_nickname = data.get("nickname")
  if raw_nickname is None:
    return jsonify({"error": "nickname is required"}), 400

  nickname = str(raw_nickname).strip()
  if len(nickname) > 80:
    return jsonify({"error": "nickname too long (max 80 chars)"}), 400

  def set_nickname(c: dict):
    task_meta = c.setdefault("task_meta", {})
    entry = task_meta.get(job_id)
    if not isinstance(entry, dict):
      entry = {}
    else:
      entry = dict(entry)
    if nickname:
      entry["nickname"] = nickname
      entry["updated_at"] = utc_now()
      task_meta[job_id] = entry
    else:
      entry.pop("nickname", None)
      entry["updated_at"] = utc_now()
      task_meta[job_id] = entry

  update_conversation(conversation_id, set_nickname)
  return jsonify({
    "conversation_id": conversation_id,
    "job_id": job_id,
    "nickname": nickname,
  }), 200


@app.route("/api/conversations/<conversation_id>/tasks/<job_id>/log", methods=["GET"])
def api_task_log(conversation_id: str, job_id: str):
  conv = get_conversation(conversation_id)
  if not conv:
    return jsonify({"error": "not found"}), 404

  job_ids = conv.get("job_ids", []) or []
  if job_id not in job_ids:
    return jsonify({"error": "job does not belong to this conversation"}), 404

  lines = _safe_positive_int(request.args.get("lines", "500"), default=500)
  job_status = _resolve_job_status(conv, job_id)

  upstream_path = f"/log/{job_id}?lines={lines}"
  status_code, payload = zhh_request(ZHH_SERVER_URL, "GET", upstream_path)

  if status_code != 200:
    fallback_payload = None
    if not is_running_like_task_status(job_status):
      fallback_payload = _local_task_log_payload(conv, job_id, lines=lines)
    if fallback_payload is not None:
      clear_task_unread_alert(conversation_id, job_id)
      return jsonify(fallback_payload), 200
    if status_code in {404, 410}:
      clear_task_unread_alert(conversation_id, job_id)
      return jsonify({
        "job_id": job_id,
        "lines": lines,
        "log": f"[{job_id}] is canceled or removed; upstream log is no longer available.",
        "source": "synthetic",
      }), 200
    detail = payload.get("error") if isinstance(payload, dict) else str(payload)
    return jsonify({
      "error": f"upstream log request failed: GET {ZHH_SERVER_URL}{upstream_path}",
      "upstream_status": status_code,
      "upstream_detail": detail,
    }), status_code

  if not isinstance(payload, dict) or ("log" not in payload):
    fallback_payload = None
    if not is_running_like_task_status(job_status):
      fallback_payload = _local_task_log_payload(conv, job_id, lines=lines)
    if fallback_payload is not None:
      clear_task_unread_alert(conversation_id, job_id)
      return jsonify(fallback_payload), 200
    return jsonify({
      "error": f"upstream returned unexpected payload: GET {ZHH_SERVER_URL}{upstream_path}",
      "upstream_status": status_code,
      "upstream_payload": payload,
    }), 502

  if not str(payload.get("log", "")).strip():
    fallback_payload = None
    if not is_running_like_task_status(job_status):
      fallback_payload = _local_task_log_payload(conv, job_id, lines=lines)
    if fallback_payload is not None:
      clear_task_unread_alert(conversation_id, job_id)
      return jsonify(fallback_payload), 200
    return jsonify({
      "error": f"upstream returned empty log: GET {ZHH_SERVER_URL}{upstream_path}",
      "upstream_status": status_code,
      "upstream_payload": payload,
    }), 502

  clear_task_unread_alert(conversation_id, job_id)

  return jsonify(payload), status_code


@app.route("/api/conversations/<conversation_id>/messages", methods=["POST"])
def api_send_message(conversation_id: str):
  data = request.get_json(force=True, silent=True) or {}
  text = (data.get("text") or "").strip()
  if not text:
    return jsonify({"error": "empty text"}), 400
  force_send = bool(data.get("force_send", False))

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
  if conv.get("auto_iterating"):
    return jsonify({"error": "auto iterate is running for this conversation"}), 409

  conv_job_ids = set(conv.get("job_ids", []) or [])
  invalid_refs = [ref for ref in normalized_refs if ref not in conv_job_ids]
  if invalid_refs:
    return jsonify({"error": "some task_refs do not belong to this conversation", "invalid_task_refs": invalid_refs}), 400

  lock = get_conversation_lock(conversation_id)
  if not lock.acquire(blocking=False):
    if force_send:
      return jsonify({"error": "conversation is actively processing; force send unavailable until current request finishes"}), 409
    return jsonify({"error": "conversation busy"}), 409

  try:
    update_conversation(conversation_id, lambda c: c.update({"status": "running"}))

    refs_payload: list[dict] = []
    ref_sources: dict[str, str] = {}
    for job_id in normalized_refs:
      status_code, payload = fetch_task_reference_payload(ZHH_SERVER_URL, job_id, lines=400)
      if status_code == 200 and isinstance(payload, dict) and "stdout" in payload:
        source = str(payload.get("stdout_source") or "").strip()
        full_log_path = str(payload.get("full_log_path") or "").strip()
        if source and source != "zhh_log":
          ref_sources[job_id] = source
        else:
          ref_sources[job_id] = "tmux out"
        stdout_text = str(payload.get("stdout", ""))
        if full_log_path:
          stdout_text = (
            f"{stdout_text}\n\n"
            f"[Full log path]\n{full_log_path}"
          )
        refs_payload.append({
          "stdout": stdout_text,
        })
      else:
        ref_sources[job_id] = "tmux out"
        refs_payload.append({
          "stdout": "",
        })

    prompt_text = build_prompt_with_task_refs(text, refs_payload)
    append_message(conversation_id, "user", text, {
      "task_refs": normalized_refs,
      "task_ref_sources": ref_sources,
    })

    result = acp_prompt_session(
      agent_path=AGENT_PATH,
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
    err_text = str(e).strip() or f"{type(e).__name__}: unknown error"
    update_conversation(conversation_id, lambda c: c.update({"status": "error", "last_error": err_text}))
    return jsonify({"error": err_text}), 500
  finally:
    lock.release()



@app.route("/")
def index():
  html = UI_TEMPLATE_PATH.read_text(encoding="utf-8")
  html = html.replace("__WORKDIR_ROOT__", str(WORKDIR_ROOT))
  return html, 200, {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
      "Pragma": "no-cache",
      "Expires": "0",
  }


register_yaml_editor_routes(app, get_conversation)
register_ka_editor_routes(app, get_conversation)


def main():
    global SERVER_CWD, AGENT_PATH

    parser = argparse.ArgumentParser(description="Cursor ACP conversation server")
    parser.add_argument("--host", default=DEFAULT_HOST)
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
    if not UI_TEMPLATE_PATH.exists():
      raise SystemExit(f"ui template not found: {UI_TEMPLATE_PATH}")

    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
