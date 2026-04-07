"""任务操作模块 - 任务相关的具体操作"""
from __future__ import annotations

from pathlib import Path

from core.config import ZHH_SERVER_URL
from core.utils import _tail_text_file, _extract_wandb_url_from_text, _extract_wandb_url_from_file
from tasks_runtime import fetch_task_reference_payload, fetch_task_output_log_path, zhh_request
from core.utils import utc_now
from core.conversation import get_conversation, update_conversation


def mark_task_status(conversation_id: str, job_id: str, status: str) -> None:
    """标记任务状态"""
    def updater(c: dict):
        task_meta = c.get("task_meta")
        if not isinstance(task_meta, dict):
            task_meta = {}
        entry = task_meta.get(job_id, {})
        if not isinstance(entry, dict):
            entry = {}
        entry["manual_status"] = status
        entry["manual_status_at"] = utc_now()
        task_meta[job_id] = entry
        c["task_meta"] = task_meta
    
    update_conversation(conversation_id, updater)


def mark_task_error_forced(conversation_id: str, job_id: str) -> None:
    """强制标记任务为错误状态"""
    def updater(c: dict):
        task_meta = c.get("task_meta")
        if not isinstance(task_meta, dict):
            task_meta = {}
        entry = task_meta.get(job_id, {})
        if not isinstance(entry, dict):
            entry = {}
        entry["forced_error"] = True
        entry["forced_error_at"] = utc_now()
        entry["unread"] = True
        entry["alert_kind"] = "failed"
        task_meta[job_id] = entry
        c["task_meta"] = task_meta
    
    update_conversation(conversation_id, updater)


def clear_task_unread_alert(conversation_id: str, job_id: str) -> None:
    """清除任务未读警告"""
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


def clear_all_task_unread_alerts(conversation_id: str) -> None:
    """清除对话中所有任务的未读警告"""
    def clear_all(c: dict):
        task_meta = c.get("task_meta")
        if not isinstance(task_meta, dict):
            return
        for job_id, entry in task_meta.items():
            if isinstance(entry, dict):
                entry.pop("unread", None)
                entry.pop("alert_kind", None)
                entry["updated_at"] = utc_now()
    
    update_conversation(conversation_id, clear_all)


def resolve_task_wandb_url(conv: dict, job_id: str) -> tuple[str | None, str]:
    """解析任务的wandb URL"""
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
    """解析任务输出日志路径"""
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
    """获取本地任务日志"""
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
    """获取缓存的完整日志路径"""
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


def get_task_log_payload(conv: dict, job_id: str, lines: int = 500, prefer_pane: bool = False) -> dict | None:
    """获取任务日志负载"""
    cached = _cached_full_log_path(conv, job_id)
    if cached:
        text = _tail_text_file(Path(cached), lines=lines)
        if text.strip():
            return {
                "job_id": job_id,
                "lines": lines,
                "log": text,
                "source": "full_log",
                "log_path": cached,
            }
    
    status_code, payload = fetch_task_reference_payload(ZHH_SERVER_URL, job_id, lines=lines)
    if status_code == 200 and isinstance(payload, dict):
        full_log_path = str(payload.get("full_log_path") or "").strip()
        if full_log_path:
            path = Path(full_log_path)
            if path.exists() and path.is_file():
                text = _tail_text_file(path, lines=lines)
                if text.strip():
                    return {
                        "job_id": job_id,
                        "lines": lines,
                        "log": text,
                        "source": "full_log",
                        "log_path": full_log_path,
                    }
        
        log_text = str(payload.get("log") or payload.get("stdout") or "")
        if log_text.strip():
            return {
                "job_id": job_id,
                "lines": lines,
                "log": log_text,
                "source": "remote",
            }
    
    return _local_task_log_payload(conv, job_id, lines=lines, prefer_pane=prefer_pane)


def snapshot_task_log_before_cancel(conversation_id: str, job_id: str, *, lines: int = 2000) -> str | None:
    """在取消任务前快照日志"""
    conv = get_conversation(conversation_id)
    if not conv:
        return None
    
    payload = get_task_log_payload(conv, job_id, lines=lines, prefer_pane=True)
    if not isinstance(payload, dict):
        return None
    
    log_text = str(payload.get("log") or "")
    source_path = str(payload.get("log_path") or payload.get("source") or "")
    
    def updater(c: dict):
        task_meta = c.get("task_meta")
        if not isinstance(task_meta, dict):
            task_meta = {}
        entry = task_meta.get(job_id, {})
        if not isinstance(entry, dict):
            entry = {}
        entry["cancel_snapshot"] = log_text[:80000]
        entry["cancel_snapshot_at"] = utc_now()
        if source_path:
            entry["cancel_log_file"] = source_path
        task_meta[job_id] = entry
        c["task_meta"] = task_meta
    
    update_conversation(conversation_id, updater)
    return log_text[:80000] if log_text else None


def zhh_cancel_job(job_id: str) -> dict:
    """取消ZHH任务"""
    url = ZHH_SERVER_URL
    if not url:
        return {"ok": False, "error": "ZHH server URL not configured"}
    
    status_code, payload = zhh_request(url, "POST", f"/cancel/{job_id}")
    if status_code == 200 and isinstance(payload, dict):
        return {"ok": True, "payload": payload}
    return {"ok": False, "error": f"Failed to cancel job: {status_code}", "payload": payload}


def zhh_run_job(args: str = "", cwd: str = "", resume_log_path: str = "") -> dict:
    """运行ZHH任务"""
    url = ZHH_SERVER_URL
    if not url:
        return {"ok": False, "error": "ZHH server URL not configured"}
    
    body: dict = {"args": args, "cwd": cwd}
    if resume_log_path:
        body["log_path"] = resume_log_path
    
    status_code, payload = zhh_request(url, "POST", "/run", payload=body)
    if status_code == 200 and isinstance(payload, dict):
        return {"ok": True, "job_id": payload.get("job_id"), "payload": payload}
    return {"ok": False, "error": f"Failed to run job: {status_code}", "payload": payload}


def zhh_resume_job(log_path: str) -> dict:
    """恢复ZHH任务"""
    url = ZHH_SERVER_URL
    if not url:
        return {"ok": False, "error": "ZHH server URL not configured"}
    
    body = {"log_path": log_path}
    status_code, payload = zhh_request(url, "POST", "/resume", payload=body)
    if status_code == 200 and isinstance(payload, dict):
        return {"ok": True, "job_id": payload.get("job_id"), "payload": payload}
    return {"ok": False, "error": f"Failed to resume job: {status_code}", "payload": payload}
