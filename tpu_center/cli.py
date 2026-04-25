#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import sys
import time
from typing import Any


CENTER_ROOT = Path(os.environ.get("ZHH_CENTER_ROOT", "/kmh-nfs-ssd-us-mount/staging/.tpu_center"))
RUN_STATUSES = ("QUEUED", "APPLYING", "RUNNING", "STALE", "INFRA_RETRY", "FAILED", "FINISHED", "CANCELLED")


def now_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ensure_layout(root: Path = CENTER_ROOT) -> None:
    for rel in ("inbox", "processing", "failed_requests", "runs", "leases", "inventory", "logs"):
        (root / rel).mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{random.randrange(1_000_000):06d}")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)
    tmp.replace(path)


def atomic_write_json(path: Path, payload: dict[str, Any], mode: int = 0o644) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", mode)


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def append_event(run_dir: Path, event: str, **fields: Any) -> None:
    payload = {"ts": now_ts(), "event": event, **fields}
    with (run_dir / "events.log").open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def run_id_from_stage_dir(stage_dir: str) -> str:
    return hashlib.sha1(str(Path(stage_dir).resolve()).encode("utf-8")).hexdigest()[:16]


def request_id() -> str:
    return f"{time.strftime('%Y%m%d_%H%M%S', time.gmtime())}_{os.getpid()}_{random.randrange(1_000_000):06d}"


def parse_wandb_notes(stage_dir: Path) -> str:
    config_path = stage_dir / "configs" / "remote_run_config.yml"
    if not config_path.exists():
        return ""

    text = config_path.read_text(encoding="utf-8", errors="replace")
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
        logging_cfg = data.get("logging") if isinstance(data, dict) else None
        notes = logging_cfg.get("wandb_notes") if isinstance(logging_cfg, dict) else None
        if notes is not None:
            return str(notes).strip()
    except Exception:
        pass

    in_logging = False
    logging_indent = 0
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if stripped.startswith("logging:"):
            in_logging = True
            logging_indent = indent
            continue
        if in_logging and indent <= logging_indent and not stripped.startswith("wandb_notes:"):
            in_logging = False
        if in_logging and stripped.startswith("wandb_notes:"):
            value = stripped.split(":", 1)[1].strip()
            if (value.startswith("'") and value.endswith("'")) or (value.startswith('"') and value.endswith('"')):
                value = value[1:-1]
            return value
    return ""


def parse_wandb_url(output_log: Path) -> tuple[str, str]:
    if not output_log.exists():
        return "", ""
    try:
        text = output_log.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return "", ""
    match = re.search(r"wandb: .*View run at (https://wandb\.ai/\S+)", text)
    if not match:
        match = re.search(r"(https://wandb\.ai/\S+/runs/[A-Za-z0-9_-]+)", text)
    if not match:
        return "", ""
    url = match.group(1).rstrip(".,)")
    run_id = url.rstrip("/").split("/")[-1]
    return url, run_id


def env_snapshot() -> tuple[dict[str, str], dict[str, str]]:
    metadata_keys = ("PROJECT", "WHO", "WECODE_USER", "VM_NAME", "ZONE", "TPU_TYPES")
    secret_keys = ("WANDB_API_KEY",)
    metadata = {k: os.environ.get(k, "") for k in metadata_keys if os.environ.get(k, "")}
    secrets = {k: os.environ.get(k, "") for k in secret_keys if os.environ.get(k, "")}
    return metadata, secrets


