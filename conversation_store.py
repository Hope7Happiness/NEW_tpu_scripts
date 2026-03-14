from __future__ import annotations

import json
import os
import uuid
from pathlib import Path


def default_store() -> dict:
    return {"conversations": {}}


def load_store(store_path: Path) -> dict:
    if not store_path.exists():
        return default_store()
    try:
        with open(store_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default_store()
        data.setdefault("conversations", {})
        return data
    except Exception:
        return default_store()


def save_store(store_path: Path, data: dict) -> None:
    tmp_path = store_path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, store_path)


def conversation_summary(conv: dict, workdir_base_fn) -> dict:
    messages = conv.get("messages", [])
    job_ids = conv.get("job_ids", [])
    last_message = messages[-1]["content"] if messages else ""
    return {
        "id": conv["id"],
        "title": conv.get("title") or "Untitled",
        "workdir_base": workdir_base_fn(conv.get("cwd", "")),
        "cwd": conv.get("cwd"),
        "mode": conv.get("mode", "agent"),
        "status": conv.get("status", "idle"),
        "cursor_session_id": conv.get("cursor_session_id"),
        "created_at": conv.get("created_at"),
        "updated_at": conv.get("updated_at"),
        "message_count": len(messages),
        "task_count": len(job_ids),
        "last_message_preview": last_message[:120],
    }


def list_conversations(store_path: Path, store_lock, summary_builder) -> list[dict]:
    with store_lock:
        data = load_store(store_path)
        items = list(data["conversations"].values())
    items.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
    return [summary_builder(item) for item in items]


def get_conversation(store_path: Path, store_lock, conversation_id: str) -> dict | None:
    with store_lock:
        data = load_store(store_path)
        return data["conversations"].get(conversation_id)


def find_conversation_by_cwd(store_path: Path, store_lock, cwd: str) -> dict | None:
    with store_lock:
        data = load_store(store_path)
        for conv in data["conversations"].values():
            if conv.get("cwd") == cwd:
                return conv
    return None


def update_conversation(store_path: Path, store_lock, conversation_id: str, updater, utc_now_fn) -> dict:
    with store_lock:
        data = load_store(store_path)
        conv = data["conversations"].get(conversation_id)
        if conv is None:
            raise KeyError(conversation_id)
        updater(conv)
        conv["updated_at"] = utc_now_fn()
        save_store(store_path, data)
        return conv


def delete_conversation(
    store_path: Path,
    store_lock,
    conversation_locks: dict,
    conversation_id: str,
) -> dict | None:
    with store_lock:
        data = load_store(store_path)
        conv = data["conversations"].pop(conversation_id, None)
        if conv is None:
            return None
        conversation_locks.pop(conversation_id, None)
        save_store(store_path, data)
        return conv


def create_conversation_record(
    store_path: Path,
    store_lock,
    title: str,
    cwd: str,
    mode: str,
    cursor_session_id: str | None,
    utc_now_fn,
) -> dict:
    conversation_id = str(uuid.uuid4())
    now = utc_now_fn()
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
    }
    with store_lock:
        data = load_store(store_path)
        data["conversations"][conversation_id] = record
        save_store(store_path, data)
    return record
