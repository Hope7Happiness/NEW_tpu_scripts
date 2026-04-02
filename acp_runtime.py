from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable


def _first_nonempty_env(keys: list[str], default: str = "") -> str:
    for key in keys:
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    return str(default or "").strip()


def _compact_error_detail(text: str, limit: int = 900) -> str:
    raw = str(text or "").strip()
    if not raw:
        return "unknown error"
    compact = re.sub(r"\s+", " ", raw)
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _is_usage_limit_error(message: str) -> bool:
    text = str(message or "").lower()
    if not text:
        return False
    signals = [
        "hit your usage limit",
        "usage limit",
        "spend limit",
        "rate limit",
        "switch to a different model",
    ]
    return any(sig in text for sig in signals)


USAGE_LIMIT_DATE_PATTERN = re.compile(r"ends on\s+(\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE)
MODEL_FALLBACK_STATE_PATH = Path(__file__).with_name("model_fallback_state.json")
_MODEL_STATE_LOCK = threading.Lock()


def _parse_limit_reset_date(message: str) -> date | None:
    text = str(message or "")
    match = USAGE_LIMIT_DATE_PATTERN.search(text)
    if not match:
        return None
    raw = match.group(1).strip()
    try:
        month_s, day_s, year_s = raw.split("/")
        return date(int(year_s), int(month_s), int(day_s))
    except Exception:
        return None


def _load_model_state() -> dict:
    if not MODEL_FALLBACK_STATE_PATH.exists():
        return {}
    try:
        payload = json.loads(MODEL_FALLBACK_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_model_state(state: dict) -> None:
    try:
        MODEL_FALLBACK_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _forced_auto_until_date() -> date | None:
    with _MODEL_STATE_LOCK:
        state = _load_model_state()
    raw = str(state.get("force_auto_until") or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except Exception:
        return None


def _is_force_auto_active() -> bool:
    until = _forced_auto_until_date()
    if until is None:
        return False
    return date.today() <= until


def _set_force_auto_until(until: date) -> None:
    with _MODEL_STATE_LOCK:
        state = _load_model_state()
        state["force_auto_until"] = until.strftime("%Y-%m-%d")
        state["updated_at"] = int(time.time())
        _save_model_state(state)


def note_usage_limit_error(message: str) -> bool:
    text = str(message or "")
    if not _is_usage_limit_error(text):
        return False
    reset_date = _parse_limit_reset_date(text)
    if reset_date is None:
        return False
    _set_force_auto_until(reset_date + timedelta(days=1))
    return True


def get_model_policy_status() -> dict:
    configured_model = _first_nonempty_env(["CLAUDE_CODE_MODEL", "CURSOR_CLI_MODEL"], default="opus") or "opus"
    until = _forced_auto_until_date()
    active = _is_force_auto_active()
    fallback_model = _limit_fallback_model()
    effective_model = fallback_model if active else configured_model
    days_remaining = 0
    force_until_text = ""
    if until is not None:
        force_until_text = until.strftime("%Y-%m-%d")
        days_remaining = max(0, (until - date.today()).days)
    return {
        "configured_model": configured_model,
        "effective_model": effective_model,
        "force_auto_active": active,
        "force_auto_until": force_until_text,
        "force_auto_days_remaining": days_remaining,
    }


def _fallback_models_from_env() -> list[str]:
    raw = _first_nonempty_env(["CLAUDE_CODE_FALLBACK_MODELS", "CURSOR_CLI_FALLBACK_MODELS"], default="sonnet,haiku")
    if not raw:
        return ["sonnet", "haiku"]
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return values or ["sonnet", "haiku"]


def _limit_fallback_model() -> str:
    preferred = _first_nonempty_env(
        ["CLAUDE_CODE_LIMIT_FALLBACK_MODEL", "CLAUDE_CODE_FORCE_FALLBACK_MODEL"],
        default="",
    )
    if preferred:
        return preferred
    fallbacks = _fallback_models_from_env()
    if fallbacks:
        return fallbacks[0]
    return "sonnet"


def _max_turns_value() -> int:
    raw = _first_nonempty_env(["CLAUDE_CODE_MAX_TURNS", "CURSOR_CLI_MAX_TURNS"], default="50")
    try:
        value = int(raw)
    except Exception:
        return 50
    return value if value > 0 else 50


class CLIPromptCanceler:
    def __init__(self):
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()

    def attach(self, proc: subprocess.Popen[str]) -> None:
        with self._lock:
            self._proc = proc

    def close(self) -> None:
        with self._lock:
            proc = self._proc
        if not proc:
            return
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


def _build_prompt_with_mode(text: str, mode: str) -> str:
    normalized_mode = str(mode or "agent").strip().lower()
    base = str(text or "")
    if normalized_mode == "ask":
        return "[Run mode: ask]\nRespond concisely and focus on direct answers.\n\n" + base
    if normalized_mode == "plan":
        return "[Run mode: plan]\nProvide a concrete implementation plan before coding.\n\n" + base
    return base


def _build_cli_command(
    agent_path: str,
    session_id: str,
    text: str,
    model_id: str | None,
    mode: str,
    force_allow: bool,
) -> list[str]:
    cmd = [agent_path]
    if force_allow:
        cmd.extend(["--permission-mode", "bypassPermissions"])
    if session_id:
        cmd.extend(["--resume", session_id])
    if model_id:
        cmd.extend(["--model", model_id])
    max_turns = _max_turns_value()
    prompt_text = _build_prompt_with_mode(text, mode)
    cmd.extend([
        "-p",
        prompt_text,
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-turns",
        str(max_turns),
    ])
    return cmd


def _run_cli_prompt(
    agent_path: str,
    cwd: str,
    session_id: str,
    text: str,
    timeout: float,
    cancel_event: threading.Event | None,
    model_id: str | None,
    mode: str,
    force_allow: bool,
    canceler: CLIPromptCanceler,
    on_progress_event: Callable[[dict], None] | None = None,
) -> dict:
    cmd = _build_cli_command(
        agent_path=agent_path,
        session_id=session_id,
        text=text,
        model_id=model_id,
        mode=mode,
        force_allow=force_allow,
    )

    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        bufsize=1,
    )
    canceler.attach(proc)

    started_at = time.monotonic()
    init_session_id = str(session_id or "").strip()
    init_model = str(model_id or "").strip()
    result_payload: dict | None = None
    raw_tail: list[str] = []

    while True:
        if timeout and timeout > 0 and (time.monotonic() - started_at) > timeout:
            canceler.close()
            raise RuntimeError("session/prompt timed out")
        if cancel_event is not None and cancel_event.is_set():
            canceler.close()
            raise InterruptedError("session/prompt canceled")

        line = proc.stdout.readline() if proc.stdout is not None else ""
        if not line:
            if proc.poll() is not None:
                break
            time.sleep(0.05)
            continue

        stripped = line.strip()
        if stripped:
            raw_tail.append(stripped)
            if len(raw_tail) > 200:
                raw_tail = raw_tail[-200:]

        try:
            event = json.loads(stripped)
        except Exception:
            continue

        if not isinstance(event, dict):
            continue

        if on_progress_event is not None:
            try:
                on_progress_event(event)
            except Exception:
                pass

        etype = str(event.get("type") or "").strip().lower()
        subtype = str(event.get("subtype") or "").strip().lower()
        if etype == "system" and subtype == "init":
            sid = str(event.get("session_id") or "").strip()
            if sid:
                init_session_id = sid
            model_name = str(event.get("model") or "").strip()
            if model_name:
                init_model = model_name
        elif etype == "result":
            result_payload = event

    stderr_text = proc.stderr.read() if proc.stderr is not None else ""
    return_code = proc.wait()

    if result_payload is None:
        if return_code != 0:
            detail = _compact_error_detail(stderr_text or "\n".join(raw_tail[-40:]) or "unknown error")
            raise RuntimeError(f"session/prompt failed ({return_code}): {detail}")
        detail = _compact_error_detail(stderr_text or "\n".join(raw_tail[-40:]) or "no result event")
        raise RuntimeError(f"session/prompt failed: {detail}")

    if bool(result_payload.get("is_error")):
        detail = _compact_error_detail(str(result_payload.get("result") or result_payload.get("error") or "unknown error"))
        raise RuntimeError(f"session/prompt failed (result error): {detail}")

    final_session_id = str(result_payload.get("session_id") or init_session_id or session_id or "").strip()
    final_model = str(init_model or model_id or "").strip() or "opus"
    stop_reason = str(result_payload.get("subtype") or result_payload.get("stop_reason") or "success")
    result_text = str(result_payload.get("result") or "").strip()

    return {
        "cursor_session_id": final_session_id,
        "text": result_text,
        "stop_reason": stop_reason,
        "model": final_model,
    }


def acp_prompt_session(
    agent_path: str,
    cwd: str,
    mode: str,
    text: str,
    cursor_session_id: str | None = None,
    timeout: float = 0.0,
    cancel_event: threading.Event | None = None,
    on_client_ready: Callable[[CLIPromptCanceler], None] | None = None,
    on_progress_event: Callable[[dict], None] | None = None,
) -> dict:
    configured_model = _first_nonempty_env(["CLAUDE_CODE_MODEL", "CURSOR_CLI_MODEL"], default="opus")
    model_id = _limit_fallback_model() if _is_force_auto_active() else configured_model
    force_allow_env = _first_nonempty_env(["CLAUDE_CODE_BYPASS_PERMISSIONS", "CURSOR_CLI_FORCE_ALLOW"], default="1").lower()
    force_allow = force_allow_env not in {"0", "false", "no", "off"}
    resolved_cwd = str(Path(cwd).expanduser())
    session_id = str(cursor_session_id or "").strip()

    canceler = CLIPromptCanceler()
    if on_client_ready is not None:
        on_client_ready(canceler)

    normalized_mode = str(mode or "agent").strip().lower()
    try:
        return _run_cli_prompt(
            agent_path=agent_path,
            cwd=resolved_cwd,
            session_id=session_id,
            text=text,
            timeout=timeout,
            cancel_event=cancel_event,
            model_id=model_id,
            mode=normalized_mode,
            force_allow=force_allow,
            canceler=canceler,
            on_progress_event=on_progress_event,
        )
    except RuntimeError as exc:
        err_text = str(exc)
        if not _is_usage_limit_error(err_text):
            raise

        note_usage_limit_error(err_text)

        attempted = {str(model_id or "").strip().lower()}
        for fallback_model in _fallback_models_from_env():
            candidate = str(fallback_model or "").strip()
            if not candidate:
                continue
            if candidate.lower() in attempted:
                continue
            attempted.add(candidate.lower())
            try:
                return _run_cli_prompt(
                    agent_path=agent_path,
                    cwd=resolved_cwd,
                    session_id=session_id,
                    text=text,
                    timeout=timeout,
                    cancel_event=cancel_event,
                    model_id=candidate,
                    mode=normalized_mode,
                    force_allow=force_allow,
                    canceler=canceler,
                    on_progress_event=on_progress_event,
                )
            except RuntimeError:
                continue
        raise
