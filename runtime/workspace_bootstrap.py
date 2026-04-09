"""One-shot Claude Code session when a workspace conversation is first created."""

from __future__ import annotations

from core.conversation import resolve_session_effort, update_conversation
from core.conversation.store import append_message
from core.global_agent_model import get_global_cli_model, get_global_llm_provider
from runtime.acp_runtime import acp_prompt_session
from runtime.agent_action_protocol import new_action_nonce
from runtime.agent_prompts import WORKSPACE_CREATE_GENERAL_PROMPT, append_server_nonce_footer


def should_skip_workspace_bootstrap() -> bool:
    """True when ``WORKSPACE_CREATE_GENERAL_PROMPT`` is empty—no CLI round is run."""
    return not str(WORKSPACE_CREATE_GENERAL_PROMPT or "").strip()


def workspace_bootstrap_prompt_text() -> str | None:
    """Return stdin body for bootstrap, or None if bootstrap should be skipped (empty prompt)."""
    raw = str(WORKSPACE_CREATE_GENERAL_PROMPT or "").strip()
    if not raw:
        return None
    session_nonce = new_action_nonce()
    return append_server_nonce_footer(raw, session_nonce=session_nonce)


def bootstrap_workspace_session(conversation_id: str, record: dict, *, agent_path: str) -> tuple[bool, str | None, bool]:
    """Run one ``acp_prompt_session`` so ``cursor_session_id`` exists before the first user message.

    Returns ``(ok, error_message, skipped)``. ``skipped`` is True when the general prompt is empty.
    On success, updates the conversation and appends bootstrap user/assistant messages.
    """
    cid = str(conversation_id or "").strip()
    text = workspace_bootstrap_prompt_text()
    if text is None:
        return True, None, True

    cwd = str(record.get("cwd") or "")
    mode = str(record.get("mode") or "agent").strip().lower() or "agent"
    session_effort = resolve_session_effort(record.get("llm_effort"))
    session_model = get_global_cli_model()

    try:
        result = acp_prompt_session(
            agent_path=agent_path,
            cwd=cwd,
            mode=mode,
            text=text,
            cursor_session_id=None,
            preferred_model=session_model,
            effort=session_effort,
            llm_provider=get_global_llm_provider(),
        )
    except Exception as exc:
        return False, str(exc), False

    model_used = str(result.get("model") or "").strip()
    effort_used = resolve_session_effort(result.get("effort") or session_effort)
    context_tokens = result.get("context_tokens")
    context_window = result.get("context_window")

    def apply_result(c: dict):
        if not c.get("cursor_session_id"):
            c["cursor_session_id"] = result["cursor_session_id"]
        c["llm_model"] = session_model
        c["llm_effort"] = resolve_session_effort(c.get("llm_effort") or session_effort)
        c["current_model"] = model_used or session_model
        c["current_effort"] = effort_used
        if isinstance(context_tokens, int) and context_tokens >= 0:
            c["current_context_tokens"] = context_tokens
        if isinstance(context_window, int) and context_window > 0:
            c["current_context_window"] = context_window

    update_conversation(cid, apply_result)

    assistant_raw = result.get("text") or f"[No text returned; stopReason={result.get('stop_reason')}]"
    append_message(
        cid,
        "user",
        text,
        {"workspace_bootstrap": True},
    )
    append_message(
        cid,
        "assistant",
        str(assistant_raw).strip(),
        {"workspace_bootstrap": True},
    )

    return True, None, False
