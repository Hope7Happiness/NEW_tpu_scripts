"""工具函数模块 - 通用工具函数"""
from __future__ import annotations

import re
import time
from pathlib import Path


def utc_now() -> float:
    """获取当前UTC时间戳"""
    return time.time()


def _compact_text_line(text: str, limit: int = 220) -> str:
    """压缩单行文本"""
    t = str(text or "").replace("\n", " ")
    if len(t) <= limit:
        return t
    return t[: limit - 1] + "…"


def _compact_error_text(text: str, limit: int = 1200) -> str:
    """压缩错误文本"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    compact = re.sub(r"\s+", " ", raw)
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _safe_positive_int(text: str | None, default: int) -> int:
    """安全解析正整数"""
    try:
        v = int(text or default)
        return max(0, v)
    except Exception:
        return default


def _tail_text_file(path: Path, lines: int = 500, max_chars: int = 120_000) -> str:
    """读取文件末尾N行"""
    if not path or not path.exists():
        return ""
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    if not raw:
        return ""
    if len(raw) > max_chars:
        raw = raw[-max_chars:]
    all_lines = raw.splitlines()
    if len(all_lines) <= lines:
        return raw
    return "\n".join(all_lines[-lines:])


def _tail_string_lines(text: str, lines: int = 400, max_chars: int = 120_000) -> str:
    """Return the last ``lines`` lines of a string (e.g. remote log body)."""
    raw = str(text or "")
    if not raw:
        return ""
    if len(raw) > max_chars:
        raw = raw[-max_chars:]
    all_lines = raw.splitlines()
    if len(all_lines) <= lines:
        return raw
    return "\n".join(all_lines[-lines:])


def _read_text_file(path: Path, max_chars: int = 2_000_000) -> str:
    """读取文本文件内容"""
    if not path or not path.exists():
        return ""
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    if len(raw) > max_chars:
        return raw[-max_chars:]
    return raw


def _sanitize_auto_dir_name(name: str) -> str:
    """清理自动生成的目录名"""
    s = str(name or "").strip()
    s = re.sub(r"[^\w\-]", "_", s)
    s = re.sub(r"_+", "_", s)
    return s.strip("_")


def _extract_wandb_url_from_text(text: str) -> str | None:
    """从文本中提取wandb URL"""
    from core.config import WANDB_URL_PATTERN
    found = WANDB_URL_PATTERN.search(str(text or ""))
    return found.group(0) if found else None


def _extract_wandb_url_from_file(path_text: str) -> str | None:
    """从文件中提取wandb URL"""
    p = Path(str(path_text or ""))
    if not p.exists():
        return None
    text = _read_text_file(p, max_chars=500_000)
    return _extract_wandb_url_from_text(text)


def normalize_new_dir_name(name: str) -> str:
    """规范化新目录名"""
    s = str(name or "").strip()
    s = s.replace("/", "_").replace("\\", "_")
    s = re.sub(r"[^a-zA-Z0-9_\-]", "_", s)
    s = re.sub(r"_+", "_", s)
    return s.strip("_")[:120]


def normalize_task_status(status: str | None) -> str:
    """规范化任务状态"""
    s = str(status or "").strip().lower()
    mapping = {
        "running": "running",
        "completed": "completed",
        "completed (wandb)": "completed",
        "completed (wandb error)": "completed",
        "failed": "failed",
        "cancelled": "cancelled",
        "unknown": "unknown",
    }
    return mapping.get(s, s or "unknown")


def is_running_like_task_status(status: str | None) -> bool:
    """检查是否为运行中状态"""
    s = str(status or "").strip().lower()
    return s in {"running", "pending", "queued"}


def is_terminal_task_status(status: str | None) -> bool:
    """检查是否为终止状态"""
    s = str(status or "").strip().lower()
    return s in {"completed", "failed", "cancelled"}


def is_failed_task_status(status: str | None) -> bool:
    """检查是否为失败状态"""
    s = str(status or "").strip().lower()
    return s == "failed"


def task_alert_kind_for_status(status: str | None) -> str:
    """根据任务状态返回警告类型"""
    s = str(status or "").strip().lower()
    if s in {"failed", "cancelled"}:
        return "error"
    if s in {"completed", "completed (wandb)", "completed (wandb error)"}:
        return "success"
    return "info"


def has_error_signature_in_log(log_text: str) -> bool:
    """检查日志中是否有错误特征"""
    text = str(log_text or "").lower()
    if not text.strip():
        return False

    strong_patterns = [
        "traceback (most recent call last)",
        "unhandled exception",
        "runtimeerror:",
        "fatal error",
        "exited with code",
        "cuda out of memory",
        "out of memory",
    ]
    if any(p in text for p in strong_patterns):
        return True

    # 保守匹配，避免将状态列表中的 "[KILLED]" / "error" 文案误判为任务失败。
    return False
