"""Agent活动跟踪模块 - 跟踪和管理Agent活动状态"""
from __future__ import annotations

import threading
import uuid
from typing import Any

from core.utils import utc_now, _compact_text_line


# Agent活动状态存储
agent_activity_lock = threading.Lock()
agent_activity_by_conversation: dict[str, dict] = {}


def _new_activity_entry(text: str, kind: str = "info") -> dict:
    """创建新的活动条目"""
    return {
        "id": str(uuid.uuid4()),
        "text": str(text or "").strip(),
        "kind": str(kind or "info").strip() or "info",
        "created_at": utc_now(),
    }


def reset_agent_activity(conversation_id: str, seed_text: str | None = None) -> None:
    """重置Agent活动状态"""
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
    """添加Agent活动记录"""
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
    """标记Agent活动结束"""
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
    """清除Agent活动记录"""
    target_id = str(conversation_id or "").strip()
    if not target_id:
        return
    with agent_activity_lock:
        agent_activity_by_conversation.pop(target_id, None)


def get_agent_activity_payload(conversation_id: str, limit: int = 120) -> dict:
    """获取Agent活动负载"""
    target_id = str(conversation_id or "").strip()
    default = {"conversation_id": target_id, "running": False, "entries": []}
    if not target_id:
        return default
    with agent_activity_lock:
        payload = agent_activity_by_conversation.get(target_id)
        if not isinstance(payload, dict):
            return default
        entries = payload.get("entries")
        if not isinstance(entries, list):
            return default
        return {
            "conversation_id": target_id,
            "running": bool(payload.get("running")),
            "updated_at": payload.get("updated_at"),
            "entries": entries[-limit:] if limit > 0 else entries,
        }


def _brief_agent_tool_input(payload: dict) -> str:
    """简要描述Agent工具输入"""
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
    """格式化Agent事件为活动行"""
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
    return lines


def record_agent_event(conversation_id: str, event: dict) -> None:
    """记录Agent事件"""
    lines = _format_agent_event_lines(event)
    for text, kind in lines:
        append_agent_activity(conversation_id, text, kind)
