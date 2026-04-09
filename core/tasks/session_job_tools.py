"""Session-scoped job helpers for agent tools: run with config, list jobs, job detail.

RUN: copy a YAML into configs/remote_run_config.yml, call ZHH /run, persist job with nickname + run_config_source.
GLOBAL QUERY: list jobs with optional status filter (default: running-like only).
QUERY: one job's description, status, config path, log file path.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from core.conversation import get_conversation, update_conversation
from core.tasks import (
    apply_running_display_overrides,
    diagnose_completed_jobs_once,
    get_conversation_jobs,
    normalize_task_status,
    update_task_alert_state,
    is_running_like_task_status,
)
from core.tasks.operations import (
    persist_zhh_job_to_conversation,
    resolve_model_log_file_path,
    zhh_run_job,
)


REMOTE_RUN_CONFIG_RELPATH = Path("configs") / "remote_run_config.yml"


def _session_cwd(conv: dict) -> Path:
    raw = conv.get("cwd")
    if not raw:
        raise ValueError("conversation has no cwd")
    path = Path(str(raw)).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"session cwd is not a directory: {path}")
    return path


def resolve_config_source_under_cwd(cwd: Path, config_path: str) -> tuple[Path, str]:
    """Resolve user config path to an absolute file under cwd. Returns (abs_path, relative_posix_from_cwd)."""
    raw = (config_path or "").strip()
    if not raw:
        raise ValueError("config_path is required")
    p = Path(raw)
    cwd_r = cwd.resolve()
    full = p.resolve() if p.is_absolute() else (cwd_r / p).resolve()
    try:
        rel = full.relative_to(cwd_r)
    except ValueError as e:
        raise ValueError("config_path must be inside the session working directory") from e
    if not full.is_file():
        raise ValueError(f"config file not found: {rel.as_posix()}")
    return full, rel.as_posix()


def copy_config_to_remote_run_slot(cwd: Path, source_file: Path) -> None:
    """Copy source YAML to <cwd>/configs/remote_run_config.yml (overwrites)."""
    dest = cwd.resolve() / REMOTE_RUN_CONFIG_RELPATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_file, dest)


def parse_status_filter_query(status_param: str | None) -> list[str] | None:
    """Interpret ?status= for global query.

    None or '' -> None (caller uses running-like default).
    'all' -> [] (empty list: no status filter).
    'running,failed' -> explicit allowed normalized statuses.
    """
    if status_param is None:
        return None
    s = str(status_param).strip()
    if not s:
        return None
    if s.lower() == "all":
        return []
    parts = [normalize_task_status(x) for x in s.split(",") if str(x).strip()]
    return parts or None


def run_job_with_config_path(
    conversation_id: str,
    config_path: str,
    description: str,
    *,
    nickname: str | None = None,
) -> dict:
    """RUN tool: copy config, zhh /run, persist job + nickname. Returns {\"ok\": true} or {\"ok\": false, \"error\": str}."""
    desc = str(description or "").strip()
    if not desc:
        return {"ok": False, "error": "description is required"}
    if len(desc) > 80:
        return {"ok": False, "error": "description too long (max 80 chars)"}
    nick = str(nickname or "").strip()
    if nick and len(nick) > 80:
        return {"ok": False, "error": "nickname too long (max 80 chars)"}

    conv = get_conversation(conversation_id)
    if not conv:
        return {"ok": False, "error": "conversation not found"}

    try:
        cwd = _session_cwd(conv)
        src, rel_str = resolve_config_source_under_cwd(cwd, config_path)
        copy_config_to_remote_run_slot(cwd, src)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except OSError as e:
        return {"ok": False, "error": f"failed to copy config: {e}"}

    result = zhh_run_job(args="", cwd=str(cwd))
    if not result.get("ok"):
        err = result.get("error", "/run failed")
        return {"ok": False, "error": str(err), "detail": result.get("payload")}

    job_id = result.get("job_id")
    if not job_id:
        return {"ok": False, "error": "run succeeded but job_id missing", "detail": result.get("payload")}
    run_data = result.get("payload", {}) or {}
    persist_zhh_job_to_conversation(
        conversation_id,
        job_id,
        run_data,
        nickname=nick or desc,
        run_config_source=rel_str,
        auto_run_by_agent=True,
    )
    return {"ok": True, "job_id": str(job_id)}


def global_query_session_jobs(conversation_id: str, status_param: str | None = None) -> dict:
    """GLOBAL QUERY: jobs with description + config path; optional status filter (default running-like only)."""
    conv = get_conversation(conversation_id)
    if not conv:
        return {"ok": False, "error": "conversation not found"}

    try:
        jobs = get_conversation_jobs(conv)
        conv, jobs = diagnose_completed_jobs_once(conversation_id, conv, jobs)
        conv, jobs = update_task_alert_state(conversation_id, conv, jobs)
        jobs = apply_running_display_overrides(jobs)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    parsed = parse_status_filter_query(status_param)
    if parsed is None:
        jobs = [j for j in jobs if isinstance(j, dict) and is_running_like_task_status(j.get("status"))]
    elif parsed:
        allowed = set(parsed)
        jobs = [j for j in jobs if isinstance(j, dict) and normalize_task_status(j.get("status")) in allowed]

    task_meta = conv.get("task_meta", {}) or {}
    rows = []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        jid = str(j.get("job_id") or "").strip()
        if not jid:
            continue
        meta = task_meta.get(jid, {}) if isinstance(task_meta, dict) else {}
        nickname = str(meta.get("nickname") or "").strip() if isinstance(meta, dict) else ""
        cfg = str(meta.get("run_config_source") or "").strip() if isinstance(meta, dict) else ""
        rows.append({
            "job_id": jid,
            "description": nickname,
            "status": normalize_task_status(j.get("status")),
            "config_path": cfg,
        })

    return {"ok": True, "jobs": rows, "count": len(rows)}


def format_session_jobs_user_message_content(results: list[tuple[str, dict]]) -> str:
    """Markdown body for an injected user message (visible to the model on the next turn)."""
    lines: list[str] = ["[Server · session job results]\n"]
    for op, payload in results:
        o = str(op or "").strip().lower()
        lines.append(f"## {o}\n")
        if not isinstance(payload, dict):
            lines.append("(invalid payload)\n\n")
            continue
        if not payload.get("ok"):
            lines.append(f"**Error:** {payload.get('error', 'unknown')}\n\n")
            continue
        if o == "run":
            lines.append(f"- **job_id:** `{payload.get('job_id', '')}`\n\n")
        elif o == "list":
            n = int(payload.get("count") or len(payload.get("jobs") or []))
            lines.append(f"- **count:** {n}\n")
            for j in payload.get("jobs") or []:
                if not isinstance(j, dict):
                    continue
                lines.append(
                    f"  - `{j.get('job_id', '')}` | {j.get('status', '')} | "
                    f"{j.get('description', '')} | {j.get('config_path', '')}\n"
                )
            lines.append("\n")
        elif o == "query":
            logf = str(payload.get("log_file") or "").strip()
            log_line = (
                f"- **log_file:** `{logf}`\n"
                if logf
                else "- **log_file:** no log found (upstream stdout is not pasted here; use the UI log viewer)\n"
            )
            lines.append(
                f"- **job_id:** `{payload.get('job_id', '')}`\n"
                f"- **status:** {payload.get('status', '')}\n"
                f"- **description:** {payload.get('description', '')}\n"
                f"- **config_path:** `{payload.get('config_path', '')}`\n"
                f"{log_line}\n"
            )
        else:
            lines.append(f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```\n\n")
    return "".join(lines)


def execute_session_job_actions(
    conversation_id: str,
    session_actions: list,
) -> tuple[list[tuple[str, dict]], str | None]:
    """Run session_job ops from parsed actions. Returns (list of (op, payload), job_id if run ok)."""
    tuples: list[tuple[str, dict]] = []
    session_job_id: str | None = None
    for act in session_actions:
        if not isinstance(act, dict):
            continue
        op = act.get("op")
        if op == "run":
            nick = str(act.get("nickname") or "").strip() or None
            sj = run_job_with_config_path(
                conversation_id,
                str(act.get("config_path") or ""),
                str(act.get("description") or ""),
                nickname=nick,
            )
            if sj.get("ok"):
                session_job_id = str(sj.get("job_id") or "") or session_job_id
            tuples.append(("run", sj))
        elif op == "list":
            st = act.get("status")
            st_param = None if st is None else str(st).strip() or None
            sj = global_query_session_jobs(conversation_id, st_param)
            tuples.append(("list", sj))
        elif op == "query":
            sj = query_session_job(conversation_id, str(act.get("job_id") or ""))
            tuples.append(("query", sj))
    return tuples, session_job_id


def format_session_job_system_content(op: str, payload: dict) -> str:
    """One-line-ish summary for message content (memory, logs); UI uses payload for detail."""
    o = str(op or "").strip().lower()
    if not isinstance(payload, dict):
        return "[Session jobs] Invalid result."
    if not payload.get("ok"):
        err = str(payload.get("error") or "unknown")
        return f"[Session jobs · {o}] failed: {err}"
    if o == "run":
        return f"[Session jobs · run] started job_id={payload.get('job_id', '')}"
    if o == "list":
        jobs = payload.get("jobs") or []
        n = int(payload.get("count") or len(jobs))
        if n == 0:
            return "[Session jobs · list] 0 jobs."
        bits = []
        for j in jobs[:10]:
            if not isinstance(j, dict):
                continue
            jid = str(j.get("job_id") or "")[:16]
            st = str(j.get("status") or "")
            bits.append(f"{jid}:{st}")
        tail = f" (+{n - len(bits)} more)" if n > len(bits) else ""
        return f"[Session jobs · list] {n} jobs — " + ", ".join(bits) + tail
    if o == "query":
        jid = str(payload.get("job_id") or "")
        st = str(payload.get("status") or "")
        cfg = str(payload.get("config_path") or "")
        logf = str(payload.get("log_file") or "").strip()
        log_disp = logf if logf else "no log found"
        return f"[Session jobs · query] job_id={jid} status={st} config={cfg} log={log_disp}"
    return f"[Session jobs · {o}] ok"


def query_session_job(conversation_id: str, job_id: str) -> dict:
    """QUERY: one job — description, status, config_path, log_file."""
    jid = str(job_id or "").strip()
    if not jid:
        return {"ok": False, "error": "job_id is required"}

    conv = get_conversation(conversation_id)
    if not conv:
        return {"ok": False, "error": "conversation not found"}

    job_ids = conv.get("job_ids", []) or []
    if jid not in job_ids:
        return {"ok": False, "error": "job does not belong to this conversation"}

    try:
        jobs = get_conversation_jobs(conv)
        conv, jobs = diagnose_completed_jobs_once(conversation_id, conv, jobs)
        conv, jobs = update_task_alert_state(conversation_id, conv, jobs)
        jobs = apply_running_display_overrides(jobs)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    status = "unknown"
    for j in jobs:
        if isinstance(j, dict) and str(j.get("job_id") or "").strip() == jid:
            status = normalize_task_status(j.get("status"))
            break

    task_meta = conv.get("task_meta", {}) or {}
    entry = task_meta.get(jid, {}) if isinstance(task_meta, dict) else {}
    nickname = str(entry.get("nickname") or "").strip() if isinstance(entry, dict) else ""
    cfg = str(entry.get("run_config_source") or "").strip() if isinstance(entry, dict) else ""

    log_file = resolve_model_log_file_path(conv, jid)

    return {
        "ok": True,
        "job_id": jid,
        "description": nickname,
        "status": status,
        "config_path": cfg,
        "log_file": log_file,
    }


def set_job_nickname(conversation_id: str, job_id: str, nickname: str) -> dict:
    """Shared nickname update (same rules as HTTP /tasks/.../nickname)."""
    from core.utils import utc_now

    jid = str(job_id or "").strip()
    nick = str(nickname or "").strip()
    if not jid:
        return {"ok": False, "error": "job_id is required"}
    if len(nick) > 80:
        return {"ok": False, "error": "nickname too long (max 80 chars)"}

    conv = get_conversation(conversation_id)
    if not conv:
        return {"ok": False, "error": "conversation not found"}
    job_ids = conv.get("job_ids", []) or []
    if jid not in job_ids:
        return {"ok": False, "error": "job does not belong to this conversation"}

    def set_nickname(c: dict):
        task_meta = c.setdefault("task_meta", {})
        entry = task_meta.get(jid, {})
        if not isinstance(entry, dict):
            entry = {}
        else:
            entry = dict(entry)
        if nick:
            entry["nickname"] = nick
            entry["updated_at"] = utc_now()
        else:
            entry.pop("nickname", None)
            entry["updated_at"] = utc_now()
        task_meta[jid] = entry

    update_conversation(conversation_id, set_nickname)
    return {"ok": True, "nickname": nick}
