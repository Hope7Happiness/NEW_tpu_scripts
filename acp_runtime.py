from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable


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
    cmd.append(text)

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    canceler.attach(proc)

    started_at = time.time()
    while proc.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            canceler.close()
            raise InterruptedError("session/prompt canceled")
        if (time.time() - started_at) > timeout:
            canceler.close()
            raise RuntimeError(f"session/prompt timed out after {timeout:.1f}s (agent process running)")
        time.sleep(0.1)

    stdout_text, stderr_text = proc.communicate()
    if proc.returncode != 0:
        detail = (stderr_text or stdout_text or "").strip()
        raise RuntimeError(f"session/prompt failed ({proc.returncode}): {detail}")

    payload = _parse_json_result(stdout_text)
    return {
        "cursor_session_id": str(payload.get("session_id") or session_id),
        "text": str(payload.get("result") or "").strip(),
        "stop_reason": str(payload.get("subtype") or "success"),
    }


def acp_prompt_session(
    agent_path: str,
    cwd: str,
    mode: str,
    text: str,
    cursor_session_id: str | None = None,
    timeout: float = 900.0,
    cancel_event: threading.Event | None = None,
    on_client_ready: Callable[[CLIPromptCanceler], None] | None = None,
) -> dict:
    model_id = str(os.environ.get("CURSOR_CLI_MODEL") or "").strip() or None
    force_allow_env = str(os.environ.get("CURSOR_CLI_FORCE_ALLOW", "1")).strip().lower()
    force_allow = force_allow_env not in {"0", "false", "no", "off"}
    resolved_cwd = str(Path(cwd).expanduser())
    session_id = str(cursor_session_id or "").strip()
    if not session_id:
        session_id = _create_chat_session(agent_path, resolved_cwd, timeout=min(timeout, 120.0))

    canceler = CLIPromptCanceler()
    if on_client_ready is not None:
        on_client_ready(canceler)

    return _run_cli_prompt(
        agent_path=agent_path,
        cwd=resolved_cwd,
        session_id=session_id,
        text=text,
        timeout=timeout,
        cancel_event=cancel_event,
        model_id=model_id,
        mode=str(mode or "agent").strip().lower(),
        force_allow=force_allow,
        canceler=canceler,
    )
