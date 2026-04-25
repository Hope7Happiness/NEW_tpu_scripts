#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shlex
import subprocess
import shutil
import sys
import time
from typing import Any


CENTER_ROOT = Path(os.environ.get("ZHH_CENTER_ROOT", "/kmh-nfs-ssd-us-mount/staging/.tpu_center"))
RUN_STATUSES = ("QUEUED", "APPLYING", "RUNNING", "STALE", "INFRA_RETRY", "FAILED", "FINISHED", "CANCELLED")
SCRIPT_ROOT = Path(os.environ.get("ZHH_SCRIPT_ROOT", Path(__file__).resolve().parents[1]))
LEGACY_LOCK_ROOT = Path("/kmh-nfs-ssd-us-mount/code/qiao/tpu_lock")
WORKER_USER = os.environ.get("ZHH_CENTER_WORKER_USER", "zak")
SUDO_PASSWORD_FILE = Path(os.environ.get("ZHH_CENTER_SUDO_PASSWORD_FILE", SCRIPT_ROOT / ".center_sudo_password"))


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


def tmux_name(run_id: str) -> str:
    return f"zhh_center_{run_id[:12]}"


def parse_tpu_type(vm_name: str) -> tuple[str, str]:
    match = re.search(r"(v[0-9][a-z0-9]*)-([0-9]+)", vm_name)
    if not match:
        return "", ""
    return match.group(1), match.group(2)


def requirement_class(vm_name: str) -> str:
    if vm_name in ("auto", "autov6", "autov6e"):
        return "v6e"
    if "autov5" in vm_name or "autov5p" in vm_name:
        return "v5p"
    if "autov4" in vm_name:
        return "v4"
    return ""


def should_sudo_to_worker_user() -> bool:
    return bool(WORKER_USER) and WORKER_USER != getpass.getuser()


def sudo_root_shell_command(root_script: str) -> str:
    quoted_script = shlex.quote(root_script)
    if SUDO_PASSWORD_FILE.exists():
        return f"printf '%s\\n' \"$(cat {shlex.quote(str(SUDO_PASSWORD_FILE))})\" | sudo -S -p '' bash -lc {quoted_script}"
    return f"sudo -n bash -lc {quoted_script}"


def worker_user_shell_command(inner_command: str, env_file: Path | None = None) -> str:
    if not should_sudo_to_worker_user():
        if env_file is None:
            return inner_command
        return f"source {shlex.quote(str(env_file))} && {inner_command}"

    env_part = ""
    if env_file is not None:
        env_part = f"set -a; source {shlex.quote(str(env_file))}; set +a; "
    root_script = f"{env_part}exec sudo -E -H -u {shlex.quote(WORKER_USER)} bash -lc {shlex.quote(inner_command)}"
    return sudo_root_shell_command(root_script)


