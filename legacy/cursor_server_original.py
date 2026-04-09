#!/usr/bin/env python3
"""
cursor_server.py — HTTP chat UI backed by Claude Code sessions.

This server drives Claude Code CLI sessions per conversation and stores the
mapping between UI conversations and resumed session IDs. It does not depend on
any pre-existing tmux windows.

Usage:
    python cursor_server.py [--port 7860] [--host 0.0.0.0] [--cwd PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import getpass
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from acp_runtime import acp_prompt_session, get_model_policy_status, note_usage_limit_error
from core.tasks.session_job_tools import execute_session_job_actions
from runtime.agent_action_protocol import (
    extract_session_job_actions,
    format_session_job_parse_errors_message,
    new_action_nonce,
)
from runtime.agent_prompts import append_server_nonce_footer
from auto_fix_runtime import AutoFixCoordinator
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
  fetch_task_output_log_path,
  fetch_task_reference_payload,
  get_conversation_jobs,
  zhh_request,
)
from yaml_editor_api import register_ka_editor_routes, register_yaml_editor_routes


APP_ROOT = Path(__file__).parent.absolute()
CONFIG_PATH = APP_ROOT / "config.json"
WANDB_URL_PATTERN = re.compile(r"https?://(?:[A-Za-z0-9-]+\.)*wandb\.(?:ai|me)/[^\s\"'<>())]+")
COMPLETION_DIAGNOSIS_RULE_VERSION = 2
WECODE_USER = str(
  os.environ.get("WECODE_USER")
  or os.environ.get("CURCHAT_USER")
  or os.environ.get("WHO")
  or getpass.getuser()
).strip()
os.environ.setdefault("WECODE_USER", WECODE_USER)
os.environ.setdefault("CURCHAT_USER", WECODE_USER)
CURCHAT_USER = WECODE_USER


def _default_user_code_root() -> Path:
  candidate = Path(f"/kmh-nfs-ssd-us-mount/code/{WECODE_USER}").expanduser()
  if candidate.exists() and candidate.is_dir():
    return candidate.resolve()
  return APP_ROOT.parent


def load_ui_config() -> dict:
  default_code_root = _default_user_code_root()
  defaults = {
    "host": "0.0.0.0",
    "port": 7860,
    "workdir_root": str(default_code_root),
    "default_cwd": str(default_code_root),
    "agent_path": "claude",
    "store_file": "cursor_sessions.json",
    "task_server_url": "http://localhost:8080",
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
DEFAULT_AGENT = (
  os.environ.get("CLAUDE_CODE_PATH")
  or os.environ.get("CURSOR_AGENT_PATH")
  or str(UI_CONFIG.get("agent_path") or "claude")
)
store_file = Path(str(UI_CONFIG.get("store_file") or "cursor_sessions.json"))
STORE_PATH = store_file if store_file.is_absolute() else (APP_ROOT / store_file)
DEFAULT_CWD = str(config_path_value(UI_CONFIG.get("default_cwd") or APP_ROOT, APP_ROOT))
WORKDIR_ROOT = config_path_value(UI_CONFIG.get("workdir_root") or APP_ROOT.parent, APP_ROOT.parent)
ZHH_SERVER_URL = str(
  os.environ.get("WECODE_TASK_SERVER_URL")
  or os.environ.get("CURCHAT_TASK_SERVER_URL")
  or os.environ.get("ZHH_SERVER_URL")
  or UI_CONFIG.get("task_server_url")
  or "http://localhost:8080"
).strip()
UI_TEMPLATE_PATH = APP_ROOT / "cursor_server_ui.html"
SESSION_DEFAULT_MODEL = "opus"
SESSION_DEFAULT_EFFORT = "high"
ALLOWED_SESSION_MODELS = {"opus", "sonnet", "haiku"}
ALLOWED_SESSION_EFFORTS = {"low", "medium", "high", "max"}

app = Flask(__name__)

store_lock = threading.Lock()
conversation_locks: dict[str, threading.Lock] = {}
SERVER_CWD = DEFAULT_CWD
AGENT_PATH = DEFAULT_AGENT
try:
  AUTO_FIX_SCHEDULER_INTERVAL_SECONDS = int(os.environ.get("AUTO_FIX_SCHEDULER_INTERVAL_SECONDS", "10"))
except Exception:
  AUTO_FIX_SCHEDULER_INTERVAL_SECONDS = 10


def utc_now() -> float:
    return time.time()


agent_activity_lock = threading.Lock()
agent_activity_by_conversation: dict[str, dict] = {}


def _new_activity_entry(text: str, kind: str = "info") -> dict:
  return {
    "id": str(uuid.uuid4()),
    "text": str(text or "").strip(),
    "kind": str(kind or "info").strip() or "info",
    "created_at": utc_now(),
  }


def reset_agent_activity(conversation_id: str, seed_text: str | None = None) -> None:
  target_id = str(conversation_id or "").strip()
  if not target_id:
    return
  entries: list[dict] = []
  if seed_text:
    entries.append(_new_activity_entry(seed_text, "info"))
  with agent_activity_lock:
    agent_activity_by_conversation[target_id] = {
      "conversation_id": target_id,
      "running": True,
      "updated_at": utc_now(),
      "entries": entries,
    }


def append_agent_activity(conversation_id: str, text: str, kind: str = "info") -> None:
  target_id = str(conversation_id or "").strip()
  content = str(text or "").strip()
  if not target_id or not content:
    return

  with agent_activity_lock:
    payload = agent_activity_by_conversation.get(target_id)
    if not isinstance(payload, dict):
      payload = {
        "conversation_id": target_id,
        "running": True,
        "updated_at": utc_now(),
        "entries": [],
      }

    entries = payload.get("entries")
    if not isinstance(entries, list):
      entries = []

    if entries:
      last = entries[-1]
      if isinstance(last, dict) and str(last.get("text") or "").strip() == content:
        payload["updated_at"] = utc_now()
        payload["entries"] = entries
        payload["running"] = True
        agent_activity_by_conversation[target_id] = payload
        return

    entries.append(_new_activity_entry(content, kind))
    if len(entries) > 240:
      entries = entries[-240:]

    payload["entries"] = entries
    payload["running"] = True
    payload["updated_at"] = utc_now()
    agent_activity_by_conversation[target_id] = payload


def finish_agent_activity(conversation_id: str, error_text: str | None = None) -> None:
  target_id = str(conversation_id or "").strip()
  if not target_id:
    return
  with agent_activity_lock:
    payload = agent_activity_by_conversation.get(target_id)
    if not isinstance(payload, dict):
      payload = {
        "conversation_id": target_id,
        "running": False,
        "updated_at": utc_now(),
        "entries": [],
      }
    payload["running"] = False
    payload["updated_at"] = utc_now()
    entries = payload.get("entries")
    if not isinstance(entries, list):
      entries = []
    if error_text:
      compact = str(error_text).strip().replace("\n", " ")
      if len(compact) > 360:
        compact = compact[:357] + "..."
      entries.append(_new_activity_entry(compact, "error"))
      if len(entries) > 240:
        entries = entries[-240:]
    payload["entries"] = entries
    agent_activity_by_conversation[target_id] = payload


def clear_agent_activity(conversation_id: str) -> None:
  target_id = str(conversation_id or "").strip()
  if not target_id:
    return
  with agent_activity_lock:
    agent_activity_by_conversation.pop(target_id, None)


def _brief_agent_tool_input(payload: dict) -> str:
  if not isinstance(payload, dict):
    return ""
  for key in ("command", "file_path", "description", "prompt", "query", "pattern", "url"):
    value = payload.get(key)
    if value is None:
      continue
    text = str(value).strip().replace("\n", " ")
    if not text:
      continue
    if len(text) > 140:
      text = text[:137] + "..."
    return text
  return ""


def _format_agent_event_lines(event: dict) -> list[tuple[str, str]]:
  if not isinstance(event, dict):
    return []
  etype = str(event.get("type") or "").strip().lower()
  subtype = str(event.get("subtype") or "").strip().lower()
  lines: list[tuple[str, str]] = []

  if etype == "system" and subtype == "init":
    model = str(event.get("model") or "").strip() or "unknown"
    sid = str(event.get("session_id") or "").strip()
    sid_short = sid[:12] if sid else "-"
    lines.append((f"Session ready · model={model} · id={sid_short}", "info"))
    return lines

  if etype == "system" and subtype == "task_started":
    desc = str(event.get("description") or "").strip()
    if desc:
      lines.append((f"Task started: {desc}", "info"))
      return lines

  if etype == "system" and subtype == "task_progress":
    tool = str(event.get("last_tool_name") or "").strip()
    desc = str(event.get("description") or "").strip()
    if tool and desc:
      lines.append((f"[{tool}] {desc}", "info"))
      return lines
    if desc:
      lines.append((desc, "info"))
      return lines

  if etype == "system" and subtype == "task_notification":
    status = str(event.get("status") or "").strip() or "unknown"
    summary = str(event.get("summary") or event.get("description") or "").strip() or "task"
    kind = "success" if status.lower() == "completed" else "warn"
    lines.append((f"Task {status}: {summary}", kind))
    return lines

  if etype == "assistant":
    message_raw = event.get("message")
    message: dict = message_raw if isinstance(message_raw, dict) else {}
    content_raw = message.get("content")
    blocks = content_raw if isinstance(content_raw, list) else []
    for block in blocks:
      if not isinstance(block, dict):
        continue
      block_type = str(block.get("type") or "").strip().lower()
      if block_type == "tool_use":
        name = str(block.get("name") or "").strip() or "tool"
        input_raw = block.get("input")
        input_payload: dict = input_raw if isinstance(input_raw, dict) else {}
        detail = _brief_agent_tool_input(input_payload)
        if detail:
          lines.append((f"Tool {name}: {detail}", "info"))
        else:
          lines.append((f"Tool {name}", "info"))
    return lines

  if etype == "user":
    message_raw = event.get("message")
    message: dict = message_raw if isinstance(message_raw, dict) else {}
    content_raw = message.get("content")
    blocks = content_raw if isinstance(content_raw, list) else []
    for block in blocks:
      if not isinstance(block, dict):
        continue
      if str(block.get("type") or "").strip().lower() != "tool_result":
        continue
      if bool(block.get("is_error")):
        content = block.get("content")
        text = str(content or "").strip().replace("\n", " ")
        if len(text) > 150:
          text = text[:147] + "..."
        if text:
          lines.append((f"Tool error: {text}", "error"))
    return lines

  if etype == "rate_limit_event":
    info_raw = event.get("rate_limit_info")
    info: dict = info_raw if isinstance(info_raw, dict) else {}
    status = str(info.get("status") or "").strip()
    if status and status.lower() != "allowed":
      lines.append((f"Rate limit: {status}", "warn"))
    return lines

  if etype == "result":
    is_error = bool(event.get("is_error"))
    if is_error:
      detail = str(event.get("result") or "").strip()
      if len(detail) > 160:
        detail = detail[:157] + "..."
      lines.append((f"Run failed: {detail or 'unknown error'}", "error"))
    else:
      lines.append(("Run completed", "success"))
    return lines

  return lines


def record_agent_event(conversation_id: str, event: dict) -> None:
  for text, kind in _format_agent_event_lines(event):
    append_agent_activity(conversation_id, text, kind)
  if isinstance(event, dict) and str(event.get("type") or "").strip().lower() == "result":
    finish_agent_activity(conversation_id)


def get_agent_activity_payload(conversation_id: str, limit: int = 120) -> dict:
  target_id = str(conversation_id or "").strip()
  with agent_activity_lock:
    payload = agent_activity_by_conversation.get(target_id)
    if not isinstance(payload, dict):
      return {
        "conversation_id": target_id,
        "running": False,
        "updated_at": utc_now(),
        "entries": [],
      }
    entries = payload.get("entries")
    if not isinstance(entries, list):
      entries = []
    safe_limit = max(1, min(int(limit or 120), 400))
    sliced = entries[-safe_limit:]
    return {
      "conversation_id": target_id,
      "running": bool(payload.get("running")),
      "updated_at": payload.get("updated_at"),
      "entries": sliced,
    }


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


def normalize_new_dir_name(name: str) -> str:
  value = str(name or "").strip()
  if not value:
    raise ValueError("new_dir_name is required")
  if not re.match(r"^[A-Za-z0-9._-]+$", value):
    raise ValueError("new_dir_name must match [A-Za-z0-9._-]+")
  if value in {".", ".."}:
    raise ValueError("invalid new_dir_name")
  return value


def destination_from_parent(parent_dir: str, new_dir_name: str) -> Path:
  parent = normalize_workdir(parent_dir)
  name = normalize_new_dir_name(new_dir_name)
  target = (parent / name).resolve()
  try:
    target.relative_to(WORKDIR_ROOT)
  except ValueError as exc:
    raise ValueError(f"target must be inside {WORKDIR_ROOT}") from exc
  if target.exists():
    raise ValueError(f"target directory already exists: {target}")
  return target


def ensure_github_repo_url(url: str) -> str:
  value = str(url or "").strip()
  if not value:
    raise ValueError("repo_url is required")
  if "github.com" not in value:
    raise ValueError("repo_url must be a GitHub repository URL")
  return value


def create_workdir_by_clone(parent_dir: str, repo_url: str, new_dir_name: str) -> Path:
  destination = destination_from_parent(parent_dir, new_dir_name)
  safe_url = ensure_github_repo_url(repo_url)
  result = subprocess.run(
    ["git", "clone", safe_url, str(destination)],
    capture_output=True,
    text=True,
    timeout=600,
  )
  if result.returncode != 0:
    detail = (result.stderr or result.stdout or "git clone failed").strip()
    raise RuntimeError(detail)
  return destination


def create_workdir_by_worktree(source_dir: str, branch_name: str, new_dir_name: str) -> Path:
  source = normalize_workdir(source_dir)
  branch = str(branch_name or "").strip()
  if not branch:
    raise ValueError("branch_name is required")
  destination = destination_from_parent(str(source.parent), new_dir_name)

  check = subprocess.run(
    ["git", "-C", str(source), "rev-parse", "--is-inside-work-tree"],
    capture_output=True,
    text=True,
    timeout=30,
  )
  if check.returncode != 0:
    raise ValueError(f"source directory is not a git repository: {source}")

  result = subprocess.run(
    ["git", "-C", str(source), "worktree", "add", "-b", branch, str(destination)],
    capture_output=True,
    text=True,
    timeout=600,
  )
  if result.returncode != 0:
    detail = (result.stderr or result.stdout or "git worktree add failed").strip()
    raise RuntimeError(detail)
  return destination


def _sanitize_auto_dir_name(name: str) -> str:
  value = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "").strip()).strip("-._")
  return value or "copy"


def create_workdir_by_copy(source_dir: str, new_dir_name: str | None = None) -> Path:
  source = normalize_workdir(source_dir)
  raw_name = str(new_dir_name or "").strip()
  if raw_name:
    destination = destination_from_parent(str(source.parent), raw_name)
  else:
    base = _sanitize_auto_dir_name(source.name)
    candidate_name = f"{base}-copy"
    destination = (source.parent / candidate_name).resolve()
    index = 2
    while destination.exists():
      destination = (source.parent / f"{candidate_name}-{index}").resolve()
      index += 1

  try:
    destination.relative_to(WORKDIR_ROOT)
  except ValueError as exc:
    raise ValueError(f"target must be inside {WORKDIR_ROOT}") from exc

  if destination.exists():
    raise ValueError(f"target directory already exists: {destination}")

  try:
    shutil.copytree(source, destination, symlinks=True)
  except Exception as exc:
    raise RuntimeError(f"copy directory failed: {exc}") from exc
  return destination


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
    checked_version = int(entry.get("completion_log_rule_version") or 0)
    needs_recheck = (not checked) or (checked_version != COMPLETION_DIAGNOSIS_RULE_VERSION)

    if status == "completed" and needs_recheck:
      log_text = ""
      output_status, output_path, _ = fetch_task_output_log_path(ZHH_SERVER_URL, job_id)
      if output_status == 200 and output_path:
        log_text = _tail_text_file(Path(output_path), lines=1200)
      else:
        status_code, payload = fetch_task_log_payload(ZHH_SERVER_URL, job_id, lines=1200)
        if status_code == 200 and isinstance(payload, dict):
          log_text = str(payload.get("log") or "")

      exit_code_raw = job.get("exit_code")
      has_nonzero_exit_code = False
      try:
        if exit_code_raw is not None and str(exit_code_raw).strip() != "":
          has_nonzero_exit_code = int(exit_code_raw) != 0
      except Exception:
        has_nonzero_exit_code = False

      has_error = has_nonzero_exit_code or has_error_signature_in_log(log_text)
      entry["completion_log_checked"] = True
      entry["completion_log_checked_at"] = utc_now()
      entry["completion_log_rule_version"] = COMPLETION_DIAGNOSIS_RULE_VERSION
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

    became_failed = (
      bool(old_status)
      and old_status != now_status
      and not is_failed_task_status(old_status)
      and is_failed_task_status(now_status)
    )
    already_attempted = bool(prev_entry.get("auto_fix_attempted", False))
    if became_failed and not already_attempted:
      next_entry["auto_fix_pending"] = True
      next_entry["auto_fix_pending_at"] = utc_now()
    elif not is_failed_task_status(now_status):
      next_entry.pop("auto_fix_pending", None)
      next_entry.pop("auto_fix_pending_at", None)

    needs_output_log_capture = (
      is_terminal_task_status(now_status)
      and not str(prev_entry.get("full_log_path") or "").strip()
      and normalize_task_status(prev_entry.get("output_log_path_checked_status")) != now_status
    )
    if needs_output_log_capture:
      captured_path = ""
      try:
        capture_status, capture_payload = fetch_task_reference_payload(ZHH_SERVER_URL, job_id, lines=400)
        if capture_status == 200 and isinstance(capture_payload, dict):
          candidate = str(capture_payload.get("full_log_path") or capture_payload.get("stdout_source") or "").strip()
          if candidate:
            candidate_path = Path(candidate)
            if candidate_path.exists() and candidate_path.is_file():
              captured_path = str(candidate_path)
      except Exception:
        captured_path = ""

      if captured_path:
        next_entry["full_log_path"] = captured_path
      next_entry["output_log_path_checked_status"] = now_status
      next_entry["output_log_path_checked_at"] = utc_now()

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
    if str(next_entry.get("full_log_path") or "").strip():
      enriched["full_log_path"] = str(next_entry.get("full_log_path") or "")
    updated_jobs.append(enriched)

  if prev_meta != next_meta:
    def set_task_meta(c: dict):
      c["task_meta"] = next_meta

    conv = update_conversation(conversation_id, set_task_meta)

  return conv, updated_jobs


def apply_running_display_overrides(jobs: list[dict]) -> list[dict]:
  updated_jobs: list[dict] = []
  for job in jobs:
    if not isinstance(job, dict):
      continue
    enriched = dict(job)
    status = normalize_task_status(enriched.get("status"))
    if status == "running":
      display_status = "running"
      try:
        job_id = str(enriched.get("job_id") or "").strip()
        if job_id:
          code, payload = fetch_task_log_payload(ZHH_SERVER_URL, job_id, lines=120)
          if code == 200 and isinstance(payload, dict):
            log_text = str(payload.get("log") or "")
            tail_text = "\n".join(log_text.splitlines()[-20:]).lower()
            if "creating tpu vm..." in tail_text:
              display_status = "creating"
      except Exception:
        display_status = "running"
      enriched["display_status"] = display_status
    updated_jobs.append(enriched)
  return updated_jobs


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


def _read_text_file(path: Path, max_chars: int = 2_000_000) -> str:
  try:
    raw = path.read_text(encoding="utf-8", errors="replace")
  except Exception:
    return ""
  if len(raw) <= max_chars:
    return raw
  return raw[:max_chars]


def _extract_wandb_url_from_text(text: str) -> str | None:
  content = str(text or "")
  if not content.strip():
    return None

  repaired = content
  while True:
    updated = re.sub(
      r"(https?://[^\s\"'<>]+)\n([A-Za-z0-9/_?&=%#@:+.-]+)",
      r"\1\2",
      repaired,
    )
    if updated == repaired:
      break
    repaired = updated

  candidates = []
  for match in WANDB_URL_PATTERN.finditer(repaired):
    raw_url = match.group(0).rstrip(".,;)")
    run_url = re.search(
      r"(https?://(?:[A-Za-z0-9-]+\.)*wandb\.(?:ai|me)/[^\s\"'<>]*/runs/[A-Za-z0-9]{8})",
      raw_url,
    )
    if run_url:
      candidates.append(run_url.group(1))
    else:
      candidates.append(raw_url)
  if not candidates:
    return None

  def score(url: str) -> tuple[int, int]:
    lower = url.lower()
    return (
      3 if "/runs/" in lower else (2 if "wandb.ai" in lower else 1),
      len(url),
    )

  return max(candidates, key=score)


def _extract_wandb_url_from_file(path_text: str) -> str | None:
  path = Path(str(path_text or "").strip())
  if not path_text or not path.exists() or not path.is_file():
    return None
  return _extract_wandb_url_from_text(_read_text_file(path))


def resolve_task_wandb_url(conv: dict, job_id: str) -> tuple[str | None, str]:
  status_code, payload = fetch_task_reference_payload(ZHH_SERVER_URL, job_id, lines=12000)
  if status_code == 200 and isinstance(payload, dict):
    full_log_path = str(payload.get("full_log_path") or "").strip()
    if full_log_path:
      from_output = _extract_wandb_url_from_file(full_log_path)
      if from_output:
        return from_output, full_log_path

    for key in ("stdout", "log"):
      candidate = _extract_wandb_url_from_text(str(payload.get(key) or ""))
      if candidate:
        return candidate, key

  task_meta = conv.get("task_meta")
  if isinstance(task_meta, dict):
    entry = task_meta.get(job_id)
    if isinstance(entry, dict):
      for key in ("pane_log_file", "final_log_file", "cancel_log_file"):
        candidate_path = str(entry.get(key) or "").strip()
        if not candidate_path:
          continue
        candidate = _extract_wandb_url_from_file(candidate_path)
        if candidate:
          return candidate, candidate_path

  local_payload = _local_task_log_payload(conv, job_id, lines=20000, prefer_pane=True)
  if isinstance(local_payload, dict):
    candidate = _extract_wandb_url_from_text(str(local_payload.get("log") or ""))
    if candidate:
      source = str(local_payload.get("log_path") or local_payload.get("source") or "local")
      return candidate, source

  return None, ""


def resolve_task_output_log_path(job_id: str) -> str | None:
  status_code, payload = fetch_task_reference_payload(ZHH_SERVER_URL, job_id, lines=12000)
  if status_code != 200 or not isinstance(payload, dict):
    return None

  for key in ("full_log_path", "stdout_source"):
    value = str(payload.get(key) or "").strip()
    if not value:
      continue
    path = Path(value)
    if path.exists() and path.is_file():
      return str(path)
  return None


def _local_task_log_payload(conv: dict, job_id: str, lines: int = 500, prefer_pane: bool = False) -> dict | None:
  task_meta = conv.get("task_meta")
  if not isinstance(task_meta, dict):
    return None
  entry = task_meta.get(job_id)
  if not isinstance(entry, dict):
    return None

  key_order = (
    ("pane_log_file", "final_log_file", "cancel_log_file")
    if prefer_pane
    else ("cancel_log_file", "final_log_file", "pane_log_file")
  )
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


def _cached_full_log_path(conv: dict, job_id: str) -> str:
  task_meta = conv.get("task_meta") if isinstance(conv, dict) else None
  if not isinstance(task_meta, dict):
    return ""
  entry = task_meta.get(job_id)
  if not isinstance(entry, dict):
    return ""
  value = str(entry.get("full_log_path") or "").strip()
  if not value:
    return ""
  path = Path(value)
  if not path.exists() or not path.is_file():
    return ""
  return str(path)


def _build_task_reference_payload(
  conversation_id: str,
  conv: dict,
  job_id: str,
  *,
  lines: int = 400,
) -> tuple[str, str]:
  status_code, payload = fetch_task_reference_payload(ZHH_SERVER_URL, job_id, lines=lines)
  if status_code != 200 or not isinstance(payload, dict) or "stdout" not in payload:
    cached_path = _cached_full_log_path(conv, job_id)
    if cached_path:
      cached_text = _tail_text_file(Path(cached_path), lines=lines)
      return (
        f"{cached_text}\n\n"
        "[Full log path]\n"
        f"{cached_path}\n"
        "Use the path above to inspect the full output log; do not inline the whole file.",
        cached_path,
      )
    err = "failed to resolve output.log path"
    if isinstance(payload, dict):
      err = str(payload.get("error") or err)
    raise RuntimeError(f"task {job_id} reference failed: {err}")

  source = str(payload.get("stdout_source") or "").strip()
  full_log_path = str(payload.get("full_log_path") or "").strip()
  stdout_text = str(payload.get("stdout", ""))
  if not full_log_path:
    raise RuntimeError(f"task {job_id} reference failed: output.log path missing")

  stdout_text = (
    f"{stdout_text}\n\n"
    "[Full log path]\n"
    f"{full_log_path}\n"
    "Use the path above to inspect the full output log; do not inline the whole file."
  )

  def save_full_path(c: dict):
    task_meta = c.setdefault("task_meta", {})
    entry = task_meta.get(job_id)
    if not isinstance(entry, dict):
      entry = {}
    else:
      entry = dict(entry)
    if str(entry.get("full_log_path") or "") != full_log_path:
      entry["full_log_path"] = full_log_path
      entry["updated_at"] = utc_now()
    task_meta[job_id] = entry

  update_conversation(conversation_id, save_full_path)
  ref_source = source if source else full_log_path
  return stdout_text, ref_source


def snapshot_task_log_before_cancel(conversation_id: str, job_id: str, *, lines: int = 2000) -> str | None:
  full_output_log_path = ""
  try:
    output_status, output_path, _ = fetch_task_output_log_path(ZHH_SERVER_URL, job_id)
    if output_status == 200 and output_path:
      full_output_log_path = str(output_path).strip()
      if full_output_log_path:
        p = Path(full_output_log_path)
        if not p.exists() or not p.is_file():
          full_output_log_path = ""
  except Exception:
    full_output_log_path = ""

  snapshot_path: Path | None = None
  try:
    status_code, payload = fetch_task_log_payload(ZHH_SERVER_URL, job_id, lines=lines)
  except Exception:
    status_code, payload = 0, None

  if status_code == 200 and isinstance(payload, dict):
    text = str(payload.get("log") or "")
    if text.strip():
      try:
        logs_dir = APP_ROOT / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = logs_dir / f"{job_id}.cancel.log"
        snapshot_path.write_text(text, encoding="utf-8")
      except Exception:
        snapshot_path = None

  def updater(c: dict):
    task_meta = c.setdefault("task_meta", {})
    entry = task_meta.get(job_id)
    if not isinstance(entry, dict):
      entry = {}
    else:
      entry = dict(entry)
    if snapshot_path is not None:
      entry["cancel_log_file"] = str(snapshot_path)
    if full_output_log_path:
      entry["full_log_path"] = full_output_log_path
    entry["updated_at"] = utc_now()
    task_meta[job_id] = entry

  update_conversation(conversation_id, updater)
  return str(snapshot_path) if snapshot_path is not None else None


def clear_all_task_unread_alerts(conversation_id: str) -> None:
  def updater(c: dict):
    task_meta = c.get("task_meta")
    if not isinstance(task_meta, dict):
      return

    changed = False
    next_meta: dict[str, dict] = {}
    for job_id, entry in task_meta.items():
      if not isinstance(entry, dict):
        continue
      next_entry = dict(entry)
      if next_entry.get("unread"):
        next_entry["unread"] = False
        changed = True
      if next_entry.get("alert_kind"):
        next_entry["alert_kind"] = ""
        changed = True
      next_meta[job_id] = next_entry

    if changed:
      c["task_meta"] = next_meta

  update_conversation(conversation_id, updater)


def _resolve_job_status(conv: dict, job_id: str) -> str:
  try:
    jobs = get_conversation_jobs(ZHH_SERVER_URL, conv)
    conv, jobs = diagnose_completed_jobs_once(str(conv.get("id") or ""), conv, jobs)
    conv, jobs = update_task_alert_state(str(conv.get("id") or ""), conv, jobs)
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


def list_workdir_children(workdir: str | None, allow_outside_root: bool = False) -> dict:
  if allow_outside_root:
    base_value = str(workdir or "/")
    current = Path(base_value).expanduser().resolve()
    if not current.exists():
      raise ValueError(f"workdir does not exist: {current}")
    if not current.is_dir():
      raise ValueError(f"workdir is not a directory: {current}")
  else:
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

  if allow_outside_root:
    is_root = current.parent == current
  else:
    is_root = current == WORKDIR_ROOT
  parent_path = None if is_root else str(current.parent)
  if is_root:
    parent_relative_path = None
  elif allow_outside_root:
    parent_relative_path = str(current.parent)
  else:
    parent_relative_path = relative_workdir(current.parent)

  root_value = "/" if allow_outside_root else str(WORKDIR_ROOT)
  current_relative = str(current) if allow_outside_root else relative_workdir(current)

  return {
    "root": root_value,
    "current": str(current),
    "current_relative": current_relative,
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


def mark_task_error_forced(conversation_id: str, job_id: str) -> None:
  def updater(c: dict):
    task_meta = c.setdefault("task_meta", {})
    entry = task_meta.get(job_id)
    if not isinstance(entry, dict):
      entry = {}
    else:
      entry = dict(entry)
    entry["last_status"] = "error"
    entry["force_error"] = True
    entry["updated_at"] = utc_now()
    task_meta[job_id] = entry

  update_conversation(conversation_id, updater)


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


def create_conversation_record(
  title: str,
  cwd: str,
  mode: str,
  cursor_session_id: str | None,
  llm_model: str = SESSION_DEFAULT_MODEL,
  llm_effort: str = SESSION_DEFAULT_EFFORT,
) -> dict:
  return create_conversation_record_impl(
    STORE_PATH,
    store_lock,
    title,
    cwd,
    mode,
    cursor_session_id,
    utc_now,
    llm_model,
    llm_effort,
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


def _compact_text_line(text: str, limit: int = 220) -> str:
  value = re.sub(r"\s+", " ", str(text or "").strip())
  if len(value) <= limit:
    return value
  return value[: limit - 3] + "..."


def _compact_error_text(text: str, limit: int = 1200) -> str:
  value = re.sub(r"\s+", " ", str(text or "").strip())
  if not value:
    return "unknown error"
  if len(value) <= limit:
    return value
  return value[: limit - 3] + "..."


def build_conversation_memory_summary(conv: dict, max_items: int = 32, max_chars: int = 6000) -> str:
  messages = conv.get("messages") or []
  if not isinstance(messages, list):
    messages = []

  carry = str(conv.get("memory_summary") or "").strip()
  lines: list[str] = []
  if carry:
    lines.append("Previous summary:")
    lines.append(carry)

  excerpt = [m for m in messages if isinstance(m, dict) and m.get("role") in {"user", "assistant", "system"}]
  excerpt = excerpt[-max_items:]
  if excerpt:
    lines.append("Recent turns:")
  for item in excerpt:
    role = str(item.get("role") or "msg").strip().lower()
    content = _compact_text_line(str(item.get("content") or ""), limit=240)
    if not content:
      continue
    lines.append(f"- {role}: {content}")

  summary = "\n".join(lines).strip()
  if len(summary) <= max_chars:
    return summary
  return summary[-max_chars:]


def resolve_session_model(raw_model: str | None) -> str:
  model = str(raw_model or "").strip().lower()
  if model in ALLOWED_SESSION_MODELS:
    return model
  return SESSION_DEFAULT_MODEL


def resolve_session_effort(raw_effort: str | None) -> str:
  effort = str(raw_effort or "").strip().lower()
  if effort in ALLOWED_SESSION_EFFORTS:
    return effort
  return SESSION_DEFAULT_EFFORT


def parse_session_setting_command(text: str) -> tuple[str, str] | tuple[None, None]:
  raw = str(text or "").strip()
  if not raw.startswith("/"):
    return None, None
  parts = raw.split()
  if len(parts) != 2:
    return None, None
  command = str(parts[0] or "").strip().lower()
  value = str(parts[1] or "").strip().lower()
  if command == "/model":
    return "model", value
  if command == "/effort":
    return "effort", value
  return None, None


def record_run_job(conversation_id: str, run_data: dict, *, auto_run_by_agent: bool = False) -> str | None:
  job_id = str(run_data.get("job_id") or "").strip()
  if not job_id:
    return None

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
    "auto_run_by_agent": bool(auto_run_by_agent),
  })
  return job_id


AUTO_FIX_COORDINATOR = AutoFixCoordinator(
  get_conversation=get_conversation,
  get_conversation_lock=get_conversation_lock,
  update_conversation=update_conversation,
  append_message=append_message,
  resolve_job_status=_resolve_job_status,
  is_failed_task_status=is_failed_task_status,
  normalize_task_status=normalize_task_status,
  maybe_autoname=maybe_autoname,
  acp_prompt_session=acp_prompt_session,
  agent_path_getter=lambda: AGENT_PATH,
  utc_now=utc_now,
  report_agent_event=lambda conversation_id, event: record_agent_event(conversation_id, event),
)


def run_auto_fix_scheduler_loop(interval_seconds: int = AUTO_FIX_SCHEDULER_INTERVAL_SECONDS) -> None:
  interval = max(2, int(interval_seconds or 10))
  while True:
    try:
      conv_items = list_conversations()
      for item in conv_items:
        conversation_id = str((item or {}).get("id") or "").strip()
        if not conversation_id:
          continue
        conv = get_conversation(conversation_id)
        if not conv:
          continue
        try:
          jobs = get_conversation_jobs(ZHH_SERVER_URL, conv)
          conv, jobs = diagnose_completed_jobs_once(conversation_id, conv, jobs)
          conv, jobs = update_task_alert_state(conversation_id, conv, jobs)
          AUTO_FIX_COORDINATOR.maybe_schedule(conversation_id, conv, jobs)
        except Exception:
          continue
    except Exception:
      pass
    time.sleep(interval)


@app.route("/api/conversations", methods=["GET"])
def api_list_conversations():
    return jsonify({"conversations": list_conversations()})


@app.route("/api/conversations", methods=["POST"])
def api_create_conversation():
    data = request.get_json(force=True, silent=True) or {}
    create_type = str(data.get("create_type") or "directory").strip().lower()
    workdir = data.get("workdir")
    mode = data.get("mode") or "agent"

    if mode not in {"agent", "ask", "plan"}:
        return jsonify({"error": "invalid mode"}), 400

    try:
        if create_type == "directory":
          cwd = str(normalize_workdir(str(workdir or "")))
        elif create_type == "clone":
          cwd = str(create_workdir_by_clone(
            str(data.get("parent_dir") or ""),
            str(data.get("repo_url") or ""),
            str(data.get("new_dir_name") or ""),
          ))
        elif create_type == "worktree":
          cwd = str(create_workdir_by_worktree(
            str(data.get("source_dir") or ""),
            str(data.get("branch_name") or ""),
            str(data.get("new_dir_name") or ""),
          ))
        elif create_type == "copy":
          cwd = str(create_workdir_by_copy(
            str(data.get("source_dir") or ""),
            str(data.get("new_dir_name") or ""),
          ))
        else:
          return jsonify({"error": "invalid create_type"}), 400
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502

    existing = find_conversation_by_cwd(cwd)
    if existing is not None:
      return jsonify({"conversation": conversation_summary(existing), "detail": existing, "reused": True})

    title = workdir_base(cwd)
    record = create_conversation_record(
      title,
      cwd,
      mode,
      None,
      llm_model=SESSION_DEFAULT_MODEL,
      llm_effort=SESSION_DEFAULT_EFFORT,
    )
    return jsonify({"conversation": conversation_summary(record), "detail": record, "reused": False})


@app.route("/api/workdirs", methods=["GET"])
def api_workdirs():
    try:
        allow_outside_root = str(request.args.get("allow_outside_root") or "").strip().lower() in {"1", "true", "yes", "on"}
        data = list_workdir_children(request.args.get("path"), allow_outside_root=allow_outside_root)
        return jsonify(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/conversations/<conversation_id>", methods=["GET"])
def api_get_conversation(conversation_id: str):
    conv = get_conversation(conversation_id)
    if not conv:
        return jsonify({"error": "not found"}), 404
    return jsonify({"conversation": conv, "summary": conversation_summary(conv)})


@app.route("/api/conversations/<conversation_id>/activity", methods=["GET"])
def api_get_conversation_activity(conversation_id: str):
    conv = get_conversation(conversation_id)
    if not conv:
      return jsonify({"error": "not found"}), 404
    limit = _safe_positive_int(request.args.get("limit", "120"), default=120)
    return jsonify(get_agent_activity_payload(conversation_id, limit=limit))


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
  clear_agent_activity(conversation_id)

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
    jobs = apply_running_display_overrides(jobs)
    AUTO_FIX_COORDINATOR.maybe_schedule(conversation_id, conv, jobs)
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

  record_run_job(conversation_id, run_data)

  return jsonify({"conversation_id": conversation_id, "job": run_data}), 200


@app.route("/api/conversations/<conversation_id>/tasks/<job_id>/cancel", methods=["POST"])
def api_cancel_task(conversation_id: str, job_id: str):
  conv = get_conversation(conversation_id)
  if not conv:
    return jsonify({"error": "not found"}), 404

  job_ids = conv.get("job_ids", []) or []
  if job_id not in job_ids:
    return jsonify({"error": "job does not belong to this conversation"}), 404

  snapshot_task_log_before_cancel(conversation_id, job_id)
  status_code, payload = zhh_request(ZHH_SERVER_URL, "POST", f"/cancel/{job_id}")
  if status_code in {200, 404}:
    mark_task_status(conversation_id, job_id, "canceled")
  return jsonify(payload), status_code


@app.route("/api/conversations/<conversation_id>/tasks/<job_id>/mark-error", methods=["POST"])
def api_mark_task_error(conversation_id: str, job_id: str):
  conv = get_conversation(conversation_id)
  if not conv:
    return jsonify({"error": "not found"}), 404

  job_ids = conv.get("job_ids", []) or []
  if job_id not in job_ids:
    return jsonify({"error": "job does not belong to this conversation"}), 404

  status = _resolve_job_status(conv, job_id)
  if not is_running_like_task_status(status):
    return jsonify({"error": f"task status is {status}, only running-like tasks can be marked as error"}), 409

  mark_task_error_forced(conversation_id, job_id)
  snapshot_task_log_before_cancel(conversation_id, job_id)
  cancel_status, cancel_payload = zhh_request(ZHH_SERVER_URL, "POST", f"/cancel/{job_id}")

  if cancel_status in {200, 404}:
    return jsonify({
      "conversation_id": conversation_id,
      "job_id": job_id,
      "status": "error",
      "cancel_status": cancel_status,
      "cancel_payload": cancel_payload,
    }), 200

  return jsonify({
    "conversation_id": conversation_id,
    "job_id": job_id,
    "status": "error",
    "cancel_status": cancel_status,
    "cancel_payload": cancel_payload,
    "error": f"marked as error locally, but upstream cancel failed with {cancel_status}",
  }), 502


@app.route("/api/conversations/<conversation_id>/tasks/<job_id>/resume", methods=["POST"])
def api_resume_task(conversation_id: str, job_id: str):
  conv = get_conversation(conversation_id)
  if not conv:
    return jsonify({"error": "not found"}), 404

  job_ids = conv.get("job_ids", []) or []
  if job_id not in job_ids:
    return jsonify({"error": "job does not belong to this conversation"}), 404

  status = "unknown"
  try:
    jobs = get_conversation_jobs(ZHH_SERVER_URL, conv)
    conv, jobs = diagnose_completed_jobs_once(conversation_id, conv, jobs)
    conv, jobs = update_task_alert_state(conversation_id, conv, jobs)
    for job in jobs:
      if isinstance(job, dict) and str(job.get("job_id") or "") == job_id:
        status = normalize_task_status(job.get("status"))
        break
  except Exception:
    status = _resolve_job_status(conv, job_id)

  if not is_failed_task_status(status):
    return jsonify({"error": f"task status is {status}, only failed/error-like tasks can be resumed"}), 409

  output_log_path = resolve_task_output_log_path(job_id)
  if not output_log_path:
    return jsonify({"error": "output.log path not found for this task"}), 404

  status_code, run_data = zhh_request(ZHH_SERVER_URL, "POST", "/resume", {"log_path": output_log_path})
  if status_code != 200:
    return jsonify({"error": run_data.get("error", f"/resume failed with {status_code}"), "detail": run_data}), status_code

  resumed_job_id = str(run_data.get("job_id") or "").strip()
  if resumed_job_id:
    def add_job(c: dict):
      ids = c.setdefault("job_ids", [])
      if resumed_job_id not in ids:
        ids.append(resumed_job_id)

      task_meta = c.setdefault("task_meta", {})
      entry = task_meta.get(resumed_job_id)
      if not isinstance(entry, dict):
        entry = {}
      else:
        entry = dict(entry)

      entry["last_status"] = normalize_task_status(run_data.get("status") or "starting")
      entry["updated_at"] = utc_now()
      entry["resume_from_job_id"] = job_id
      entry["resume_log_path"] = output_log_path
      source_entry = task_meta.get(job_id)
      source_nickname = ""
      if isinstance(source_entry, dict):
        source_nickname = str(source_entry.get("nickname") or "").strip()
      if source_nickname:
        entry["nickname"] = f"resume · {source_nickname}"
      elif not str(entry.get("nickname") or "").strip():
        entry["nickname"] = f"resume · {job_id[:8]}"
      for key in ("zhh_args", "created_at", "final_log_file", "pane_log_file", "command", "cwd", "mode"):
        value = run_data.get(key)
        if value is not None and value != "":
          entry[key] = value
      task_meta[resumed_job_id] = entry

    update_conversation(conversation_id, add_job)
    append_message(conversation_id, "system", f"Resumed job {job_id} as {resumed_job_id}", {
      "system_event": "task_resume",
      "job_id": resumed_job_id,
      "job_status": str(run_data.get("status") or "starting"),
      "resume_from_job_id": job_id,
      "resume_log_path": output_log_path,
    })

  return jsonify({
    "conversation_id": conversation_id,
    "source_job_id": job_id,
    "log_path": output_log_path,
    "job": run_data,
  }), 200


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
        status = normalize_task_status(job.get("status"))
        break
  except Exception:
    pass

  if not is_terminal_task_status(status):
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
        "log": f"[{job_id}] upstream no longer provides this log, and no local log file snapshot was found.",
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


@app.route("/api/conversations/<conversation_id>/tasks/<job_id>/wandb", methods=["GET"])
def api_task_wandb(conversation_id: str, job_id: str):
  conv = get_conversation(conversation_id)
  if not conv:
    return jsonify({"error": "not found"}), 404

  job_ids = conv.get("job_ids", []) or []
  if job_id not in job_ids:
    return jsonify({"error": "job does not belong to this conversation"}), 404

  url, source = resolve_task_wandb_url(conv, job_id)
  if not url:
    return jsonify({"error": "wandb link not found in task logs"}), 404

  clear_task_unread_alert(conversation_id, job_id)

  return jsonify({
    "job_id": job_id,
    "wandb_url": url,
    "source": source,
  }), 200


@app.route("/api/conversations/<conversation_id>/tasks/mark-all-read", methods=["POST"])
def api_mark_all_tasks_read(conversation_id: str):
  conv = get_conversation(conversation_id)
  if not conv:
    return jsonify({"error": "not found"}), 404
  clear_all_task_unread_alerts(conversation_id)
  return jsonify({"conversation_id": conversation_id, "ok": True}), 200


@app.route("/api/conversations/<conversation_id>/compact", methods=["POST"])
def api_compact_conversation(conversation_id: str):
  conv = get_conversation(conversation_id)
  if not conv:
    return jsonify({"error": "not found"}), 404

  lock = get_conversation_lock(conversation_id)
  if not lock.acquire(blocking=False):
    return jsonify({"error": "conversation busy"}), 409

  try:
    latest = get_conversation(conversation_id)
    if not latest:
      return jsonify({"error": "not found"}), 404

    summary = build_conversation_memory_summary(latest)
    updated = update_conversation(conversation_id, lambda c: c.update({
      "memory_summary": summary,
      "memory_summary_pending": True,
      "cursor_session_id": None,
      "status": "idle",
      "compaction_count": int(c.get("compaction_count") or 0) + 1,
      "compacted_at": utc_now(),
    }))

    append_message(conversation_id, "system", "Context compacted. Session context reset with memory summary.", {
      "system_event": "conversation_compacted",
    })

    return jsonify({
      "conversation": updated,
      "summary_chars": len(summary),
      "compaction_count": int(updated.get("compaction_count") or 0),
    }), 200
  finally:
    lock.release()


@app.route("/api/conversations/<conversation_id>/auto-fix/stop", methods=["POST"])
def api_stop_auto_fix(conversation_id: str):
  conv = get_conversation(conversation_id)
  if not conv:
    return jsonify({"error": "not found"}), 404

  status = str(conv.get("status") or "").strip().lower()
  if status != "debugging":
    return jsonify({"error": "auto fix stop is only available while debugging"}), 409

  stopped, job_id = AUTO_FIX_COORDINATOR.request_stop(conversation_id)
  if not stopped:
    return jsonify({"error": "no active auto fix worker"}), 409

  return jsonify({
    "ok": True,
    "conversation_id": conversation_id,
    "job_id": str(job_id or ""),
  }), 202


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

  lowered_text = text.strip().lower()
  if lowered_text.startswith("/model") or lowered_text.startswith("/effort"):
    if len(text.split()) != 2:
      return jsonify({"error": "invalid setting command. use /model <opus|sonnet|haiku> or /effort <low|medium|high|max>"}), 400

  setting_key, setting_value = parse_session_setting_command(text)
  if setting_key is not None:
    status = str(conv.get("status") or "").strip().lower()
    if status in {"running", "debugging"}:
      return jsonify({"error": "session setting update is unavailable while agent is running"}), 409

    if setting_key == "model":
      normalized = str(setting_value or "").strip().lower()
      if normalized not in ALLOWED_SESSION_MODELS:
        return jsonify({"error": "invalid model. allowed: opus, sonnet, haiku"}), 400

      append_message(conversation_id, "user", text)
      append_message(conversation_id, "assistant", f"Model updated to `{normalized}` for this session.")
      updated = update_conversation(conversation_id, lambda c: c.update({
        "llm_model": normalized,
        "current_model": normalized,
      }))
      return jsonify({
        "conversation": updated,
        "assistant": f"Model updated to `{normalized}` for this session.",
        "setting": {"key": "model", "value": normalized},
      }), 200

    if setting_key == "effort":
      normalized = str(setting_value or "").strip().lower()
      if normalized not in ALLOWED_SESSION_EFFORTS:
        return jsonify({"error": "invalid effort. allowed: low, medium, high, max"}), 400

      append_message(conversation_id, "user", text)
      append_message(conversation_id, "assistant", f"Effort updated to `{normalized}` for this session.")
      updated = update_conversation(conversation_id, lambda c: c.update({
        "llm_effort": normalized,
        "current_effort": normalized,
      }))
      return jsonify({
        "conversation": updated,
        "assistant": f"Effort updated to `{normalized}` for this session.",
        "setting": {"key": "effort", "value": normalized},
      }), 200

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
    session_model = resolve_session_model(conv.get("llm_model"))
    session_effort = resolve_session_effort(conv.get("llm_effort"))
    reset_agent_activity(conversation_id, "Prompt sent to Claude Code.")
    update_conversation(conversation_id, lambda c: c.update({"status": "running"}))

    refs_payload = [{"job_id": job_id} for job_id in normalized_refs]
    ref_sources = {job_id: "session_job query" for job_id in normalized_refs}

    session_job_nonce = new_action_nonce()
    prompt_base = text
    prompt_text = append_server_nonce_footer(
        build_prompt_with_task_refs(prompt_base, refs_payload),
        session_nonce=session_job_nonce,
    )
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
      preferred_model=session_model,
      effort=session_effort,
      on_progress_event=lambda event: record_agent_event(conversation_id, event),
    )

    model_used = str(result.get("model") or "").strip()
    effort_used = resolve_session_effort(result.get("effort") or session_effort)
    context_tokens = result.get("context_tokens")
    context_window = result.get("context_window")
    def set_runtime_metadata(c: dict):
      if not c.get("cursor_session_id"):
        c["cursor_session_id"] = result["cursor_session_id"]
      c["llm_model"] = resolve_session_model(c.get("llm_model") or session_model)
      c["llm_effort"] = resolve_session_effort(c.get("llm_effort") or session_effort)
      c["current_model"] = model_used or session_model
      c["current_effort"] = effort_used
      if isinstance(context_tokens, int) and context_tokens >= 0:
        c["current_context_tokens"] = context_tokens
      if isinstance(context_window, int) and context_window > 0:
        c["current_context_window"] = context_window
      if c.get("memory_summary_pending"):
        c["memory_summary_pending"] = False
    update_conversation(conversation_id, set_runtime_metadata)

    assistant_raw_text = result["text"] or f"[No text returned; stopReason={result['stop_reason']}]"
    assistant_text, session_actions, parse_errors = extract_session_job_actions(
        assistant_raw_text, session_job_nonce,
    )
    if parse_errors:
      append_message(
          conversation_id,
          "system",
          format_session_job_parse_errors_message(parse_errors),
          {"system_event": "session_job_parse_error", "errors": parse_errors},
      )
    if not assistant_text.strip() and session_actions:
      assistant_text = "[Action received: session job]"
    skip_assistant_append = (
      session_actions
      and assistant_text == "[Action received: session job]"
    )
    if not skip_assistant_append and (assistant_text.strip() or session_actions):
      append_message(conversation_id, "assistant", assistant_text)

    _, triggered_job = execute_session_job_actions(conversation_id, session_actions)

    maybe_autoname(conversation_id)
    updated = update_conversation(conversation_id, lambda c: c.update({"status": "idle"}))
    finish_agent_activity(conversation_id)
    return jsonify({
      "conversation": updated,
      "assistant": assistant_text,
      "stop_reason": result["stop_reason"],
      "triggered_job_id": triggered_job,
      "session_job_parse_errors": parse_errors,
    })
  except Exception as e:
    err_text = _compact_error_text(str(e).strip() or f"{type(e).__name__}: unknown error")
    note_usage_limit_error(err_text)
    update_conversation(conversation_id, lambda c: c.update({"status": "error", "last_error": err_text}))
    finish_agent_activity(conversation_id, f"Run failed: {err_text}")
    return jsonify({"error": err_text}), 500
  finally:
    lock.release()



@app.route("/")
def index():
  policy = get_model_policy_status()
  html = UI_TEMPLATE_PATH.read_text(encoding="utf-8")
  html = html.replace("__WORKDIR_ROOT__", str(WORKDIR_ROOT))
  html = html.replace("__DEFAULT_SESSION_MODEL__", SESSION_DEFAULT_MODEL)
  html = html.replace("__DEFAULT_SESSION_EFFORT__", SESSION_DEFAULT_EFFORT)
  html = html.replace("__DEFAULT_MODEL__", str(policy.get("effective_model") or "default"))
  html = html.replace("__CONFIGURED_MODEL__", str(policy.get("configured_model") or "default"))
  return html, 200, {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
      "Pragma": "no-cache",
      "Expires": "0",
  }


@app.route("/api/runtime/model-policy", methods=["GET"])
def api_model_policy():
  return jsonify(get_model_policy_status())


@app.route("/assets/<path:filename>")
def serve_ui_asset(filename: str):
  allowed_suffixes = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico"}
  requested = Path(filename)
  if requested.suffix.lower() not in allowed_suffixes:
    return jsonify({"error": "unsupported asset type"}), 404
  asset_path = (APP_ROOT / requested).resolve()
  try:
    asset_path.relative_to(APP_ROOT)
  except ValueError:
    return jsonify({"error": "invalid asset path"}), 404
  if not asset_path.exists() or not asset_path.is_file():
    return jsonify({"error": "asset not found"}), 404
  return send_from_directory(APP_ROOT, str(requested))


@app.route("/favicon.ico")
def favicon_alias():
  favicon_path = APP_ROOT / "favicon.ico"
  if favicon_path.exists() and favicon_path.is_file():
    return send_from_directory(APP_ROOT, "favicon.ico")
  icon_path = APP_ROOT / "wecode-64.png"
  if icon_path.exists() and icon_path.is_file():
    return send_from_directory(APP_ROOT, "wecode-64.png")
  icon_path = APP_ROOT / "wecode.png"
  if icon_path.exists() and icon_path.is_file():
    return send_from_directory(APP_ROOT, "wecode.png")
  return jsonify({"error": "favicon not found"}), 404


register_yaml_editor_routes(app, get_conversation)
register_ka_editor_routes(app, get_conversation)


def bootstrap_model_policy_from_store() -> None:
    try:
      if not STORE_PATH.exists():
        return
      payload = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except Exception:
      return

    conversations = payload.get("conversations") if isinstance(payload, dict) else None
    if not isinstance(conversations, dict):
      return

    for conv in conversations.values():
      if not isinstance(conv, dict):
        continue
      err = str(conv.get("last_error") or "").strip()
      if err:
        note_usage_limit_error(err)


def main():
    global SERVER_CWD, AGENT_PATH

    parser = argparse.ArgumentParser(description="WeCode conversation server (Claude Code backbone)")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--cwd", default=DEFAULT_CWD, help="Default working directory for new sessions")
    parser.add_argument("--agent-path", default=DEFAULT_AGENT)
    args = parser.parse_args()

    SERVER_CWD = str(Path(args.cwd).expanduser())
    requested_agent = str(args.agent_path or "").strip()
    requested_path = Path(requested_agent).expanduser()
    if requested_path.is_absolute() or "/" in requested_agent:
      AGENT_PATH = str(requested_path)
    else:
      AGENT_PATH = requested_agent

    resolved_agent = ""
    if Path(AGENT_PATH).exists():
      resolved_agent = AGENT_PATH
    else:
      resolved_agent = shutil.which(AGENT_PATH) or ""
    if resolved_agent:
      AGENT_PATH = resolved_agent

    print("WeCode server (Claude Code)")
    print(f"wecode user: {WECODE_USER}")
    print(f"agent path : {AGENT_PATH}")
    print(f"default cwd: {SERVER_CWD}")
    print(f"store file : {STORE_PATH}")
    print(f"task server: {ZHH_SERVER_URL}")
    print(f"url        : http://{args.host}:{args.port}")

    if not AGENT_PATH or not Path(AGENT_PATH).exists():
        raise SystemExit(f"agent not found: {AGENT_PATH}")
    if not Path(SERVER_CWD).exists():
        raise SystemExit(f"cwd does not exist: {SERVER_CWD}")
    if not UI_TEMPLATE_PATH.exists():
      raise SystemExit(f"ui template not found: {UI_TEMPLATE_PATH}")

    bootstrap_model_policy_from_store()

    scheduler_thread = threading.Thread(
      target=run_auto_fix_scheduler_loop,
      args=(AUTO_FIX_SCHEDULER_INTERVAL_SECONDS,),
      daemon=True,
      name="auto-fix-scheduler",
    )
    scheduler_thread.start()
    print(f"auto-fix scheduler interval: {AUTO_FIX_SCHEDULER_INTERVAL_SECONDS}s")

    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
