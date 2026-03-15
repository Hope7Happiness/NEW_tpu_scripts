#!/usr/bin/env python3
import argparse
import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path


class ACPClient:
    def __init__(self, agent_path: str, cwd: str):
        self.agent_path = agent_path
        self.cwd = cwd
        self.proc = None
        self._next_id = 1
        self._pending = {}
        self._pending_lock = threading.Lock()
        self.updates = queue.Queue()
        self._stderr_lines = queue.Queue()

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
        if self.proc and self.proc.poll() is None:
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
                self.updates.put({"type": "raw", "line": line})
                continue

            if "id" in msg and ("result" in msg or "error" in msg):
                with self._pending_lock:
                    waiter = self._pending.pop(msg["id"], None)
                if waiter:
                    waiter.put(msg)
                continue

            method = msg.get("method")
            if method == "session/update":
                self.updates.put(msg)
            elif method == "session/request_permission":
                self.respond(msg["id"], {
                    "outcome": {"outcome": "selected", "optionId": "allow-once"}
                })
            else:
                self.updates.put(msg)

    def _read_stderr(self):
        for line in self.proc.stderr:
            self._stderr_lines.put(line.rstrip("\n"))

    def request(self, method: str, params: dict, timeout: float = 60.0):
        req_id = self._next_id
        self._next_id += 1
        waiter = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[req_id] = waiter
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        msg = waiter.get(timeout=timeout)
        if "error" in msg:
            raise RuntimeError(f"{method} failed: {msg['error']}")
        return msg["result"]

    def respond(self, req_id: int, result: dict):
        payload = {"jsonrpc": "2.0", "id": req_id, "result": result}
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def drain_stderr(self):
        out = []
        while True:
            try:
                out.append(self._stderr_lines.get_nowait())
            except queue.Empty:
                return out


def extract_text(update: dict) -> str:
    u = (update.get("params") or {}).get("update") or {}
    if u.get("sessionUpdate") == "agent_message_chunk":
        content = u.get("content") or {}
        if isinstance(content, dict):
            return content.get("text") or ""
    return ""


def main():
    parser = argparse.ArgumentParser(description="Minimal Cursor ACP probe")
    parser.add_argument("--agent-path", default=str(Path.home() / ".local/bin/agent"))
    parser.add_argument("--cwd", default=str(Path.cwd()))
    parser.add_argument("--prompt", default="Reply with exactly: ACP probe OK")
    parser.add_argument("--load-session", default="", help="Existing ACP/CLI session id to load")
    parser.add_argument("--mode", default="agent", choices=["agent", "ask", "plan"])
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    client = ACPClient(args.agent_path, args.cwd)
    try:
        client.start()

        init = client.request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
            "clientInfo": {"name": "cursor-acp-probe", "version": "0.1"},
        })
        print("INIT:", json.dumps(init, ensure_ascii=False))

        auth = client.request("authenticate", {"methodId": "cursor_login"}, timeout=120.0)
        print("AUTH:", json.dumps(auth, ensure_ascii=False))

        if args.load_session:
            loaded = client.request("session/load", {
                "sessionId": args.load_session,
                "cwd": args.cwd,
                "mcpServers": [],
                "mode": args.mode,
            }, timeout=120.0)
            session_id = loaded.get("sessionId") or args.load_session
            print("LOADED_SESSION:", session_id)
            print("LOAD_RESULT:", json.dumps(loaded, ensure_ascii=False))
        else:
            created = client.request("session/new", {
                "cwd": args.cwd,
                "mcpServers": [],
                "mode": args.mode,
            }, timeout=120.0)
            session_id = created["sessionId"]
            print("NEW_SESSION:", session_id)
            print("NEW_RESULT:", json.dumps(created, ensure_ascii=False))

        done = {"value": False}
        streamed = []

        def pump_updates():
            while not done["value"]:
                try:
                    update = client.updates.get(timeout=0.2)
                except queue.Empty:
                    continue
                text = extract_text(update)
                if text:
                    sys.stdout.write(text)
                    sys.stdout.flush()
                    streamed.append(text)

        t = threading.Thread(target=pump_updates, daemon=True)
        t.start()

        result = client.request("session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": args.prompt}],
        }, timeout=args.timeout)
        done["value"] = True
        t.join(timeout=1)

        print("\nSTOP_REASON:", result.get("stopReason"))
        print("RESULT:", json.dumps(result, ensure_ascii=False))
        print("STREAMED_TEXT:", "".join(streamed))

        stderr_lines = client.drain_stderr()
        if stderr_lines:
            print("STDERR:")
            for line in stderr_lines:
                print(line)
    finally:
        client.close()


if __name__ == "__main__":
    main()
