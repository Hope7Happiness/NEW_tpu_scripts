"""配置管理模块 - 统一管理应用配置"""
from __future__ import annotations

import json
import os
import re
import getpass
from pathlib import Path


APP_ROOT = Path(__file__).parent.parent.absolute()
CONFIG_PATH = APP_ROOT / "config.json"

WANDB_URL_PATTERN = re.compile(r"https?://(?:[A-Za-z0-9-]+\.)*wandb\.(?:ai|me)/[^\s\"'<>())]+")
COMPLETION_DIAGNOSIS_RULE_VERSION = 2

WECODE_USER = str(
    os.environ.get("WECODE_USER")
    or os.environ.get("CURCHAT_USER")
    or os.environ.get("WHO")
    or getpass.getuser()
).strip()
os.environ.setdefault("WECODE_USER", WECODE_USER)
os.environ.setdefault("CURCHAT_USER", WECODE_USER)
CURCHAT_USER = WECODE_USER


def utc_now() -> float:
    """获取当前UTC时间戳"""
    import time
    return time.time()


def _default_user_code_root() -> Path:
    """获取默认用户代码根目录"""
    candidate = Path(f"/kmh-nfs-ssd-us-mount/code/{WECODE_USER}").expanduser()
    if candidate.exists() and candidate.is_dir():
        return candidate.resolve()
    return APP_ROOT.parent


def load_ui_config() -> dict:
    """加载UI配置，支持从config.json读取"""
    default_code_root = _default_user_code_root()
    defaults = {
        "host": "0.0.0.0",
        "port": 7860,
        "workdir_root": str(default_code_root),
        "default_cwd": str(default_code_root),
        "agent_path": "claude",
        "store_file": "cursor_sessions.json",
        "task_server_url": "http://localhost:8080",
    }

    if not CONFIG_PATH.exists():
        return defaults

    try:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return defaults

    if not isinstance(payload, dict):
        return defaults

    ui = payload.get("ui_server")
    if not isinstance(ui, dict):
        return defaults

    merged = dict(defaults)
    merged.update({k: v for k, v in ui.items() if v is not None})
    return merged


# 全局配置实例
UI_CONFIG = load_ui_config()


def config_path_value(value: str | Path, fallback: Path) -> Path:
    """解析配置路径值"""
    raw = Path(str(value or fallback)).expanduser()
    if not raw.is_absolute():
        raw = (APP_ROOT / raw).resolve()
    else:
        raw = raw.resolve()
    return raw


# 服务配置
DEFAULT_HOST = str(UI_CONFIG.get("host") or "0.0.0.0")
DEFAULT_PORT = int(
    os.environ.get("CURSOR_SERVER_PORT")
    or os.environ.get("WECODE_UI_PORT")
    or os.environ.get("WECODE_PORT")
    or os.environ.get("CURCHAT_PORT")
    or str(UI_CONFIG.get("port") or 7860)
)
DEFAULT_AGENT = (
    os.environ.get("CLAUDE_CODE_PATH")
    or os.environ.get("CURSOR_AGENT_PATH")
    or str(UI_CONFIG.get("agent_path") or "claude")
)
store_file = Path(str(UI_CONFIG.get("store_file") or "cursor_sessions.json"))
STORE_PATH = store_file if store_file.is_absolute() else (APP_ROOT / store_file)
DEFAULT_CWD = str(config_path_value(UI_CONFIG.get("default_cwd") or APP_ROOT, APP_ROOT))
WORKDIR_ROOT = config_path_value(UI_CONFIG.get("workdir_root") or APP_ROOT.parent, APP_ROOT.parent)
ZHH_SERVER_URL = str(
    os.environ.get("WECODE_TASK_SERVER_URL")
    or os.environ.get("CURCHAT_TASK_SERVER_URL")
    or os.environ.get("ZHH_SERVER_URL")
    or UI_CONFIG.get("task_server_url")
    or "http://localhost:8080"
).strip()
UI_TEMPLATE_PATH = APP_ROOT / "ui" / "index.html"

# 会话默认设置
ALLOWED_SESSION_MODELS = {"opus", "sonnet", "haiku", "composer-2", "composer-2-fast"}
_configured_default_model = str(
    os.environ.get("WECODE_DEFAULT_MODEL")
    or os.environ.get("CURCHAT_DEFAULT_MODEL")
    or "composer-2-fast"
).strip().lower()
SESSION_DEFAULT_MODEL = _configured_default_model if _configured_default_model in ALLOWED_SESSION_MODELS else "composer-2-fast"
SESSION_DEFAULT_EFFORT = "high"
ALLOWED_SESSION_EFFORTS = {"low", "medium", "high", "max"}

# 自动修复调度器配置
try:
    AUTO_FIX_SCHEDULER_INTERVAL_SECONDS = int(os.environ.get("AUTO_FIX_SCHEDULER_INTERVAL_SECONDS", "10"))
except Exception:
    AUTO_FIX_SCHEDULER_INTERVAL_SECONDS = 10
