from __future__ import annotations

import json
from urllib import error as urllib_error
from urllib import request as urllib_request


def _is_running_like_status(status: str | None) -> bool:
    return str(status or "").strip().lower() in {"running", "starting", "queued", "pending"}


def _is_local_cancel_like(status: str | None) -> bool:
    return str(status or "").strip().lower() in {"canceled", "cancelled", "aborted"}


def zhh_request(
    zhh_server_url: str,
    method: str,
    path: str,
    payload: dict | None = None,
    timeout: float = 20.0,
) -> tuple[int, dict]:
    url = f"{zhh_server_url}{path}"
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
        return 503, {"error": f"failed to reach zhh server at {zhh_server_url}: {e}"}


def get_conversation_jobs(zhh_server_url: str, conversation: dict) -> list[dict]:
    job_ids = conversation.get("job_ids", []) or []
    task_meta = conversation.get("task_meta", {}) or {}
    messages = conversation.get("messages", []) or []
    if not job_ids:
        return []

    status_code, status_data = zhh_request(zhh_server_url, "GET", "/status")
    if status_code != 200:
        raise RuntimeError(status_data.get("error", f"status code {status_code}"))

    jobs = status_data.get("jobs", [])
    by_id = {job.get("job_id"): job for job in jobs if isinstance(job, dict) and job.get("job_id")}
    system_run_jobs = {
        str(msg.get("job_id") or "").strip()
        for msg in messages
        if isinstance(msg, dict)
        and str(msg.get("role") or "") == "system"
        and str(msg.get("system_event") or "") == "task_run"
        and str(msg.get("job_id") or "").strip()
    }

    ordered = []
    for job_id in reversed(job_ids):
        raw_meta = task_meta.get(job_id)
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        nickname = ""
        if meta:
            nickname = str(meta.get("nickname") or "").strip()
        cached_status = str(meta.get("last_status") or "").strip().lower() if meta else ""
        if cached_status == "unknown":
            cached_status = ""
        if job_id in by_id:
            item = dict(by_id[job_id])
            item["nickname"] = nickname
            live_status = str(item.get("status") or "").strip().lower()
            if isinstance(meta, dict) and bool(meta.get("force_error")):
                item["status"] = "error"
            if cached_status and _is_local_cancel_like(cached_status) and _is_running_like_status(live_status):
                item["status"] = cached_status
            elif (not item.get("status") or live_status == "unknown") and cached_status:
                item["status"] = cached_status
            if meta:
                for key in (
                    "resume_from_job_id",
                    "resume_log_path",
                    "auto_fix_from_job_id",
                    "force_error",
                ):
                    value = meta.get(key)
                    if value is not None and value != "":
                        item[key] = value
            ordered.append(item)
        else:
            fallback_status = cached_status or ("canceled" if job_id in system_run_jobs else "unknown")
            item = {
                "job_id": job_id,
                "status": fallback_status,
                "missing": True,
                "nickname": nickname,
            }
            for key in (
                "zhh_args",
                "created_at",
                "final_log_file",
                "pane_log_file",
                "command",
                "cwd",
                "resume_from_job_id",
                "resume_log_path",
                "auto_fix_from_job_id",
                "force_error",
            ):
                if meta and meta.get(key):
                    item[key] = meta.get(key)
            ordered.append(item)
    return ordered


def fetch_task_log_payload(zhh_server_url: str, job_id: str, lines: int = 400) -> tuple[int, dict]:
    return zhh_request(zhh_server_url, "GET", f"/log/{job_id}?lines={lines}")


def fetch_task_output_log_path(zhh_server_url: str, job_id: str) -> tuple[int, str | None, dict]:
    status_code, payload = zhh_request(zhh_server_url, "GET", f"/status/{job_id}")
    if status_code != 200 or not isinstance(payload, dict):
        return status_code, None, payload if isinstance(payload, dict) else {"error": f"status code {status_code}"}

    output_log = str(payload.get("output_log") or "").strip()
    if not output_log:
        return 404, None, {
            "error": f"output_log unavailable for job {job_id}; ensure /job-log-dir/{job_id} was reported",
            "detail": payload,
        }

    return 200, output_log, payload


def fetch_task_reference_payload(zhh_server_url: str, job_id: str, lines: int = 400) -> tuple[int, dict]:
    status_code, payload = fetch_task_log_payload(zhh_server_url, job_id, lines=lines)
    if status_code != 200 or not isinstance(payload, dict):
        return status_code, payload

    base_stdout = str(payload.get("log", ""))
    output_status, output_path, output_payload = fetch_task_output_log_path(zhh_server_url, job_id)
    if output_status != 200 or not output_path:
        err = output_payload if isinstance(output_payload, dict) else {"error": f"status code {output_status}"}
        return output_status, err

    merged = dict(payload)
    merged["stdout"] = base_stdout
    merged["stdout_source"] = output_path
    merged["full_log_path"] = output_path

    return status_code, merged


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
