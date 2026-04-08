"""Global agent model (Anyclaude / Claude Code CLI) — one choice for all sessions."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from core.config import APP_ROOT

# IDs and --model / provider values aligned with Anyclaude `src/utils/model/modelOptions.ts`
# and `src/services/llm/providerResolver.ts` (Codex 5.3 needs explicit provider for OpenAI vs Copilot).
GLOBAL_AGENT_MODEL_OPTIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "composer-2-cursor",
        "label": "Composer 2 (Cursor)",
        "cli_model": "composer-2",
        "llm_provider": "cursor",
    },
    {
        "id": "codex-53-openai",
        "label": "Codex 5.3 (OpenAI)",
        "cli_model": "gpt-5.3-codex",
        "llm_provider": "openai",
    },
    {
        "id": "codex-53-copilot",
        "label": "Codex 5.3 (Copilot)",
        "cli_model": "gpt-5.3-codex",
        "llm_provider": "github-copilot",
    },
    {
        "id": "sonnet-46-cursor",
        "label": "Claude Sonnet 4.6 (Cursor)",
        "cli_model": "cursor/claude-4.6-sonnet-medium",
        "llm_provider": "cursor",
    },
    {
        "id": "sonnet-46-anthropic",
        "label": "Claude Sonnet 4.6 (Anthropic)",
        "cli_model": "claude-sonnet-4-6",
        "llm_provider": "anthropic",
    },
    {
        "id": "opus-anthropic",
        "label": "Claude Opus (Anthropic)",
        "cli_model": "opus",
        "llm_provider": "anthropic",
    },
)

_DEFAULT_SELECTION_ID = "composer-2-cursor"
_GLOBAL_AGENT_MODEL_PATH = APP_ROOT / "data" / "global_agent_model.json"
_LOCK = threading.Lock()

_OPTION_BY_ID = {str(o["id"]): o for o in GLOBAL_AGENT_MODEL_OPTIONS}


def list_global_agent_model_options_public() -> list[dict[str, str]]:
    """id + label for UI."""
    return [{"id": str(o["id"]), "label": str(o["label"])} for o in GLOBAL_AGENT_MODEL_OPTIONS]


def _default_option() -> dict[str, Any]:
    return _OPTION_BY_ID.get(_DEFAULT_SELECTION_ID) or GLOBAL_AGENT_MODEL_OPTIONS[0]


def get_option_by_id(selection_id: str | None) -> dict[str, Any] | None:
    if not selection_id:
        return None
    return _OPTION_BY_ID.get(str(selection_id).strip())


def load_selection_id() -> str:
    with _LOCK:
        if not _GLOBAL_AGENT_MODEL_PATH.exists():
            return _DEFAULT_SELECTION_ID
        try:
            payload = json.loads(_GLOBAL_AGENT_MODEL_PATH.read_text(encoding="utf-8"))
        except Exception:
            return _DEFAULT_SELECTION_ID
        if not isinstance(payload, dict):
            return _DEFAULT_SELECTION_ID
        raw = str(payload.get("selection_id") or payload.get("id") or "").strip()
        if raw and raw in _OPTION_BY_ID:
            return raw
        return _DEFAULT_SELECTION_ID


def save_selection_id(selection_id: str) -> dict[str, Any]:
    sid = str(selection_id or "").strip()
    opt = get_option_by_id(sid)
    if not opt:
        raise ValueError("invalid global model id")
    _GLOBAL_AGENT_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"selection_id": sid, "cli_model": opt["cli_model"], "llm_provider": opt["llm_provider"]}
    with _LOCK:
        _GLOBAL_AGENT_MODEL_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return opt


def get_active_option() -> dict[str, Any]:
    return get_option_by_id(load_selection_id()) or _default_option()


def get_global_cli_model() -> str:
    return str(get_active_option().get("cli_model") or "").strip()


def get_global_llm_provider() -> str:
    """Return provider string for CLAUDE_CODE_LLM_PROVIDER (lowercase)."""
    return str(get_active_option().get("llm_provider") or "").strip().lower()


def get_global_model_policy_fields() -> dict[str, Any]:
    opt = get_active_option()
    return {
        "global_selection_id": str(opt.get("id") or ""),
        "global_model_label": str(opt.get("label") or ""),
        "global_cli_model": str(opt.get("cli_model") or ""),
        "global_llm_provider": str(opt.get("llm_provider") or ""),
    }
