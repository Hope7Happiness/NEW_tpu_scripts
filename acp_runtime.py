from __future__ import annotations

import json
import queue
import subprocess
import threading
from collections import deque


class ACPClient:
    def __init__(self, agent_path: str, cwd: str):
        self.agent_path = agent_path
        self.cwd = cwd
        self.proc = None
        self.pending: dict[int, queue.Queue] = {}
        self.pending_lock = threading.Lock()
        self.updates: queue.Queue = queue.Queue()
        self._recent_stderr = deque(maxlen=30)
        self._next_id = 1

    def start(self):
        self.proc = subprocess.Popen(
            [self.agent_path, "acp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=self.cwd,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def close(self):
        if not self.proc:
            return
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass

    def _read_stdout(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            if "id" in msg and ("result" in msg or "error" in msg):
                with self.pending_lock:
                    waiter = self.pending.pop(msg["id"], None)
                if waiter is not None:
                    waiter.put(msg)
                continue

            if msg.get("method") == "session/request_permission":
                self.respond(msg["id"], {
                    "outcome": {"outcome": "selected", "optionId": "allow-once"}
                })
                continue

            if msg.get("method") == "session/update":
                self.updates.put(msg)

    def _read_stderr(self):
        for line in self.proc.stderr:
            self._recent_stderr.append(line.rstrip("\n"))

    def request(self, method: str, params: dict, timeout: float = 120.0) -> dict:
        request_id = self._next_id
        self._next_id += 1

        waiter: queue.Queue = queue.Queue(maxsize=1)
        with self.pending_lock:
            self.pending[request_id] = waiter

        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

        try:
            msg = waiter.get(timeout=timeout)
        except queue.Empty as exc:
            with self.pending_lock:
                self.pending.pop(request_id, None)
            stderr_tail = " | ".join(list(self._recent_stderr)[-5:])
            proc_state = "exited" if (self.proc and self.proc.poll() is not None) else "running"
            detail = f"{method} timed out after {timeout}s (agent process {proc_state})"
            if stderr_tail:
                detail += f"; stderr: {stderr_tail}"
            raise RuntimeError(detail) from exc
        if "error" in msg:
            raise RuntimeError(msg["error"])
        return msg["result"]

    def respond(self, request_id: int, result: dict) -> None:
        payload = {"jsonrpc": "2.0", "id": request_id, "result": result}
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()


def extract_chunk_text(update_message: dict) -> str:
    params = update_message.get("params") or {}
    update = params.get("update") or {}
    if update.get("sessionUpdate") != "agent_message_chunk":
        return ""
    content = update.get("content") or {}
    if isinstance(content, dict):
        return content.get("text") or ""
    return ""


def acp_initialize(client: ACPClient) -> None:
    client.request("initialize", {
        "protocolVersion": 1,
        "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False},
            "terminal": False,
        },
        "clientInfo": {"name": "cursor-server", "version": "0.1"},
    })
    client.request("authenticate", {"methodId": "cursor_login"})


def acp_prompt_session(
    agent_path: str,
    cwd: str,
    mode: str,
    text: str,
    cursor_session_id: str | None = None,
    timeout: float = 300.0,
) -> dict:
    client = ACPClient(agent_path, cwd)
    try:
        client.start()
        acp_initialize(client)
        if cursor_session_id:
            client.request("session/load", {
                "sessionId": cursor_session_id,
                "cwd": cwd,
                "mcpServers": [],
                "mode": mode,
            })
            active_session_id = cursor_session_id
        else:
            new_session = client.request("session/new", {
                "cwd": cwd,
                "mcpServers": [],
                "mode": mode,
            })
            active_session_id = new_session["sessionId"]

        chunks: list[str] = []
        stop_flag = {"done": False}

        def pump_updates():
            while not stop_flag["done"]:
                try:
                    update = client.updates.get(timeout=0.2)
                except queue.Empty:
                    continue
                chunk = extract_chunk_text(update)
                if chunk:
                    chunks.append(chunk)

        pump_thread = threading.Thread(target=pump_updates, daemon=True)
        pump_thread.start()

        result = client.request("session/prompt", {
            "sessionId": active_session_id,
            "prompt": [{"type": "text", "text": text}],
        }, timeout=timeout)

        stop_flag["done"] = True
        pump_thread.join(timeout=1)

        return {
            "cursor_session_id": active_session_id,
            "text": "".join(chunks).strip(),
            "stop_reason": result.get("stopReason", "unknown"),
        }
    finally:
        client.close()
