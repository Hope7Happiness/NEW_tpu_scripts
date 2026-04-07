"""任务诊断模块 - 任务状态诊断和错误检测"""
from __future__ import annotations

from pathlib import Path
from core.config import COMPLETION_DIAGNOSIS_RULE_VERSION, ZHH_SERVER_URL
from core.utils import utc_now, _tail_text_file, has_error_signature_in_log
from core.conversation import update_conversation
from tasks_runtime import fetch_task_output_log_path, fetch_task_log_payload, fetch_task_reference_payload


def diagnose_completed_jobs_once(conversation_id: str, conv: dict, jobs: list[dict]) -> tuple[dict, list[dict]]:
    """诊断已完成任务"""
    task_meta = conv.get("task_meta", {}) or {}
    if not isinstance(task_meta, dict):
        task_meta = {}

    next_meta: dict[str, dict] = {}
    changed = False
    updated_jobs: list[dict] = []

    from core.tasks.utils import normalize_task_status
    
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

            if has_nonzero_exit_code:
                entry["completion_log_reason"] = "nonzero_exit_code"
            elif has_error:
                entry["completion_log_reason"] = "error_signature"
            else:
                entry["completion_log_reason"] = "ok"

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
    """更新任务警告状态"""
    task_meta = conv.get("task_meta", {}) or {}
    if not isinstance(task_meta, dict):
        task_meta = {}

    from core.tasks.utils import (
        normalize_task_status, is_running_like_task_status, 
        is_terminal_task_status, is_failed_task_status, task_alert_kind_for_status
    )
    
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
            # 仅在状态发生变化并进入终态时标记未读，避免每次轮询都把已读重置为未读。
            if old_status != now_status and is_terminal_task_status(now_status):
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

        next_entry["unread"] = unread
        next_entry["alert_kind"] = alert_kind
        if nickname:
            next_entry["nickname"] = nickname

        enriched = dict(job)
        enriched["__meta"] = next_entry
        enriched["unread"] = unread
        enriched["alert_kind"] = alert_kind
        enriched["nickname"] = nickname

        updated_jobs.append(enriched)
        next_meta[job_id] = next_entry

    for key, value in task_meta.items():
        if key not in next_meta and isinstance(value, dict):
            next_meta[key] = value

    if next_meta != task_meta:
        def save_meta(c: dict):
            c["task_meta"] = next_meta
        
        conv = update_conversation(conversation_id, save_meta)

    return conv, updated_jobs


def apply_running_display_overrides(jobs: list[dict]) -> list[dict]:
    """应用运行中任务的显示覆盖"""
    from core.tasks.utils import is_running_like_task_status
    
    updated: list[dict] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        enriched = dict(job)
        meta = enriched.get("__meta") or {}
        if not isinstance(meta, dict):
            meta = {}

        nickname = str(meta.get("nickname") or "").strip()
        if nickname:
            enriched["nickname"] = nickname

        display = str(enriched.get("display_status") or enriched.get("status") or "").strip()
        if is_running_like_task_status(enriched.get("status")) and display == "running":
            enriched["display_status"] = "running"

        updated.append(enriched)
    return updated