def run_shell_as_worker_user(command: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return run_shell(worker_user_shell_command(command), timeout=timeout)


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


def classify_failure(exit_code: int, output_log: Path | None) -> tuple[str, str]:
    if exit_code == 0:
        return "FINISHED", "exit code 0"
    text = ""
    if output_log and output_log.exists():
        try:
            text = output_log.read_text(encoding="utf-8", errors="replace")
        except Exception:
            text = ""
    if not text:
        return "INFRA_RETRY", f"exit code {exit_code}; no output log"
    infra_patterns = (
        "[/usr/bin/ssh] exited with return code [255]",
        "Terminating process because the coordinator detected missing heartbeats.",
        "googlecloudsdk.command_lib.util.ssh.ssh.CommandError",
        "ERROR: (gcloud.compute.tpus.tpu-vm.ssh)",
        "UNKNOWN: TPU initialization failed:",
        "ABORTED: The TPU is already in use by process",
        "Unable to initialize backend",
        "Command execution on worker 0 failed with exit status 134",
        "(core dumped)",
    )
    for pattern in infra_patterns:
        if pattern in text:
            return "INFRA_RETRY", pattern
    code_patterns = (
        "Traceback (most recent call last)",
        "RuntimeError:",
        "ValueError:",
        "AssertionError",
        "ModuleNotFoundError",
        "KeyError:",
    )
    for pattern in code_patterns:
        if pattern in text:
            return "FAILED", pattern
    return "FAILED", f"exit code {exit_code}; no infra signature"


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


def run_command(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def run_shell(command: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def run_itou() -> list[dict[str, str]]:
    command = os.environ.get("ZHH_ITOU_COMMAND", "itou")
    try:
        proc = run_shell_as_worker_user(command, timeout=int(os.environ.get("ZHH_ITOU_TIMEOUT", "30")))
    except Exception as exc:
        print(f"failed to run itou: {exc}", file=sys.stderr)
        return []
    if proc.returncode != 0:
        print((proc.stderr or proc.stdout or "itou failed").strip(), file=sys.stderr)
        return []
    candidates: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        vm_name, zone = parts[0], parts[1]
        key = (vm_name, zone)
        if key in seen:
            continue
        seen.add(key)
        tpu_class, tpu_size = parse_tpu_type(vm_name)
        candidates.append({"vm_name": vm_name, "zone": zone, "class": tpu_class, "size": tpu_size})
    return candidates


def has_legacy_lock(vm_name: str) -> bool:
    if not LEGACY_LOCK_ROOT.exists():
        return False
    return any(LEGACY_LOCK_ROOT.glob(f"*_{vm_name}_*"))


def lease_path(vm_name: str) -> Path:
    return CENTER_ROOT / "leases" / f"{vm_name}.json"


def has_center_lease(vm_name: str) -> bool:
    return lease_path(vm_name).exists()


def cloud_ready(vm_name: str, zone: str) -> bool:
    if os.environ.get("ZHH_CENTER_SKIP_CLOUD_CHECK") == "1":
        return True
    try:
        command = " ".join([
            "gcloud", "compute", "tpus", "tpu-vm", "describe",
            shlex.quote(vm_name), shlex.quote(f"--zone={zone}"), shlex.quote("--format=value(state)"),
        ])
        proc = run_shell_as_worker_user(command, timeout=25)
    except Exception:
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "READY"


def remote_has_python(vm_name: str, zone: str) -> bool:
    if os.environ.get("ZHH_CENTER_SKIP_REMOTE_BUSY_CHECK") == "1":
        return False
    marker = f"zhhcenter{random.randrange(1_000_000):06d}"
    remote_command = f"ps -ef | grep python | grep -E '(\\.py|-m)' | grep -v {marker} | grep -v grep"
    try:
        gcloud = str(SCRIPT_ROOT / "google-cloud-sdk/bin/gcloud") if (SCRIPT_ROOT / "google-cloud-sdk/bin/gcloud").exists() else "gcloud"
        command = " ".join([
            shlex.quote(gcloud), "compute", "tpus", "tpu-vm", "ssh",
            shlex.quote(vm_name), "--zone", shlex.quote(zone), "--command", shlex.quote(remote_command),
        ])
        proc = run_shell_as_worker_user(command, timeout=30)
    except Exception:
        return True
    return bool(proc.stdout.strip())


def available_tpus() -> list[dict[str, str]]:
    ensure_layout()
    available: list[dict[str, str]] = []
    items: list[dict[str, str]] = []
    raw = run_itou()
    for item in raw:
        vm_name = item["vm_name"]
        zone = item["zone"]
        reason = ""
        if has_center_lease(vm_name):
            reason = "center lease"
        elif has_legacy_lock(vm_name):
            reason = "legacy lock"
        elif not cloud_ready(vm_name, zone):
            reason = "not READY"
        elif remote_has_python(vm_name, zone):
            reason = "remote python process"
        if reason:
            item = {**item, "available": "false", "reason": reason}
        else:
            item = {**item, "available": "true", "reason": ""}
            available.append(item)
        item.setdefault("checked_at", now_ts())
        items.append(item)
    atomic_write_json(CENTER_ROOT / "inventory" / "latest.json", {"ts": now_ts(), "items": items, "available": available})
    return available


def run_matches_tpu(run: dict[str, Any], tpu: dict[str, str]) -> bool:
    req = run.get("requirements") or {}
    vm_req = str(req.get("vm_name") or "")
    zone_req = str(req.get("zone") or "")
    type_req = str(req.get("tpu_types") or "")
    zones = [z.strip() for z in zone_req.split(",") if z.strip()]
    if zones and tpu["zone"] not in zones:
        return False
    if vm_req and "auto" not in vm_req:
        return tpu["vm_name"] == vm_req
    cls = requirement_class(vm_req or "auto")
    if cls and tpu.get("class") != cls:
        return False
    sizes = [s.strip() for s in type_req.split(",") if s.strip()]
    if sizes and tpu.get("size") not in sizes:
        return False
    return True


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


def write_worker_env(run_dir: Path, run: dict[str, Any]) -> Path:
    env: dict[str, str] = {}
    submit_request_path = run_dir / "submit_request.json"
    if submit_request_path.exists():
        request = read_json(submit_request_path)
        env.update({str(k): str(v) for k, v in (request.get("env") or {}).items()})
    secret_path = run_dir / "secret_env.json"
    if secret_path.exists():
        env.update({str(k): str(v) for k, v in read_json(secret_path).items()})
    env["ZHH_CENTER_ROOT"] = str(CENTER_ROOT)
    env["ZHH_CENTER_RUN_ID"] = str(run["run_id"])
    env["ZHH_SCRIPT_ROOT"] = str(SCRIPT_ROOT)
    lines = ["# generated by zhh center"]
    for key, value in sorted(env.items()):
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        lines.append(f"export {key}={shlex.quote(value)}")
    path = run_dir / "worker_env.sh"
    atomic_write_text(path, "\n".join(lines) + "\n", mode=0o600)
    return path


def create_lease(run: dict[str, Any], tpu: dict[str, str]) -> None:
    lease = {
        "run_id": run["run_id"],
        "vm_name": tpu["vm_name"],
        "zone": tpu["zone"],
        "created_at": now_ts(),
        "heartbeat_at": now_ts(),
    }
    atomic_write_json(lease_path(tpu["vm_name"]), lease)
    lock_name = f"center_{tpu['vm_name']}_{run['run_id']}_{time.strftime('%Y-%m-%d_%H-%M-%S', time.gmtime())}"
    lock_path = LEGACY_LOCK_ROOT / lock_name
    try:
        LEGACY_LOCK_ROOT.mkdir(parents=True, exist_ok=True)
        lock_path.touch()
    except PermissionError:
        command = f"mkdir -p {shlex.quote(str(LEGACY_LOCK_ROOT))} && touch {shlex.quote(str(lock_path))}"
        run_shell(sudo_root_shell_command(command), timeout=15)


def release_lease(vm_name: str) -> None:
    lease_path(vm_name).unlink(missing_ok=True)
    if LEGACY_LOCK_ROOT.exists():
        try:
            for path in LEGACY_LOCK_ROOT.glob(f"center_{vm_name}_*"):
                path.unlink(missing_ok=True)
        except PermissionError:
            command = " ".join([
                "find", shlex.quote(str(LEGACY_LOCK_ROOT)), "-maxdepth", "1", "-type", "f",
                "-name", shlex.quote(f"center_{vm_name}_*"), "-delete",
            ])
            run_shell(sudo_root_shell_command(command), timeout=15)


def launch_worker(run: dict[str, Any], tpu: dict[str, str]) -> bool:
    run_dir = CENTER_ROOT / "runs" / run["run_id"]
    run_path = run_dir / "run.json"
    env_file = write_worker_env(run_dir, run)
    session = tmux_name(run["run_id"])
    extra_args = [str(x) for x in (run.get("extra_args") or [])]
    quoted_args = " ".join(shlex.quote(x) for x in extra_args)
    inner_command = (
        f"exec {shlex.quote(str(SCRIPT_ROOT / 'main.sh'))} center-worker "
        f"{shlex.quote(run['run_id'])} {shlex.quote(run['stage_dir'])} "
        f"{shlex.quote(tpu['vm_name'])} {shlex.quote(tpu['zone'])} -- {quoted_args}"
    )
    command = worker_user_shell_command(inner_command, env_file)
    create_lease(run, tpu)
    run.update({
        "status": "APPLYING",
        "assigned_tpu": {"vm_name": tpu["vm_name"], "zone": tpu["zone"], "class": tpu.get("class", ""), "size": tpu.get("size", "")},
        "worker": {"tmux_session": session, "started_at": now_ts(), "host": os.uname().nodename},
    })
    attempts = list(run.get("attempts") or [])
    attempts.append({"ts": now_ts(), "vm_name": tpu["vm_name"], "zone": tpu["zone"], "event": "launch"})
    run["attempts"] = attempts
    atomic_write_json(run_path, run)
    append_event(run_dir, "launch_worker", vm_name=tpu["vm_name"], zone=tpu["zone"], tmux_session=session)
    proc = subprocess.run(["tmux", "new-session", "-d", "-s", session, "bash", "-lc", command], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        release_lease(tpu["vm_name"])
        run["status"] = "INFRA_RETRY"
        run["assigned_tpu"] = None
        run["last_error"] = proc.stderr.strip() or "tmux launch failed"
        atomic_write_json(run_path, run)
        append_event(run_dir, "launch_failed", error=run["last_error"])
        return False
    return True


def tmux_session_exists(session: str) -> bool:
    if not session:
        return False
    proc = subprocess.run(["tmux", "has-session", "-t", session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.returncode == 0


def reconcile_active_runs() -> int:
    changed = 0
    stale_seconds = int(os.environ.get("ZHH_CENTER_STALE_SECONDS", str(30 * 60)))
    now = int(time.time())
    for run in load_runs():
        status = str(run.get("status") or "")
        if status not in ("APPLYING", "RUNNING", "STALE"):
            continue
        run_dir = CENTER_ROOT / "runs" / run["run_id"]
        run_path = run_dir / "run.json"
        worker = run.get("worker") or {}
        session = worker.get("tmux_session") if isinstance(worker, dict) else ""
        assigned = run.get("assigned_tpu") or {}
        vm_name = assigned.get("vm_name") if isinstance(assigned, dict) else ""
        if not tmux_session_exists(str(session)):
            if vm_name:
                release_lease(str(vm_name))
            run["status"] = "INFRA_RETRY"
            run["assigned_tpu"] = None
            run["worker"] = None
            run["last_error"] = "worker tmux session is not running"
            atomic_write_json(run_path, run)
            append_event(run_dir, "worker_missing_requeued", tmux_session=session)
            changed += 1
            continue
        output_log = Path(run["output_log"]) if run.get("output_log") else None
        if output_log and output_log.exists():
            mtime = int(output_log.stat().st_mtime)
            if status == "STALE" and now - mtime < stale_seconds:
                run["status"] = "RUNNING"
                run["last_error"] = None
                atomic_write_json(run_path, run)
                append_event(run_dir, "stale_recovered")
                changed += 1
            elif status == "RUNNING" and now - mtime >= stale_seconds:
                run["status"] = "STALE"
                run["last_error"] = f"output log stale for {(now - mtime) // 60} min"
                atomic_write_json(run_path, run)
                append_event(run_dir, "marked_stale", last_log_mtime=mtime)
                changed += 1
    return changed


def schedule_once() -> int:
    runs = load_runs()
    queue = [r for r in runs if r.get("status") in ("QUEUED", "INFRA_RETRY")]
    queue.sort(key=lambda r: (-int(r.get("priority", 0)), str(r.get("submitted_at", "")), str(r.get("run_id", ""))))
    if not queue:
        return 0
    tpus = available_tpus()
    scheduled = 0
    for run in queue:
        matches = [t for t in tpus if run_matches_tpu(run, t)]
        if not matches:
            continue
        choice = random.choice(matches)
        if launch_worker(run, choice):
            scheduled += 1
            tpus = [t for t in tpus if t["vm_name"] != choice["vm_name"]]
    return scheduled


def worker_log_dir(args: argparse.Namespace) -> int:
    run_dir = CENTER_ROOT / "runs" / args.run_id
    run_path = run_dir / "run.json"
    run = read_json(run_path)
    log_dir = str(Path(args.log_dir).resolve())
    run["current_log_dir"] = log_dir
    run["output_log"] = str(Path(log_dir) / "output.log")
    run["status"] = "RUNNING"
    update_observations(run)
    atomic_write_json(run_path, run)
    append_event(run_dir, "log_dir", log_dir=log_dir)
    return 0


def worker_finished(args: argparse.Namespace) -> int:
    run_dir = CENTER_ROOT / "runs" / args.run_id
    run_path = run_dir / "run.json"
    run = read_json(run_path)
    output_log = Path(run["output_log"]) if run.get("output_log") else None
    status, reason = classify_failure(int(args.exit_code), output_log)
    assigned = run.get("assigned_tpu") or {}
    vm_name = assigned.get("vm_name") if isinstance(assigned, dict) else ""
    if vm_name:
        release_lease(str(vm_name))
    run["last_error"] = None if status == "FINISHED" else reason
    if status == "INFRA_RETRY":
        run["status"] = "INFRA_RETRY"
        run["assigned_tpu"] = None
        run["worker"] = None
    else:
        run["status"] = status
    update_observations(run)
    atomic_write_json(run_path, run)
    append_event(run_dir, "worker_finished", exit_code=int(args.exit_code), status=run["status"], reason=reason)
    return 0


def cancel(args: argparse.Namespace) -> int:
    run_dir = CENTER_ROOT / "runs" / args.run_id
    run_path = run_dir / "run.json"
    if not run_path.exists():
        print(f"run not found: {args.run_id}", file=sys.stderr)
        return 1
    run = read_json(run_path)
    worker = run.get("worker") or {}
    session = worker.get("tmux_session") if isinstance(worker, dict) else ""
    if session:
        subprocess.run(["tmux", "kill-session", "-t", str(session)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assigned = run.get("assigned_tpu") or {}
    if isinstance(assigned, dict) and assigned.get("vm_name") and assigned.get("zone") and not args.no_kill:
        kill_command = " ".join([
            "exec", shlex.quote(str(SCRIPT_ROOT / "main.sh")), "kill",
            shlex.quote(str(assigned["vm_name"])), shlex.quote(str(assigned["zone"])),
        ])
        run_shell(worker_user_shell_command(kill_command), timeout=180)
        release_lease(str(assigned["vm_name"]))
    run["status"] = "CANCELLED"
    run["last_error"] = "cancelled by user"
    atomic_write_json(run_path, run)
    append_event(run_dir, "cancelled", kill_tpu=not args.no_kill)
    print(f"cancelled {args.run_id}")
    return 0


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


def tpus(args: argparse.Namespace) -> int:
    ensure_layout()
    if args.cached:
        path = CENTER_ROOT / "inventory" / "latest.json"
        if not path.exists():
            print("No cached inventory found. Run `zhh center tpus` without --cached first.")
            return 0
        payload = read_json(path)
        items = payload.get("items", [])
        print(f"TPU inventory cached at {payload.get('ts', '-')}")
    else:
        available_tpus()
        payload = read_json(CENTER_ROOT / "inventory" / "latest.json")
        items = payload.get("items", [])
        print(f"TPU inventory refreshed at {payload.get('ts', '-')}")
    if not items:
        print("No TPU candidates found.")
        return 0
    print(f"{'STATE':<10} {'TYPE':<8} {'VM_NAME':<48} {'ZONE':<18} REASON")
    for item in items:
        state = "free" if item.get("available") == "true" else "skip"
        typ = f"{item.get('class', '')}-{item.get('size', '')}".strip("-")
        print(f"{state:<10} {typ:<8} {item.get('vm_name', '-'):<48} {item.get('zone', '-'):<18} {item.get('reason', '')}")
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
            reconciled = reconcile_active_runs()
            if reconciled and not args.quiet:
                print(f"reconciled {reconciled} active run(s)")
            if not args.no_schedule:
                scheduled = schedule_once()
                if scheduled and not args.quiet:
                    print(f"scheduled {scheduled} run(s)")
            load_runs()
            time.sleep(args.interval)


def tick(args: argparse.Namespace) -> int:
    ingest_once(verbose=not args.quiet)
    reconciled = reconcile_active_runs()
    scheduled = 0 if args.no_schedule else schedule_once()
    if not args.quiet:
        print(f"reconciled {reconciled} active run(s)")
        print(f"scheduled {scheduled} run(s)")
    return 0


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

    p_tick = sub.add_parser("tick", help="ingest and schedule once")
    p_tick.add_argument("--quiet", action="store_true")
    p_tick.add_argument("--no-schedule", action="store_true")
    p_tick.set_defaults(func=tick)

    p_status = sub.add_parser("s", aliases=["status"], help="show centralized run status")
    p_status.set_defaults(func=status)

    p_tpus = sub.add_parser("tpus", help="show center TPU inventory")
    p_tpus.add_argument("--cached", action="store_true")
    p_tpus.set_defaults(func=tpus)

    p_start = sub.add_parser("start", help="start the center daemon loop")
    p_start.add_argument("--interval", type=float, default=5.0)
    p_start.add_argument("--quiet", action="store_true")
    p_start.add_argument("--no-schedule", action="store_true")
    p_start.set_defaults(func=start)

    p_log = sub.add_parser("worker-log-dir", help=argparse.SUPPRESS)
    p_log.add_argument("--run-id", required=True)
    p_log.add_argument("--log-dir", required=True)
    p_log.set_defaults(func=worker_log_dir)

    p_done = sub.add_parser("worker-finished", help=argparse.SUPPRESS)
    p_done.add_argument("--run-id", required=True)
    p_done.add_argument("--exit-code", type=int, required=True)
    p_done.set_defaults(func=worker_finished)

    p_cancel = sub.add_parser("cancel", help="cancel a run and kill its TPU if assigned")
    p_cancel.add_argument("run_id")
    p_cancel.add_argument("--no-kill", action="store_true")
    p_cancel.set_defaults(func=cancel)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
