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
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from acp_runtime import acp_prompt_session, get_model_policy_status, note_usage_limit_error
from agent_action_protocol import (
  extract_run_job_action,
  new_action_nonce,
  with_run_job_skill_instruction,
)
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


def _compact_text_line(text: str, limit: int = 220) -> str:
  value = re.sub(r"\s+", " ", str(text or "").strip())
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


def build_prompt_with_memory(conv: dict, user_text: str) -> str:
  memory = str((conv or {}).get("memory_summary") or "").strip()
  memory_pending = bool((conv or {}).get("memory_summary_pending", False))
  cursor_session_id = str((conv or {}).get("cursor_session_id") or "").strip()
  text = str(user_text or "").strip()
  should_inject = bool(memory) and (memory_pending or not cursor_session_id)
  if not should_inject:
    return text
  return (
    "[Conversation memory summary]\n"
    f"{memory}\n\n"
    "[Current user request]\n"
    f"{text}"
  )


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


def trigger_run_job_for_conversation(conversation_id: str, auto_run_by_agent: bool = False) -> tuple[str | None, str | None]:
  conv = get_conversation(conversation_id)
  if not conv:
    return None, "conversation not found"
  status_code, run_data = zhh_request(ZHH_SERVER_URL, "POST", "/run", {"cwd": conv["cwd"], "args": ""})
  if status_code != 200:
    return None, str(run_data.get("error", f"/run failed with {status_code}"))
  return record_run_job(conversation_id, run_data, auto_run_by_agent=auto_run_by_agent), None


AUTO_FIX_COORDINATOR = AutoFixCoordinator(
  get_conversation=get_conversation,
  get_conversation_lock=get_conversation_lock,
  update_conversation=update_conversation,
  append_message=append_message,
  build_task_reference_payload=lambda conversation_id, conv, job_id: _build_task_reference_payload(
    conversation_id,
    conv,
    job_id,
    lines=400,
  ),
  resolve_job_status=_resolve_job_status,
  is_failed_task_status=is_failed_task_status,
  normalize_task_status=normalize_task_status,
  maybe_autoname=maybe_autoname,
  acp_prompt_session=acp_prompt_session,
  agent_path_getter=lambda: AGENT_PATH,
  trigger_run_job=trigger_run_job_for_conversation,
  utc_now=utc_now,
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
    record = create_conversation_record(title, cwd, mode, None)
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
      stdout_text, source = _build_task_reference_payload(conversation_id, conv, job_id, lines=400)
      ref_sources[job_id] = source
      refs_payload.append({
        "stdout": stdout_text,
      })

    run_action_nonce = new_action_nonce()
    prompt_base = build_prompt_with_memory(conv, text)
    prompt_text = build_prompt_with_task_refs(with_run_job_skill_instruction(prompt_base, run_action_nonce), refs_payload)
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

    model_used = str(result.get("model") or "").strip()
    if not conv.get("cursor_session_id") or model_used:
      def set_runtime_metadata(c: dict):
        if not c.get("cursor_session_id"):
          c["cursor_session_id"] = result["cursor_session_id"]
        if model_used:
          c["current_model"] = model_used
        if c.get("memory_summary_pending"):
          c["memory_summary_pending"] = False
      update_conversation(conversation_id, set_runtime_metadata)

    assistant_raw_text = result["text"] or f"[No text returned; stopReason={result['stop_reason']}]"
    assistant_text, should_run_job = extract_run_job_action(assistant_raw_text, run_action_nonce)
    if not assistant_text:
      assistant_text = "[Action received: run job]"
    if not (should_run_job and assistant_text == "[Action received: run job]"):
      append_message(conversation_id, "assistant", assistant_text)

    if should_run_job:
      append_message(conversation_id, "system", "action: run job", {
        "system_event": "agent_action_run_job",
      })

    triggered_job = None
    if should_run_job:
      triggered_job, run_err = trigger_run_job_for_conversation(conversation_id, auto_run_by_agent=True)
      if run_err:
        append_message(conversation_id, "system", f"Agent requested run job, but /run failed: {run_err}", {
          "system_event": "task_run_failed",
          "auto_run_by_agent": True,
        })

    maybe_autoname(conversation_id)
    updated = update_conversation(conversation_id, lambda c: c.update({"status": "idle"}))
    return jsonify({
      "conversation": updated,
      "assistant": assistant_text,
      "stop_reason": result["stop_reason"],
      "triggered_job_id": triggered_job,
    })
  except Exception as e:
    err_text = str(e).strip() or f"{type(e).__name__}: unknown error"
    note_usage_limit_error(err_text)
    update_conversation(conversation_id, lambda c: c.update({"status": "error", "last_error": err_text}))
    return jsonify({"error": err_text}), 500
  finally:
    lock.release()



@app.route("/")
def index():
  policy = get_model_policy_status()
  html = UI_TEMPLATE_PATH.read_text(encoding="utf-8")
  html = html.replace("__WORKDIR_ROOT__", str(WORKDIR_ROOT))
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
  icon_path = APP_ROOT / "curchat-64.png"
  if icon_path.exists() and icon_path.is_file():
    return send_from_directory(APP_ROOT, "curchat-64.png")
  icon_path = APP_ROOT / "curchat.png"
  if icon_path.exists() and icon_path.is_file():
    return send_from_directory(APP_ROOT, "curchat.png")
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
