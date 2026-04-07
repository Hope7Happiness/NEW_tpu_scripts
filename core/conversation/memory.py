"""对话记忆模块 - 管理对话历史和记忆"""
from __future__ import annotations

import re
from core.utils import _compact_text_line
from core.config import ALLOWED_SESSION_MODELS, ALLOWED_SESSION_EFFORTS, SESSION_DEFAULT_MODEL, SESSION_DEFAULT_EFFORT


def build_conversation_memory_summary(conv: dict, max_items: int = 32, max_chars: int = 6000) -> str:
    """构建对话记忆摘要"""
    messages = conv.get("messages", [])
    if not messages:
        return ""
    
    lines: list[str] = []
    total_chars = 0
    
    for msg in messages[-max_items:]:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").strip()
        content = str(msg.get("content") or "").strip()
        if not content:
            continue
        
        compact = _compact_text_line(content, limit=400)
        line = f"[{role}] {compact}"
        
        if total_chars + len(line) > max_chars:
            break
        
        lines.append(line)
        total_chars += len(line) + 1
    
    return "\n".join(lines)


def build_prompt_with_memory(conv: dict, user_text: str) -> str:
    """构建带记忆的提示"""
    summary = build_conversation_memory_summary(conv)
    
    if not summary:
        return user_text
    
    parts: list[str] = [
        "[Conversation Context]",
        summary,
        "",
        "[Current Request]",
        user_text,
    ]
    
    return "\n".join(parts)


def resolve_session_model(raw_model: str | None) -> str:
    """解析会话模型"""
    model = str(raw_model or "").strip().lower()
    if model in ALLOWED_SESSION_MODELS:
        return model
    return SESSION_DEFAULT_MODEL


def resolve_session_effort(raw_effort: str | None) -> str:
    """解析会话effort级别"""
    effort = str(raw_effort or "").strip().lower()
    if effort in ALLOWED_SESSION_EFFORTS:
        return effort
    return SESSION_DEFAULT_EFFORT


def parse_session_setting_command(text: str) -> tuple[str, str] | tuple[None, None]:
    """解析会话设置命令"""
    s = str(text or "").strip()
    if not s.startswith("/"):
        return None, None
    
    parts = s.split(None, 1)
    if len(parts) < 2:
        return None, None
    
    cmd = parts[0].lower()
    value = parts[1].strip().lower()
    
    if cmd == "/model":
        if value in ALLOWED_SESSION_MODELS:
            return "model", value
    elif cmd == "/effort":
        if value in ALLOWED_SESSION_EFFORTS:
            return "effort", value
    
    return None, None
