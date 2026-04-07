"""对话存储模块 - 管理对话的CRUD操作"""
from __future__ import annotations

import json
import os
import uuid
import threading
from pathlib import Path

from core.config import STORE_PATH
from core.utils import utc_now
from core.workdir import workdir_base


# 全局锁
store_lock = threading.Lock()
conversation_locks: dict[str, threading.Lock] = {}


def _load_store() -> dict:
    """加载存储数据"""
    if not STORE_PATH.exists():
        return {"conversations": {}}
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"conversations": {}}
        data.setdefault("conversations", {})
        return data
    except Exception:
        return {"conversations": {}}


def _save_store(data: dict) -> None:
    """保存存储数据"""
    tmp_path = STORE_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, STORE_PATH)


def get_conversation_lock(conversation_id: str) -> threading.Lock:
    """获取对话锁"""
    target_id = str(conversation_id or "").strip()
    if target_id not in conversation_locks:
        conversation_locks[target_id] = threading.Lock()
    return conversation_locks[target_id]


def list_conversations() -> list[dict]:
    """列出所有对话摘要"""
    with store_lock:
        data = _load_store()
        items = list(data["conversations"].values())
        items.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
        return [conversation_summary(item) for item in items]


def get_conversation(conversation_id: str) -> dict | None:
    """获取对话详情"""
    with store_lock:
        data = _load_store()
        return data["conversations"].get(conversation_id)


def find_conversation_by_cwd(cwd: str) -> dict | None:
    """通过工作目录查找对话"""
    with store_lock:
        data = _load_store()
        for conv in data["conversations"].values():
            if conv.get("cwd") == cwd:
                return conv
        return None


def update_conversation(conversation_id: str, updater) -> dict:
    """更新对话"""
    with store_lock:
        data = _load_store()
        conv = data["conversations"].get(conversation_id)
        if conv is None:
            raise KeyError(conversation_id)
        updater(conv)
        conv["updated_at"] = utc_now()
        _save_store(data)
        return conv


def delete_conversation(conversation_id: str) -> dict | None:
    """删除对话"""
    with store_lock:
        data = _load_store()
        conv = data["conversations"].pop(conversation_id, None)
        if conv is None:
            return None
        conversation_locks.pop(conversation_id, None)
        _save_store(data)
        return conv


def create_conversation_record(
    title: str,
    cwd: str,
    mode: str,
    cursor_session_id: str | None,
    llm_model: str = "opus",
    llm_effort: str = "high",
) -> dict:
    """创建对话记录"""
    conversation_id = str(uuid.uuid4())
    now = utc_now()
    record = {
        "id": conversation_id,
        "title": title,
        "cwd": cwd,
        "mode": mode,
        "cursor_session_id": cursor_session_id,
        "status": "idle",
        "created_at": now,
        "updated_at": now,
        "messages": [],
        "job_ids": [],
        "llm_model": str(llm_model or "opus").strip() or "opus",
        "llm_effort": str(llm_effort or "high").strip() or "high",
        "current_model": str(llm_model or "opus").strip() or "opus",
        "current_effort": str(llm_effort or "high").strip() or "high",
    }
    with store_lock:
        data = _load_store()
        data["conversations"][conversation_id] = record
        _save_store(data)
    return record


def conversation_summary(conv: dict) -> dict:
    """生成对话摘要"""
    messages = conv.get("messages", [])
    job_ids = conv.get("job_ids", [])
    task_meta = conv.get("task_meta", {}) or {}
    
    task_unread_count = 0
    task_has_failed_unread = False
    if isinstance(task_meta, dict):
        for value in task_meta.values():
            if not isinstance(value, dict):
                continue
            if not value.get("unread"):
                continue
            task_unread_count += 1
            if str(value.get("alert_kind") or "").lower() == "failed":
                task_has_failed_unread = True
    
    last_message_obj = messages[-1] if messages and isinstance(messages[-1], dict) else {}
    last_message = str(last_message_obj.get("content") or "")
    last_message_role = str(last_message_obj.get("role") or "")
    last_message_created_at = last_message_obj.get("created_at")
    
    llm_model = str(conv.get("llm_model") or "opus").strip() or "opus"
    llm_effort = str(conv.get("llm_effort") or "high").strip() or "high"
    current_model = str(conv.get("current_model") or llm_model).strip() or llm_model
    current_effort = str(conv.get("current_effort") or llm_effort).strip() or llm_effort
    current_context_tokens = conv.get("current_context_tokens")
    current_context_window = conv.get("current_context_window")
    
    return {
        "id": conv["id"],
        "title": conv.get("title") or "Untitled",
        "workdir_base": workdir_base(conv.get("cwd", "")),
        "cwd": conv.get("cwd"),
        "mode": conv.get("mode", "agent"),
        "status": conv.get("status", "idle"),
        "cursor_session_id": conv.get("cursor_session_id"),
        "created_at": conv.get("created_at"),
        "updated_at": conv.get("updated_at"),
        "message_count": len(messages),
        "task_count": len(job_ids),
        "auto_iterating": False,
        "auto_iterate_round": 0,
        "task_unread_count": task_unread_count,
        "task_has_unread": task_unread_count > 0,
        "task_has_failed_unread": task_has_failed_unread,
        "last_message_preview": last_message[:120],
        "last_message_role": last_message_role,
        "last_message_created_at": last_message_created_at,
        "last_error": str(conv.get("last_error") or ""),
        "auto_iterate_last_error": "",
        "llm_model": llm_model,
        "llm_effort": llm_effort,
        "current_model": current_model,
        "current_effort": current_effort,
        "current_context_tokens": current_context_tokens,
        "current_context_window": current_context_window,
    }


def maybe_autoname(conversation_id: str) -> None:
    """尝试自动命名对话"""
    def updater(c: dict):
        if c.get("title") and c["title"] != "Untitled":
            return
        messages = c.get("messages", [])
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = str(msg.get("content") or "").strip()
                if content:
                    title = content.split("\n")[0][:40]
                    if len(title) < len(content):
                        title += "..."
                    c["title"] = title
                    break
    
    try:
        update_conversation(conversation_id, updater)
    except Exception:
        pass


def append_message(conversation_id: str, role: str, content: str, extra: dict | None = None) -> dict:
    """向对话添加消息"""
    from core.config import ALLOWED_SESSION_MODELS, ALLOWED_SESSION_EFFORTS
    
    def updater(c: dict):
        messages = c.get("messages")
        if not isinstance(messages, list):
            messages = []
        entry = {
            "role": role,
            "content": str(content or ""),
            "created_at": utc_now(),
        }
        if isinstance(extra, dict):
            entry.update(extra)
        
        # Handle model/effort settings from message
        content_lower = str(content or "").lower().strip()
        if content_lower.startswith("/model "):
            model = content[7:].strip().lower()
            if model in ALLOWED_SESSION_MODELS:
                c["current_model"] = model
                c["llm_model"] = model
        elif content_lower.startswith("/effort "):
            effort = content[8:].strip().lower()
            if effort in ALLOWED_SESSION_EFFORTS:
                c["current_effort"] = effort
                c["llm_effort"] = effort
        
        messages.append(entry)
        c["messages"] = messages
    
    return update_conversation(conversation_id, updater)
