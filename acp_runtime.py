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


def _is_usage_limit_error(message: str) -> bool:
    text = str(message or "").lower()
    if not text:
        return False
    signals = [
        "hit your usage limit",
        "usage limit",
        "spend limit",
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
    configured_model = str(os.environ.get("CURSOR_CLI_MODEL") or "default").strip() or "default"
    until = _forced_auto_until_date()
    active = _is_force_auto_active()
    effective_model = "auto" if active else configured_model
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
    raw = str(os.environ.get("CURSOR_CLI_FALLBACK_MODELS") or "auto,composer").strip()
    if not raw:
        return ["auto", "composer"]
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return values or ["auto", "composer"]


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


def _create_chat_session(agent_path: str, cwd: str, timeout: float) -> str:
    result = subprocess.run(
        [agent_path, "create-chat"],
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"create-chat failed ({result.returncode}): {detail}")
    session_id = (result.stdout or "").strip().splitlines()
    if not session_id:
        raise RuntimeError("create-chat returned empty session id")
    return session_id[-1].strip()


def _parse_json_result(stdout_text: str) -> dict:
    lines = [line.strip() for line in str(stdout_text or "").splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict) and payload.get("type") == "result":
            return payload
    raise RuntimeError(f"agent did not return JSON result. stdout={stdout_text[-500:]}")


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
) -> dict:
    cmd = [
        agent_path,
        "--print",
        "--output-format",
        "json",
        "--trust",
        "--resume",
        session_id,
    ]
    if force_allow:
        cmd.append("--force")
    if mode in {"plan", "ask"}:
        cmd.extend(["--mode", mode])
    if model_id:
        cmd.extend(["--model", model_id])

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    canceler.attach(proc)

    try:
        if proc.stdin is not None:
            proc.stdin.write(text)
            if not text.endswith("\n"):
                proc.stdin.write("\n")
            proc.stdin.close()
            proc.stdin = None
    except Exception:
        canceler.close()
        raise RuntimeError("failed to send prompt to agent process")

    while proc.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            canceler.close()
            raise InterruptedError("session/prompt canceled")
        time.sleep(0.1)

    stdout_text = proc.stdout.read() if proc.stdout is not None else ""
    stderr_text = proc.stderr.read() if proc.stderr is not None else ""
    if proc.returncode != 0:
        detail = (stderr_text or stdout_text or "").strip()
        raise RuntimeError(f"session/prompt failed ({proc.returncode}): {detail}")

    payload = _parse_json_result(stdout_text)
    if bool(payload.get("is_error")):
        detail = str(payload.get("result") or payload.get("error") or "unknown error").strip()
        raise RuntimeError(f"session/prompt failed (result error): {detail}")
    return {
        "cursor_session_id": str(payload.get("session_id") or session_id),
        "text": str(payload.get("result") or "").strip(),
        "stop_reason": str(payload.get("subtype") or "success"),
        "model": str(model_id or "default").strip() or "default",
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
) -> dict:
    configured_model = str(os.environ.get("CURSOR_CLI_MODEL") or "").strip() or None
    model_id = "auto" if _is_force_auto_active() else configured_model
    force_allow_env = str(os.environ.get("CURSOR_CLI_FORCE_ALLOW", "1")).strip().lower()
    force_allow = force_allow_env not in {"0", "false", "no", "off"}
    resolved_cwd = str(Path(cwd).expanduser())
    session_id = str(cursor_session_id or "").strip()
    if not session_id:
        session_id = _create_chat_session(agent_path, resolved_cwd, timeout=120.0)

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
                )
            except RuntimeError:
                continue
        raise