def submit(args: argparse.Namespace) -> int:
    ensure_layout()
    stage_dir = Path(args.stage_dir).expanduser().resolve()
    if not stage_dir.exists() or not stage_dir.is_dir():
        print(f"stage dir not found: {stage_dir}", file=sys.stderr)
        return 1
    priority = int(args.priority)
    extra_args = list(args.extra_args or [])
    if extra_args and extra_args[0] == "--":
        extra_args = extra_args[1:]

    metadata_env, secret_env = env_snapshot()
    rid = run_id_from_stage_dir(str(stage_dir))
    req = request_id()
    request = {
        "schema_version": 1,
        "request_id": req,
        "run_id": rid,
        "stage_dir": str(stage_dir),
        "cwd": str(Path(args.cwd).expanduser().resolve()) if args.cwd else "",
        "priority": priority,
        "submitted_at": now_ts(),
        "description": parse_wandb_notes(stage_dir),
        "requirements": {
            "vm_name": metadata_env.get("VM_NAME", ""),
            "zone": metadata_env.get("ZONE", ""),
            "tpu_types": metadata_env.get("TPU_TYPES", ""),
        },
        "env": metadata_env,
        "secret_env": secret_env,
        "extra_args": extra_args,
    }
    path = CENTER_ROOT / "inbox" / f"{req}.json"
    atomic_write_json(path, request, mode=0o600)
    print(f"Submitted run {rid}")
    print(f"  stage_dir: {stage_dir}")
    print(f"  priority:  {priority}")
    print(f"  inbox:     {path}")
    return 0


def ingest_request(path: Path) -> tuple[bool, str]:
    processing = CENTER_ROOT / "processing" / path.name
    try:
        path.replace(processing)
    except FileNotFoundError:
        return False, "request disappeared"

    try:
        request = read_json(processing)
        stage_dir = str(Path(str(request["stage_dir"])).resolve())
        rid = str(request.get("run_id") or run_id_from_stage_dir(stage_dir))
        run_dir = CENTER_ROOT / "runs" / rid
        run_dir.mkdir(parents=True, exist_ok=True)
        run_path = run_dir / "run.json"
        created = not run_path.exists()

        if created:
            run = {
                "schema_version": 1,
                "run_id": rid,
                "stage_dir": stage_dir,
                "project": request.get("env", {}).get("PROJECT", ""),
                "who": request.get("env", {}).get("WHO", request.get("env", {}).get("WECODE_USER", "")),
                "priority": int(request.get("priority", 0)),
                "submitted_at": request.get("submitted_at", now_ts()),
                "accepted_at": now_ts(),
                "status": "QUEUED",
                "description": request.get("description") or parse_wandb_notes(Path(stage_dir)),
                "requirements": request.get("requirements", {}),
                "extra_args": request.get("extra_args", []),
                "assigned_tpu": None,
                "current_log_dir": None,
                "output_log": None,
                "wandb_url": None,
                "wandb_run_id": None,
                "last_log_mtime": None,
                "attempts": [],
                "last_error": None,
            }
            atomic_write_json(run_path, run)
            atomic_write_json(run_dir / "submit_request.json", request, mode=0o600)
            secret_env = request.get("secret_env", {})
            if secret_env:
                atomic_write_json(run_dir / "secret_env.json", secret_env, mode=0o600)
            append_event(run_dir, "accepted", request_id=request.get("request_id"))
        else:
            append_event(run_dir, "duplicate_submit", request_id=request.get("request_id"))
        processing.unlink(missing_ok=True)
        return created, rid
    except Exception as exc:
        failed = CENTER_ROOT / "failed_requests" / processing.name
        processing.replace(failed)
        return False, f"failed to ingest {path.name}: {exc}"


def update_observations(run: dict[str, Any]) -> bool:
    changed = False
    output = run.get("output_log")
    if output:
        output_log = Path(str(output))
        if output_log.exists():
            mtime = int(output_log.stat().st_mtime)
            if run.get("last_log_mtime") != mtime:
                run["last_log_mtime"] = mtime
                changed = True
            url, wandb_run_id = parse_wandb_url(output_log)
            if url and run.get("wandb_url") != url:
                run["wandb_url"] = url
                run["wandb_run_id"] = wandb_run_id
                changed = True
    return changed


