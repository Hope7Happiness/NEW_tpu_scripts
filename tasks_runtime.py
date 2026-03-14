from __future__ import annotations

import json
import re
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request


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
        meta = task_meta.get(job_id) if isinstance(task_meta.get(job_id), dict) else {}
        nickname = ""
        if meta:
            nickname = str(meta.get("nickname") or "").strip()
        cached_status = str(meta.get("last_status") or "").strip().lower() if meta else ""
        if cached_status == "unknown":
            cached_status = ""
        if job_id in by_id:
            item = dict(by_id[job_id])
            item["nickname"] = nickname
            if (not item.get("status") or str(item.get("status")).lower() == "unknown") and cached_status:
                item["status"] = cached_status
            ordered.append(item)
        else:
            fallback_status = cached_status or ("canceled" if job_id in system_run_jobs else "unknown")
            item = {
                "job_id": job_id,
                "status": fallback_status,
                "missing": True,
                "nickname": nickname,
            }
            for key in ("zhh_args", "created_at", "final_log_file", "pane_log_file", "command", "cwd"):
                if meta and meta.get(key):
                    item[key] = meta.get(key)
            ordered.append(item)
    return ordered


def fetch_task_log_payload(zhh_server_url: str, job_id: str, lines: int = 400) -> tuple[int, dict]:
    return zhh_request(zhh_server_url, "GET", f"/log/{job_id}?lines={lines}")


LAUNCH_DIR_PATTERN = re.compile(r"/kmh-nfs-ssd-us-mount/staging/[^\s\"'`]+/launch_[^/\s\"'`]+")
LOG_DIR_ID_PATTERN = re.compile(r"^log(\d+)_")


def _extract_launch_dirs(text: str) -> list[Path]:
    matches = [m.group(0) for m in LAUNCH_DIR_PATTERN.finditer(str(text or ""))]
    if not matches:
        return []
    unique_in_order = list(dict.fromkeys(matches))
    return [Path(item) for item in unique_in_order]


def _find_latest_output_log(launch_dir: Path) -> Path | None:
    logs_root = launch_dir / "logs"
    if not logs_root.exists() or not logs_root.is_dir():
        return None

    best_id = -1
    best_path: Path | None = None
    for child in logs_root.iterdir():
        if not child.is_dir():
            continue
        m = LOG_DIR_ID_PATTERN.match(child.name)
        if not m:
            continue
        output_log = child / "output.log"
        if not output_log.exists() or not output_log.is_file():
            continue
        try:
            log_id = int(m.group(1))
        except Exception:
            continue
        if log_id > best_id:
            best_id = log_id
            best_path = output_log

    return best_path


def _read_text_file(path: Path, max_chars: int = 120_000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def fetch_task_reference_payload(zhh_server_url: str, job_id: str, lines: int = 400) -> tuple[int, dict]:
    status_code, payload = fetch_task_log_payload(zhh_server_url, job_id, lines=lines)
    if status_code != 200 or not isinstance(payload, dict):
        return status_code, payload

    base_stdout = str(payload.get("log", ""))
    launch_dirs = _extract_launch_dirs(base_stdout)

    selected_output = ""
    selected_source = ""
    for launch_dir in reversed(launch_dirs):
        latest_output = _find_latest_output_log(launch_dir)
        if latest_output is None:
            continue
        content = _read_text_file(latest_output)
        if content.strip():
            selected_output = content
            selected_source = str(latest_output)
            break

    merged = dict(payload)
    if selected_output:
        merged["stdout"] = base_stdout
        merged["stdout_source"] = selected_source
        merged["full_log_path"] = selected_source
    else:
        merged["stdout"] = base_stdout
        merged["stdout_source"] = "zhh_log"

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
