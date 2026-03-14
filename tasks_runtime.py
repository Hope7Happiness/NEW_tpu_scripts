from __future__ import annotations

import json
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
    if not job_ids:
        return []

    status_code, status_data = zhh_request(zhh_server_url, "GET", "/status")
    if status_code != 200:
        raise RuntimeError(status_data.get("error", f"status code {status_code}"))

    jobs = status_data.get("jobs", [])
    by_id = {job.get("job_id"): job for job in jobs if isinstance(job, dict) and job.get("job_id")}

    ordered = []
    for job_id in reversed(job_ids):
        nickname = ""
        if isinstance(task_meta.get(job_id), dict):
            nickname = str(task_meta[job_id].get("nickname") or "").strip()
        if job_id in by_id:
            item = dict(by_id[job_id])
            item["nickname"] = nickname
            ordered.append(item)
        else:
            ordered.append({"job_id": job_id, "status": "unknown", "missing": True, "nickname": nickname})
    return ordered


def fetch_task_log_payload(zhh_server_url: str, job_id: str, lines: int = 400) -> tuple[int, dict]:
    return zhh_request(zhh_server_url, "GET", f"/log/{job_id}?lines={lines}")


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
