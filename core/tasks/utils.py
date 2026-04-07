"""任务工具模块 - 任务状态相关工具函数"""
from __future__ import annotations


def normalize_task_status(status: str | None) -> str:
    """规范化任务状态"""
    s = str(status or "").strip().lower()
    mapping = {
        "running": "running",
        "starting": "starting", 
        "queued": "queued",
        "pending": "pending",
        "completed": "completed",
        "failed": "failed",
        "error": "error",
        "canceled": "canceled",
        "cancelled": "canceled",
        "unknown": "unknown",
    }
    return mapping.get(s, s or "unknown")


def is_running_like_task_status(status: str | None) -> bool:
    """检查是否为运行中状态"""
    s = normalize_task_status(status)
    return s in {"running", "starting", "queued", "pending"}


def is_terminal_task_status(status: str | None) -> bool:
    """检查是否为终止状态"""
    s = normalize_task_status(status)
    return s in {"completed", "failed", "error", "canceled"}


def is_failed_task_status(status: str | None) -> bool:
    """检查是否为失败状态"""
    s = normalize_task_status(status)
    return s in {"failed", "error"}


def task_alert_kind_for_status(status: str | None) -> str:
    """根据任务状态返回警告类型"""
    s = normalize_task_status(status)
    if s in {"failed", "error", "canceled"}:
        return "failed"
    if s == "completed":
        return "done"
    return "info"