def ingest_once(verbose: bool = True) -> int:
    ensure_layout()
    count = 0
    for path in sorted((CENTER_ROOT / "inbox").glob("*.json")):
        created, msg = ingest_request(path)
        if created:
            count += 1
            if verbose:
                print(f"accepted {msg}")
        elif verbose and msg:
            print(msg, file=sys.stderr)
    return count


def load_runs() -> list[dict[str, Any]]:
    ensure_layout()
    runs: list[dict[str, Any]] = []
    for path in sorted((CENTER_ROOT / "runs").glob("*/run.json")):
        try:
            run = read_json(path)
            if update_observations(run):
                atomic_write_json(path, run)
            runs.append(run)
        except Exception as exc:
            print(f"failed to read {path}: {exc}", file=sys.stderr)
    return runs


def fmt_age(ts: int | None) -> str:
    if not ts:
        return "-"
    delta = max(0, int(time.time()) - int(ts))
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


def status(_: argparse.Namespace) -> int:
    runs = load_runs()
    status_order = {name: i for i, name in enumerate(("RUNNING", "APPLYING", "STALE", "INFRA_RETRY", "QUEUED", "FAILED", "FINISHED", "CANCELLED"))}
    runs.sort(key=lambda r: (status_order.get(str(r.get("status")), 99), -int(r.get("priority", 0)), str(r.get("submitted_at", ""))))
    if not runs:
        print("No centralized runs found.")
        return 0
    print(f"TPU center root: {CENTER_ROOT}")
    print(f"{'STATUS':<12} {'PRI':>5} {'RUN_ID':<16} {'TPU':<36} {'AGE':<6} DESCRIPTION")
    for run in runs:
        tpu = run.get("assigned_tpu") or run.get("requirements") or {}
        if isinstance(tpu, dict):
            vm = tpu.get("vm_name") or "-"
            zone = tpu.get("zone") or ""
            tpu_text = f"{vm}@{zone}" if zone else str(vm)
        else:
            tpu_text = str(tpu)
        desc = str(run.get("description") or "-").replace("\n", " ")
        print(f"{str(run.get('status', '-')):<12} {int(run.get('priority', 0)):>5} {str(run.get('run_id', '-')):<16} {tpu_text[:36]:<36} {fmt_age(run.get('last_log_mtime')):<6} {desc[:100]}")
        if run.get("wandb_url"):
            print(f"{'':<36} wandb: {run['wandb_url']}")
        print(f"{'':<36} stage: {run.get('stage_dir', '-')}")
    return 0


def start(args: argparse.Namespace) -> int:
    ensure_layout()
    lock_path = CENTER_ROOT / "center.lock"
    with lock_path.open("w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"another center appears to be running (lock: {lock_path})", file=sys.stderr)
            return 1
        print(f"TPU center started. root={CENTER_ROOT}")
        while True:
            atomic_write_json(CENTER_ROOT / "center_heartbeat.json", {"pid": os.getpid(), "ts": now_ts()})
            ingest_once(verbose=not args.quiet)
            load_runs()
            time.sleep(args.interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zhh center", description="Centralized TPU distribution center")
    sub = parser.add_subparsers(dest="command", required=True)

    p_submit = sub.add_parser("submit-staged", help="submit an already staged directory via inbox")
    p_submit.add_argument("--stage-dir", required=True)
    p_submit.add_argument("--priority", type=int, default=0)
    p_submit.add_argument("--cwd", default="")
    p_submit.add_argument("extra_args", nargs=argparse.REMAINDER)
    p_submit.set_defaults(func=submit)

    p_ingest = sub.add_parser("ingest-once", help="ingest inbox requests once")
    p_ingest.set_defaults(func=lambda args: 0 if ingest_once(verbose=True) >= 0 else 1)

    p_status = sub.add_parser("s", aliases=["status"], help="show centralized run status")
    p_status.set_defaults(func=status)

    p_start = sub.add_parser("start", help="start the center daemon loop")
    p_start.add_argument("--interval", type=float, default=5.0)
    p_start.add_argument("--quiet", action="store_true")
    p_start.set_defaults(func=start)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
