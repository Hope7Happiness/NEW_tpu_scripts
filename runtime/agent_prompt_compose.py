"""Compose the stdin payload for one Claude Code CLI turn (acp_prompt_session).

Context stays in the Claude Code session (``--resume`` once ``cursor_session_id`` exists); we do not
inject a local conversation summary. Adds task-ref lines (if any) and the Chinese nonce footer.

Run / session_job / auto-fix rules live in ``.claude/skills/wecode-server`` (installed before CLI).
"""
from __future__ import annotations

from runtime.agent_prompts import append_server_nonce_footer
from runtime.tasks_runtime import build_prompt_with_task_refs


def compose_cli_turn_prompt(
    core_request: str,
    session_nonce: str,
    refs_payload: list,
) -> str:
    inner = str(core_request or "")
    base = build_prompt_with_task_refs(inner, refs_payload)
    return append_server_nonce_footer(base, session_nonce=session_nonce)
