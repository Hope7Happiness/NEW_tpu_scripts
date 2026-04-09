from __future__ import annotations

import json
import re
import uuid
from typing import Any

from runtime.agent_prompts import auto_fix_trigger_text


SESSION_JOB_ACTION_PATTERN = re.compile(r"<session_job>(.*?)</session_job>", re.IGNORECASE | re.DOTALL)
GIVE_UP_FIX_TAG_PATTERN = re.compile(r"<give_up_fix>(.*?)</give_up_fix>", re.IGNORECASE | re.DOTALL)


def new_action_nonce() -> str:
    return uuid.uuid4().hex


def format_session_job_parse_errors_message(errors: list[str]) -> str:
    """User-visible system message when <session_job> tags failed to parse or validate."""
    if not errors:
        return ""
    lines = [
        "[Server · session_job parse error]",
        "One or more <session_job>…</session_job> blocks were present but not applied.",
        "Fix JSON (valid object), op, fields, and nonce matching this turn's Chinese footer, then retry.",
        "",
    ]
    lines.extend(f"- {e}" for e in errors)
    return "\n".join(lines)


def extract_session_job_actions(text: str, session_nonce: str) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Strip <session_job> tags; collect valid actions; **errors** lists each tag that did not apply."""
    raw = str(text or "")
    expected = str(session_nonce or "").strip()
    actions: list[dict[str, Any]] = []
    errors: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        payload = str(match.group(1) or "").strip()
        if not payload:
            errors.append("<session_job>: empty payload between tags (need JSON object).")
            return ""
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as e:
            errors.append(f"<session_job>: invalid JSON ({e.msg} at position {e.pos}).")
            return ""
        except Exception as e:
            errors.append(f"<session_job>: invalid JSON ({e}).")
            return ""
        if not isinstance(parsed, dict):
            errors.append("<session_job>: JSON must be a single object {{...}}, not an array or string.")
            return ""
        nonce = str(parsed.get("nonce") or "").strip()
        if nonce != expected:
            errors.append(
                "<session_job>: `nonce` must exactly match the `session_job` value in this message's Chinese footer "
                f"(expected footer nonce, got {nonce!r})."
            )
            return ""
        op = str(parsed.get("op") or "").strip().lower()
        if op == "run":
            config_path = str(parsed.get("config_path") or "").strip()
            description = str(parsed.get("description") or "").strip()
            if not config_path or not description:
                errors.append(
                    '<session_job> op "run": requires non-empty `config_path` and `description` (and optional `nickname`).'
                )
                return ""
            nick = str(parsed.get("nickname") or "").strip()
            item: dict[str, Any] = {"op": "run", "config_path": config_path, "description": description}
            if nick:
                item["nickname"] = nick
            actions.append(item)
        elif op in ("list", "global_query", "jobs"):
            status_val = parsed.get("status")
            st: str | None
            if status_val is None:
                st = None
            else:
                st = str(status_val).strip()
                if not st:
                    st = None
            actions.append({"op": "list", "status": st})
        elif op in ("query", "job"):
            job_id = str(parsed.get("job_id") or "").strip()
            if not job_id:
                errors.append('<session_job> op "query": requires non-empty `job_id`.')
                return ""
            actions.append({"op": "query", "job_id": job_id})
        else:
            if not op:
                errors.append('<session_job>: missing or empty `op` (use "run", "list", or "query").')
            else:
                errors.append(f'<session_job>: unsupported op {op!r} (use "run", "list", or "query").')
            return ""
        return ""

    cleaned = SESSION_JOB_ACTION_PATTERN.sub(_replace, raw).strip()
    return cleaned, actions, errors


def build_auto_fix_prompt(job_id: str, status: str, give_up_nonce: str) -> str:
    return auto_fix_trigger_text(job_id, status, give_up_nonce)


def extract_give_up_fix_action(text: str, job_id: str, give_up_nonce: str) -> tuple[str, str | None]:
    raw = str(text or "")
    normalized_job_id = str(job_id or "").strip()
    expected_nonce = str(give_up_nonce or "").strip()
    reason: str | None = None

    def _replace(match: re.Match[str]) -> str:
        nonlocal reason
        payload = str(match.group(1) or "").strip()
        if not payload:
            return ""

        parsed = None
        try:
            parsed = json.loads(payload)
        except Exception:
            parsed = None

        target_job_id = ""
        target_reason = ""
        nonce = ""
        if isinstance(parsed, dict):
            target_job_id = str(parsed.get("job_id") or "").strip()
            target_reason = str(parsed.get("reason") or "").strip()
            nonce = str(parsed.get("nonce") or "").strip()

        if not target_job_id:
            target_job_id = normalized_job_id
        if target_job_id == normalized_job_id and target_reason and nonce == expected_nonce:
            reason = target_reason
        return ""

    cleaned = GIVE_UP_FIX_TAG_PATTERN.sub(_replace, raw)
    cleaned = cleaned.strip()
    return cleaned, reason
