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
RUN_STATUSES = ("QUEUED", "RESUME_PENDING", "APPLYING", "RUNNING", "INFRA_RETRY", "FAILED", "FINISHED", "CANCELLED")
QUEUE_STATUSES = ("QUEUED", "INFRA_RETRY", "RESUME_PENDING")
SCRIPT_ROOT = Path(os.environ.get("ZHH_SCRIPT_ROOT", Path(__file__).resolve().parents[1]))
LEGACY_LOCK_ROOT = Path("/kmh-nfs-ssd-us-mount/code/qiao/tpu_lock")
WORKER_USER = os.environ.get("ZHH_CENTER_WORKER_USER", "zak")
SUDO_PASSWORD_FILE = Path(os.environ.get("ZHH_CENTER_SUDO_PASSWORD_FILE", SCRIPT_ROOT / ".center_sudo_password"))
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[90m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BLUE = "\033[34m"
CYAN = "\033[36m"


def now_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ctext(color: str, text: object) -> str:
    return f"{color}{text}{RESET}"


def print_kv(label: str, value: object, indent: str = "  ") -> None:
    print(f"{indent}{DIM}{label + ':':<14}{RESET} {value}")


def make_shared(path: Path, directory: bool | None = None) -> None:
    try:
        is_dir = path.is_dir() if directory is None else directory
        os.chmod(path, 0o777 if is_dir else 0o666)
    except FileNotFoundError:
        return
    except PermissionError:
        return


def ensure_layout(root: Path = CENTER_ROOT) -> None:
    root.mkdir(parents=True, exist_ok=True)
    make_shared(root, directory=True)
    for rel in ("inbox", "processing", "failed_requests", "runs", "leases", "inventory", "logs", "probes", "bad_tpus"):
        path = root / rel
        path.mkdir(parents=True, exist_ok=True)
        make_shared(path, directory=True)


def atomic_write_text(path: Path, text: str, mode: int = 0o666) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    make_shared(path.parent, directory=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{random.randrange(1_000_000):06d}")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)
    tmp.replace(path)


def atomic_write_json(path: Path, payload: dict[str, Any], mode: int = 0o666) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", mode)


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def append_event(run_dir: Path, event: str, **fields: Any) -> None:
    payload = {"ts": now_ts(), "event": event, **fields}
    make_shared(run_dir, directory=True)
    path = run_dir / "events.log"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    make_shared(path, directory=False)


def run_id_from_stage_dir(stage_dir: str) -> str:
    return hashlib.sha1(str(Path(stage_dir).resolve()).encode("utf-8")).hexdigest()[:16]


def tmux_name(run_id: str) -> str:
    return f"zhh_center_{run_id[:12]}"


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def parse_tpu_type(vm_name: str) -> tuple[str, str]:
    vm_name = strip_ansi(vm_name)
    match = re.search(r"(v[0-9][a-z0-9]*)-([0-9]+)", vm_name)
    if not match:
        return "", ""
    return match.group(1), match.group(2)


def format_tpu_text(tpu: dict[str, Any] | None) -> str:
    if not isinstance(tpu, dict):
        return "-"
    vm_name = str(tpu.get("vm_name") or "-")
    zone = str(tpu.get("zone") or "")
    if vm_name.startswith("auto"):
        tpu_types = str(tpu.get("tpu_types") or tpu.get("size") or "any type")
        return f"{ctext(CYAN, vm_name)} w/ {ctext(CYAN, tpu_types)} @ {ctext(CYAN, zone or 'any zone')}"
    return f"{ctext(CYAN, vm_name)} @ {ctext(CYAN, zone)}" if zone else ctext(CYAN, vm_name)


def probe_reason_is_retryable_setup(reason: str) -> bool:
    return "Have not mount disk" in reason or "Cannot find torch/jax" in reason


def requirement_classes(vm_name: str) -> tuple[str, ...]:
    if vm_name == "autov56":
        return ("v5p", "v6e")
    if vm_name in ("auto", "autov6", "autov6e"):
        return ("v6e",)
    if "autov5" in vm_name or "autov5p" in vm_name:
        return ("v5p",)
    if "autov4" in vm_name:
        return ("v4",)
    return ()


def requirement_class(vm_name: str) -> str:
    classes = requirement_classes(vm_name)
    return classes[0] if classes else ""


def parse_alias_from_bashrc(alias_name: str, bashrc: Path) -> str:
    if not bashrc.exists():
        return ""
    try:
        lines = bashrc.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    pattern = re.compile(rf"^\s*alias\s+{re.escape(alias_name)}=(.+)\s*$")
    for line in lines:
        match = pattern.match(line)
        if not match:
            continue
        value = match.group(1).strip()
        if (value.startswith("'") and value.endswith("'")) or (value.startswith('"') and value.endswith('"')):
            value = value[1:-1]
        return value.strip()
    return ""


def resolve_itou_command() -> str:
    configured = os.environ.get("ZHH_ITOU_COMMAND", "").strip()
    if configured:
        return configured

    for bashrc in (Path.home() / ".bashrc", Path("/home/wxb/.bashrc"), Path(f"/home/{WORKER_USER}/.bashrc")):
        command = parse_alias_from_bashrc("itou", bashrc)
        if command:
            return command

    fallback = Path("/kmh-nfs-ssd-us-mount/code/zak/fast_tou.sh")
    if fallback.exists():
        return f"FAST_TOU_IDLE_ONLY=1 {shlex.quote(str(fallback))}"
    return "itou"


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
        center_worker_reasons = {
            2: "assigned TPU is not READY",
            3: "assigned TPU is busy",
            4: "assigned TPU environment check failed",
            9: "assigned TPU may be preempted",
            42: "assigned TPU unusable",
        }
        if exit_code in center_worker_reasons:
            return "INFRA_RETRY", center_worker_reasons[exit_code]
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


def trust_failure_limit(run: dict[str, Any] | None = None) -> int:
    if run and run.get("trust_failure_limit"):
        try:
            return max(1, int(run["trust_failure_limit"]))
        except (TypeError, ValueError):
            pass
    return max(1, int(os.environ.get("ZHH_CENTER_TRUST_FAILURE_LIMIT", "3")))


def record_trusted_failure(run: dict[str, Any], reason: str) -> str:
    if not run.get("trusted"):
        return "not_trusted"
    limit = trust_failure_limit(run)
    count = int(run.get("trust_failed_count", 0) or 0) + 1
    run["trust_failed_count"] = count
    run["trust_failure_limit"] = limit
    run["trust_last_failure_at"] = now_ts()
    run["trust_last_failure_reason"] = reason
    if count >= limit:
        run["trusted"] = False
        run["trust_exhausted_at"] = now_ts()
        return "exhausted"
    return "resume"


def trust_summary(run: dict[str, Any]) -> str:
    limit = trust_failure_limit(run)
    count = int(run.get("trust_failed_count", 0) or 0)
    if run.get("trusted"):
        return f"trusted; {count}/{limit} FAILED"
    if run.get("trust_exhausted_at"):
        return f"exhausted; {count}/{limit} FAILED"
    return ""


def env_snapshot() -> tuple[dict[str, str], dict[str, str]]:
    metadata_keys = ("PROJECT", "WHO", "WECODE_USER", "VM_NAME", "ZONE", "TPU_TYPES")
    secret_keys = ("WANDB_API_KEY",)
    metadata = {k: os.environ.get(k, "") for k in metadata_keys if os.environ.get(k, "")}
    secrets = {k: os.environ.get(k, "") for k in secret_keys if os.environ.get(k, "")}
    return metadata, secrets


def normalize_extra_args(extra_args: list[str] | None) -> list[str]:
    normalized = list(extra_args or [])
    if normalized and normalized[0] == "--":
        normalized = normalized[1:]
    return normalized


def build_submit_request(stage_dir_text: str, cwd: str, priority: int, extra_args: list[str], run_id: str | None = None) -> tuple[dict[str, Any], Path]:
    stage_dir = Path(stage_dir_text).expanduser().resolve()
    if not stage_dir.exists() or not stage_dir.is_dir():
        raise ValueError(f"stage dir not found: {stage_dir}")

    metadata_env, secret_env = env_snapshot()
    rid = run_id or run_id_from_stage_dir(str(stage_dir))
    req = request_id()
    request = {
        "schema_version": 1,
        "request_id": req,
        "run_id": rid,
        "stage_dir": str(stage_dir),
        "cwd": str(Path(cwd).expanduser().resolve()) if cwd else "",
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
    return request, stage_dir


def write_submit_request(request: dict[str, Any]) -> Path:
    path = CENTER_ROOT / "inbox" / f"{request['request_id']}.json"
    atomic_write_json(path, request, mode=0o600)
    return path


def print_submit_request(action: str, request: dict[str, Any], stage_dir: Path, path: Path) -> None:
    print(f"{action} {ctext(BOLD, request['run_id'])}")
    print_kv("description", ctext(BOLD, request["description"] or "-"))
    print_kv("tpu", format_tpu_text(request["requirements"]))
    print_kv("stage_dir", stage_dir)
    print_kv("priority", request["priority"])
    print_kv("inbox", path)


def submit(args: argparse.Namespace) -> int:
    ensure_layout()
    try:
        request, stage_dir = build_submit_request(
            args.stage_dir,
            args.cwd,
            int(args.priority),
            normalize_extra_args(args.extra_args),
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    path = write_submit_request(request)
    print_submit_request("Submitted run", request, stage_dir, path)
    return 0


def submit_replace(args: argparse.Namespace) -> int:
    ensure_layout()
    rid, run_path = resolve_run(args.run_id, quiet=True)
    request_path: Path | None = None
    previous_status = "PENDING"
    priority = 0

    if rid and run_path:
        previous = read_json(run_path)
        previous_status = str(previous.get("status") or "")
        try:
            priority = int(previous.get("priority", 0) or 0)
        except (TypeError, ValueError):
            priority = 0
        if previous_status in ("RUNNING", "APPLYING") and not args.force:
            print(
                f"refusing to replace active run {rid} ({previous_status}); use `wxb sub --force {rid}` to cancel and replace it",
                file=sys.stderr,
            )
            return 1
    else:
        pending_rid, pending_path = resolve_pending_request(args.run_id)
        if not pending_rid or not pending_path:
            print(f"run not found: {args.run_id}", file=sys.stderr)
            return 1
        rid = pending_rid
        request_path = pending_path
        try:
            priority = int(read_json(pending_path).get("priority", 0) or 0)
        except (TypeError, ValueError):
            priority = 0

    try:
        request, stage_dir = build_submit_request(
            args.stage_dir,
            args.cwd,
            priority,
            normalize_extra_args(args.extra_args),
            run_id=rid,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    request["replaces"] = {
        "run_id": rid,
        "previous_status": previous_status or "-",
        "replaced_at": now_ts(),
    }
    staged_request_path = CENTER_ROOT / "processing" / f"replace_{request['request_id']}.json"
    final_request_path = CENTER_ROOT / "inbox" / f"{request['request_id']}.json"
    atomic_write_json(staged_request_path, request, mode=0o600)

    try:
        if run_path:
            ok, deleted_status, _, _ = delete_run_path(rid, run_path, verbose=False)
            if not ok:
                staged_request_path.unlink(missing_ok=True)
                return 1
            previous_status = deleted_status or previous_status
            delete_pending_requests_for_run_id(rid)
        elif request_path:
            request_path.unlink(missing_ok=True)
        staged_request_path.replace(final_request_path)
    except Exception as exc:
        staged_request_path.unlink(missing_ok=True)
        print(f"failed to replace run {rid}: {exc}", file=sys.stderr)
        return 1

    print_submit_request("Re-submitted run", request, stage_dir, final_request_path)
    print_kv("previous", previous_status or "-")
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
        make_shared(run_dir, directory=True)
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
    command = resolve_itou_command()
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
        line = strip_ansi(raw_line).strip()
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


def probe_path(vm_name: str) -> Path:
    return CENTER_ROOT / "probes" / f"{safe_name(vm_name)}.json"


def probe_log_path(vm_name: str) -> Path:
    return CENTER_ROOT / "probes" / f"{safe_name(vm_name)}.log"


def probe_json_epoch(path: Path, payload: dict[str, Any] | None = None) -> int:
    if payload is None:
        try:
            payload = read_json(path)
        except Exception:
            payload = {}
    epoch = probe_payload_epoch(payload)
    if epoch:
        return epoch
    try:
        return int(path.stat().st_mtime)
    except FileNotFoundError:
        return 0


def probe_payload_epoch(payload: dict[str, Any]) -> int:
    return int(payload.get("finished_at_epoch") or payload.get("started_at_epoch") or 0)


def remove_probe_file(path: Path) -> None:
    try:
        payload = read_json(path)
        if payload.get("status") == "PROBING" and probe_is_running(payload):
            return
    except Exception:
        pass
    path.unlink(missing_ok=True)
    path.with_suffix(".log").unlink(missing_ok=True)


def prune_probe_files() -> None:
    ensure_layout()
    max_age = int(os.environ.get("ZHH_CENTER_PROBE_RETENTION_SECONDS", str(60 * 60)))
    max_files = int(os.environ.get("ZHH_CENTER_MAX_PROBE_FILES", "200"))
    now = int(time.time())
    keep: list[tuple[int, Path]] = []
    for path in (CENTER_ROOT / "probes").glob("*.json"):
        try:
            payload = read_json(path)
        except Exception:
            remove_probe_file(path)
            continue
        epoch = probe_json_epoch(path, payload)
        if payload.get("status") == "PROBING" and probe_is_running(payload):
            keep.append((epoch, path))
            continue
        if epoch and now - epoch > max_age:
            remove_probe_file(path)
            continue
        keep.append((epoch, path))
    if max_files > 0 and len(keep) > max_files:
        keep.sort(key=lambda item: item[0])
        for _, path in keep[: len(keep) - max_files]:
            remove_probe_file(path)


def bad_tpu_path(vm_name: str) -> Path:
    return CENTER_ROOT / "bad_tpus" / f"{safe_name(vm_name)}.json"


def bad_tpu_reason(vm_name: str) -> str:
    path = bad_tpu_path(vm_name)
    if not path.exists():
        return ""
    try:
        payload = read_json(path)
    except Exception:
        path.unlink(missing_ok=True)
        return ""
    bad_until = int(payload.get("bad_until", 0) or 0)
    if time.time() >= bad_until:
        path.unlink(missing_ok=True)
        return ""
    return str(payload.get("reason") or "probe failed")


def mark_tpu_bad(vm_name: str, zone: str, reason: str) -> None:
    cooldown = int(os.environ.get("ZHH_CENTER_BAD_TPU_COOLDOWN", str(10 * 60)))
    atomic_write_json(bad_tpu_path(vm_name), {
        "vm_name": vm_name,
        "zone": zone,
        "reason": reason,
        "bad_at": now_ts(),
        "bad_until": int(time.time()) + cooldown,
    })


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


def itou_inventory() -> list[dict[str, str]]:
    ensure_layout()
    items = []
    for item in run_itou():
        reason = bad_tpu_reason(item["vm_name"])
        if reason:
            items.append({**item, "available": "false", "reason": f"cooldown: {reason}", "checked_at": now_ts()})
        else:
            items.append({**item, "available": "candidate", "reason": "itou pre-check only", "checked_at": now_ts()})
    atomic_write_json(CENTER_ROOT / "inventory" / "latest.json", {"ts": now_ts(), "items": items, "available": []})
    return items


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
    classes = requirement_classes(vm_req or "auto")
    if classes and tpu.get("class") not in classes:
        return False
    sizes = [s.strip() for s in type_req.split(",") if s.strip()]
    if sizes and tpu.get("size") not in sizes:
        return False
    return True


def queue_ready_runs(runs: list[dict[str, Any]], now: int | None = None) -> list[dict[str, Any]]:
    now = int(time.time()) if now is None else now
    queue = [
        r for r in runs
        if r.get("status") in QUEUE_STATUSES and int(r.get("next_retry_at_epoch", 0) or 0) <= now
    ]
    queue.sort(key=lambda r: (-int(r.get("priority", 0)), str(r.get("submitted_at", "")), str(r.get("run_id", ""))))
    return queue


def probe_is_running(payload: dict[str, Any]) -> bool:
    pid = int(payload.get("pid", 0) or 0)
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def start_probe(tpu: dict[str, str]) -> bool:
    ensure_layout()
    vm_name = tpu["vm_name"]
    zone = tpu["zone"]
    path = probe_path(vm_name)
    if path.exists():
        try:
            payload = read_json(path)
            if payload.get("status") == "PROBING" and probe_is_running(payload):
                return False
            if payload.get("status") == "GOOD" and time.time() - int(payload.get("finished_at_epoch", 0) or 0) < int(os.environ.get("ZHH_CENTER_GOOD_TPU_TTL", "120")):
                return False
        except Exception:
            pass
    log_path = probe_log_path(vm_name)
    with log_path.open("a", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "probe-worker", "--vm-name", vm_name, "--zone", zone],
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
    atomic_write_json(path, {
        "vm_name": vm_name,
        "zone": zone,
        "class": tpu.get("class", ""),
        "size": tpu.get("size", ""),
        "status": "PROBING",
        "pid": proc.pid,
        "started_at": now_ts(),
        "log": str(log_path),
    })
    return True


def probe_worker(args: argparse.Namespace) -> int:
    ensure_layout()
    vm_name = args.vm_name
    zone = args.zone
    path = probe_path(vm_name)
    started = time.time()
    reason = ""
    status = "BAD"
    if has_center_lease(vm_name):
        reason = "center lease"
    elif not cloud_ready(vm_name, zone):
        reason = "not READY"
    elif os.environ.get("ZHH_CENTER_SKIP_PROBE_ENV_CHECK") == "1":
        status = "GOOD"
        reason = "ready (env check skipped)"
    else:
        command = " ".join(["exec", shlex.quote(str(SCRIPT_ROOT / "main.sh")), "center-probe", shlex.quote(vm_name), shlex.quote(zone)])
        try:
            proc = run_shell_as_worker_user(command, timeout=int(os.environ.get("ZHH_CENTER_PROBE_TIMEOUT", "240")))
            if proc.returncode == 0:
                status = "GOOD"
                reason = "ready"
            else:
                reason = (proc.stderr or proc.stdout or f"probe exited {proc.returncode}").strip().splitlines()[-1:]
                reason = reason[0] if reason else f"probe exited {proc.returncode}"
        except subprocess.TimeoutExpired:
            reason = "probe timeout"
    payload = {
        "vm_name": vm_name,
        "zone": zone,
        "class": parse_tpu_type(vm_name)[0],
        "size": parse_tpu_type(vm_name)[1],
        "status": status,
        "reason": reason,
        "started_at_epoch": int(started),
        "finished_at_epoch": int(time.time()),
        "finished_at": now_ts(),
        "pid": os.getpid(),
    }
    atomic_write_json(path, payload)
    if status != "GOOD":
        mark_tpu_bad(vm_name, zone, reason)
        return 1
    return 0


def good_probe_tpus(candidates: list[dict[str, str]]) -> list[dict[str, str]]:
    ttl = int(os.environ.get("ZHH_CENTER_GOOD_TPU_TTL", "120"))
    now = time.time()
    good: list[dict[str, str]] = []
    by_vm = {item["vm_name"]: item for item in candidates}
    for vm_name, item in by_vm.items():
        path = probe_path(vm_name)
        if not path.exists():
            continue
        try:
            payload = read_json(path)
        except Exception:
            continue
        if payload.get("status") != "GOOD":
            continue
        if now - int(payload.get("finished_at_epoch", 0) or 0) > ttl:
            continue
        if has_center_lease(vm_name) or bad_tpu_reason(vm_name):
            continue
        good.append(item)
    return good


def probe_summary_for_run(run: dict[str, Any]) -> str:
    probes: list[dict[str, Any]] = []
    for path in (CENTER_ROOT / "probes").glob("*.json"):
        try:
            payload = read_json(path)
        except Exception:
            continue
        if run_matches_tpu(run, {str(k): str(v) for k, v in payload.items()}):
            probes.append(payload)
    if not probes:
        return "waiting for matching TPU probe"
    probes.sort(key=probe_payload_epoch, reverse=True)
    ttl = int(os.environ.get("ZHH_CENTER_GOOD_TPU_TTL", "120"))
    now = int(time.time())
    for payload in probes:
        if payload.get("status") != "GOOD":
            continue
        epoch = probe_payload_epoch(payload)
        vm_name = str(payload.get("vm_name") or "")
        if epoch and now - epoch <= ttl and vm_name and not has_center_lease(vm_name) and not bad_tpu_reason(vm_name):
            return f"ready {format_tpu_text(payload)} ({fmt_age(epoch)} ago)"
    for payload in probes:
        if payload.get("status") == "PROBING" and probe_is_running(payload):
            epoch = probe_payload_epoch(payload)
            suffix = f" ({fmt_age(epoch)} ago)" if epoch else ""
            return f"checking {format_tpu_text(payload)}{suffix}"
    latest = probes[0]
    latest_status = str(latest.get("status") or "UNKNOWN")
    latest_reason = str(latest.get("reason") or "")
    latest_epoch = probe_payload_epoch(latest)
    latest_text = f"waiting; latest {format_tpu_text(latest)} {latest_status}"
    if latest_epoch:
        latest_text += f" {fmt_age(latest_epoch)} ago"
    if latest_reason:
        latest_text += f": {latest_reason}"
    return latest_text


def probe_candidates_for_queue(candidates: list[dict[str, str]], queue: list[dict[str, Any]]) -> list[dict[str, str]]:
    ordered: list[tuple[int, dict[str, str]]] = []
    for item in candidates:
        for idx, run in enumerate(queue):
            if run_matches_tpu(run, item):
                ordered.append((idx, item))
                break
    ordered.sort(key=lambda pair: (pair[0], pair[1].get("vm_name", "")))
    return [item for _, item in ordered]


def refresh_probe_pool(candidates: list[dict[str, str]], queue: list[dict[str, Any]]) -> int:
    started = 0
    max_new = int(os.environ.get("ZHH_CENTER_MAX_NEW_PROBES_PER_TICK", "4"))
    for item in probe_candidates_for_queue(candidates, queue):
        vm_name = item["vm_name"]
        if has_center_lease(vm_name) or bad_tpu_reason(vm_name):
            continue
        if start_probe(item):
            started += 1
            if started >= max_new:
                break
    return started


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
            if str(run.get("status") or "") == "STALE":
                requeue_stale_run(run, path, stale_reason_for_run(run, int(time.time())))
                run = read_json(path)
            if update_observations(run):
                atomic_write_json(path, run)
            runs.append(run)
        except Exception as exc:
            print(f"failed to read {path}: {exc}", file=sys.stderr)
    return runs


def resolve_run(prefix: str, quiet: bool = False) -> tuple[str, Path] | tuple[None, None]:
    ensure_layout()
    matches = []
    for path in (CENTER_ROOT / "runs").glob("*/run.json"):
        rid = path.parent.name
        if rid == prefix or rid.startswith(prefix):
            matches.append((rid, path))
    if not matches:
        if not quiet:
            print(f"run not found: {prefix}", file=sys.stderr)
        return None, None
    if len(matches) > 1:
        if not quiet:
            print(f"ambiguous run id prefix: {prefix}", file=sys.stderr)
            for rid, _ in matches:
                print(f"  {rid}", file=sys.stderr)
        return None, None
    return matches[0]


def resolve_pending_request(prefix: str) -> tuple[str, Path] | tuple[None, None]:
    ensure_layout()
    matches = []
    for path in (CENTER_ROOT / "inbox").glob("*.json"):
        try:
            rid = str(read_json(path).get("run_id") or "")
        except Exception:
            continue
        if rid == prefix or rid.startswith(prefix):
            matches.append((rid, path))
    if not matches:
        return None, None
    if len(matches) > 1:
        print(f"ambiguous pending run id prefix: {prefix}", file=sys.stderr)
        for rid, _ in matches:
            print(f"  {rid}", file=sys.stderr)
        return None, None
    return matches[0]


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
        make_shared(LEGACY_LOCK_ROOT, directory=True)
        lock_path.touch()
        make_shared(lock_path, directory=False)
    except PermissionError:
        command = f"mkdir -p {shlex.quote(str(LEGACY_LOCK_ROOT))} && chmod 777 {shlex.quote(str(LEGACY_LOCK_ROOT))} && touch {shlex.quote(str(lock_path))} && chmod 666 {shlex.quote(str(lock_path))}"
        run_shell(sudo_root_shell_command(command), timeout=15)


def release_lease(vm_name: str) -> None:
    lease = lease_path(vm_name)
    try:
        lease.unlink(missing_ok=True)
    except PermissionError:
        command = f"chmod 777 {shlex.quote(str(lease.parent))} && rm -f {shlex.quote(str(lease))}"
        proc = run_shell(sudo_root_shell_command(command), timeout=15)
        if proc.returncode != 0:
            print((proc.stderr or proc.stdout or f"failed to remove lease {lease}").strip(), file=sys.stderr)
    if LEGACY_LOCK_ROOT.exists():
        try:
            for path in LEGACY_LOCK_ROOT.glob(f"center_{vm_name}_*"):
                path.unlink(missing_ok=True)
        except PermissionError:
            command = " ".join([
                "chmod", "777", shlex.quote(str(LEGACY_LOCK_ROOT)), "&&",
                "find", shlex.quote(str(LEGACY_LOCK_ROOT)), "-maxdepth", "1", "-type", "f",
                "-name", shlex.quote(f"center_{vm_name}_*"), "-delete",
            ])
            run_shell(sudo_root_shell_command(command), timeout=15)


def launch_worker(run: dict[str, Any], tpu: dict[str, str]) -> bool:
    run_dir = CENTER_ROOT / "runs" / run["run_id"]
    run_path = run_dir / "run.json"
    env_file = write_worker_env(run_dir, run)
    session = tmux_name(run["run_id"])
    launch_log = run_dir / "worker_launch.log"
    extra_args = [str(x) for x in (run.get("extra_args") or [])]
    quoted_args = " ".join(shlex.quote(x) for x in extra_args)
    inner_command = (
        f"exec {shlex.quote(str(SCRIPT_ROOT / 'main.sh'))} center-worker "
        f"{shlex.quote(run['run_id'])} {shlex.quote(run['stage_dir'])} "
        f"{shlex.quote(tpu['vm_name'])} {shlex.quote(tpu['zone'])} -- {quoted_args}"
    )
    command = worker_user_shell_command(inner_command, env_file)
    command = " ".join([
        "set -o pipefail;",
        "{", "printf", shlex.quote(f"[center] worker start {now_ts()} run={run['run_id']} tpu={tpu['vm_name']} zone={tpu['zone']}\\n"), ";",
        command, ";", "} 2>&1 | tee -a", shlex.quote(str(launch_log)),
    ])
    create_lease(run, tpu)
    assigned_tpu = {"vm_name": tpu["vm_name"], "zone": tpu["zone"], "class": tpu.get("class", ""), "size": tpu.get("size", "")}
    for key in ("source", "session"):
        if tpu.get(key):
            assigned_tpu[key] = tpu[key]
    run.update({
        "status": "APPLYING",
        "assigned_tpu": assigned_tpu,
        "worker": {"tmux_session": session, "started_at": now_ts(), "host": os.uname().nodename, "launch_log": str(launch_log)},
        "worker_launch_log": str(launch_log),
    })
    attempts = list(run.get("attempts") or [])
    attempt = {"ts": now_ts(), "vm_name": tpu["vm_name"], "zone": tpu["zone"], "event": "launch"}
    for key in ("source", "session"):
        if tpu.get(key):
            attempt[key] = tpu[key]
    attempts.append(attempt)
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


def kill_tmux_session(session: str) -> None:
    if not session:
        return
    subprocess.run(["tmux", "kill-session", "-t", session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stale_reason_for_run(run: dict[str, Any], now: int) -> str:
    output_log = Path(str(run["output_log"])) if run.get("output_log") else None
    if output_log and output_log.exists():
        try:
            mtime = int(output_log.stat().st_mtime)
            return f"output log stale for {(now - mtime) // 60} min"
        except OSError:
            pass
    return str(run.get("last_error") or "run marked STALE; requeued for resume")


def requeue_stale_run(run: dict[str, Any], run_path: Path, reason: str) -> None:
    run_dir = run_path.parent
    worker = run.get("worker") or {}
    session = worker.get("tmux_session") if isinstance(worker, dict) else ""
    assigned = run.get("assigned_tpu") or {}
    vm_name = assigned.get("vm_name") if isinstance(assigned, dict) else ""
    zone = assigned.get("zone") if isinstance(assigned, dict) else ""
    kill_tmux_session(str(session or ""))
    if vm_name:
        release_lease(str(vm_name))
        if zone:
            mark_tpu_bad(str(vm_name), str(zone), reason)
    updated = now_ts()
    run["status"] = "RESUME_PENDING"
    run["last_error"] = reason
    run["assigned_tpu"] = None
    run["worker"] = None
    run["worker_missing_count"] = 0
    run["next_retry_at_epoch"] = None
    run["next_retry_at"] = None
    run["current_stage"] = "Waiting to resume after stale output"
    run["updated_at"] = updated
    run["current_stage_at"] = updated
    run["current_stage_epoch"] = int(time.time())
    atomic_write_json(run_path, run)
    append_event(run_dir, "stale_requeued", reason=reason, tmux_session=str(session or ""), vm_name=str(vm_name or ""), zone=str(zone or ""))


def reconcile_active_runs() -> int:
    changed = 0
    stale_seconds = int(os.environ.get("ZHH_CENTER_STALE_SECONDS", str(30 * 60)))
    now = int(time.time())
    for run in load_runs():
        status = str(run.get("status") or "")
        if status not in ("APPLYING", "RUNNING"):
            continue
        run_dir = CENTER_ROOT / "runs" / run["run_id"]
        run_path = run_dir / "run.json"
        worker = run.get("worker") or {}
        session = worker.get("tmux_session") if isinstance(worker, dict) else ""
        assigned = run.get("assigned_tpu") or {}
        vm_name = assigned.get("vm_name") if isinstance(assigned, dict) else ""
        zone = assigned.get("zone") if isinstance(assigned, dict) else ""
        if not tmux_session_exists(str(session)):
            if vm_name:
                release_lease(str(vm_name))
            missing_count = int(run.get("worker_missing_count", 0) or 0) + 1
            max_missing = int(os.environ.get("ZHH_CENTER_MAX_WORKER_MISSING_RETRIES", "3"))
            reason = "worker tmux session exited before reporting status"
            if missing_count >= max_missing:
                trust_action = record_trusted_failure(run, reason)
                if trust_action == "resume":
                    if vm_name and zone:
                        mark_tpu_bad(str(vm_name), str(zone), f"trusted failure: {reason}")
                    run["status"] = "RESUME_PENDING"
                    run["worker_missing_count"] = 0
                    run["current_stage"] = f"Waiting to resume ({trust_summary(run)})"
                else:
                    if trust_action == "exhausted" and vm_name and zone:
                        mark_tpu_bad(str(vm_name), str(zone), f"trusted failure: {reason}")
                    run["status"] = "FAILED"
                    run["current_stage"] = "Failed" if trust_action == "not_trusted" else f"Failed ({trust_summary(run)})"
                run["next_retry_at_epoch"] = None
                run["next_retry_at"] = None
            else:
                delay = min(300, 30 * missing_count)
                run["status"] = "INFRA_RETRY"
                run["next_retry_at_epoch"] = now + delay
                run["next_retry_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + delay))
            run["assigned_tpu"] = None
            run["worker"] = None
            if run.get("status") != "RESUME_PENDING":
                run["worker_missing_count"] = missing_count
            run["last_error"] = reason
            if run.get("status") not in ("FAILED", "RESUME_PENDING"):
                run["current_stage"] = "Worker exited before reporting status"
            run["updated_at"] = now_ts()
            run["current_stage_at"] = run["updated_at"]
            run["current_stage_epoch"] = now
            atomic_write_json(run_path, run)
            append_event(run_dir, "worker_missing", tmux_session=session, count=missing_count, status=run["status"])
            changed += 1
            continue
        output_log = Path(run["output_log"]) if run.get("output_log") else None
        if output_log and output_log.exists():
            mtime = int(output_log.stat().st_mtime)
            if status == "RUNNING" and now - mtime >= stale_seconds:
                requeue_stale_run(run, run_path, f"output log stale for {(now - mtime) // 60} min")
                changed += 1
    return changed


def schedule_once_unlocked(verbose: bool = False) -> int:
    prune_probe_files()
    runs = load_runs()
    now = int(time.time())
    queue = queue_ready_runs(runs, now)
    candidates = [item for item in itou_inventory() if item.get("available") == "candidate"]
    if not queue:
        return 0
    matching_candidates = probe_candidates_for_queue(candidates, queue)
    refresh_probe_pool(candidates, queue)
    tpus = good_probe_tpus(candidates)
    scheduled = 0
    for run in queue:
        matches = [t for t in tpus if run_matches_tpu(run, t)]
        if not matches:
            continue
        choice = random.choice(matches)
        if launch_worker(run, choice):
            scheduled += 1
            tpus = [t for t in tpus if t["vm_name"] != choice["vm_name"]]
    if verbose:
        top = queue[0]
        print_kv("schedule", f"queued={len(queue)} candidates={len(candidates)} matching={len(matching_candidates)} ready={len(tpus) + scheduled} scheduled={scheduled}")
        print_kv("top_run", f"{top.get('run_id', '-')} {format_tpu_text(top.get('requirements') if isinstance(top.get('requirements'), dict) else None)}")
        print_kv("top_probe", probe_summary_for_run(top))
    return scheduled


def schedule_once(verbose: bool = False) -> int:
    ensure_layout()
    lock_path = CENTER_ROOT / "schedule.lock"
    with lock_path.open("w", encoding="utf-8") as lock_file:
        make_shared(lock_path, directory=False)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        return schedule_once_unlocked(verbose=verbose)


def apply_ready(args: argparse.Namespace) -> int:
    ensure_layout()
    tpu_class, tpu_size = parse_tpu_type(args.tpu_type or args.vm_name)
    tpu = {
        "vm_name": args.vm_name,
        "zone": args.zone,
        "class": tpu_class,
        "size": tpu_size,
        "source": "apply",
        "session": args.session,
    }
    if not tpu["class"] or not tpu["size"]:
        print(f"failed to parse TPU type from {args.tpu_type or args.vm_name}", file=sys.stderr)
        return 1
    lock_path = CENTER_ROOT / "schedule.lock"
    with lock_path.open("w", encoding="utf-8") as lock_file:
        make_shared(lock_path, directory=False)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        if has_center_lease(args.vm_name):
            print(f"apply shortcut skipped: {args.vm_name} already has center lease")
            return 2
        queue = queue_ready_runs(load_runs())
        for run in queue:
            if not run_matches_tpu(run, tpu):
                continue
            if launch_worker(run, tpu):
                append_event(CENTER_ROOT / "runs" / run["run_id"], "apply_shortcut_assigned", vm_name=args.vm_name, zone=args.zone, session=args.session)
                print(f"apply shortcut assigned {args.vm_name} @ {args.zone} to {run['run_id']}")
                return 0
            print(f"apply shortcut failed to launch worker for {run['run_id']}", file=sys.stderr)
            return 1
    print(f"apply shortcut found no matching queued run for {format_tpu_text(tpu)}")
    return 2


def worker_log_dir(args: argparse.Namespace) -> int:
    run_dir = CENTER_ROOT / "runs" / args.run_id
    run_path = run_dir / "run.json"
    if not run_path.exists():
        return 0
    run = read_json(run_path)
    log_dir = str(Path(args.log_dir).resolve())
    run["current_log_dir"] = log_dir
    run["output_log"] = str(Path(log_dir) / "output.log")
    run["status"] = "RUNNING"
    update_observations(run)
    atomic_write_json(run_path, run)
    append_event(run_dir, "log_dir", log_dir=log_dir)
    return 0


def worker_stage(args: argparse.Namespace) -> int:
    run_dir = CENTER_ROOT / "runs" / args.run_id
    run_path = run_dir / "run.json"
    if not run_path.exists():
        return 0
    run = read_json(run_path)
    run["current_stage"] = str(args.stage)
    run["current_stage_at"] = now_ts()
    run["current_stage_epoch"] = int(time.time())
    if args.log_file:
        run["current_stage_log"] = str(Path(args.log_file).expanduser())
    if args.detail:
        run["current_stage_detail"] = str(args.detail)
    atomic_write_json(run_path, run)
    append_event(run_dir, "worker_stage", stage=str(args.stage), log_file=str(args.log_file or ""), detail=str(args.detail or ""))
    return 0


def worker_finished(args: argparse.Namespace) -> int:
    run_dir = CENTER_ROOT / "runs" / args.run_id
    run_path = run_dir / "run.json"
    if not run_path.exists():
        return 0
    run = read_json(run_path)
    output_log = Path(run["output_log"]) if run.get("output_log") else None
    status, reason = classify_failure(int(args.exit_code), output_log)
    assigned = run.get("assigned_tpu") or {}
    vm_name = assigned.get("vm_name") if isinstance(assigned, dict) else ""
    zone = assigned.get("zone") if isinstance(assigned, dict) else ""
    if vm_name:
        release_lease(str(vm_name))
    run["last_error"] = None if status == "FINISHED" else reason
    if status == "INFRA_RETRY":
        if vm_name and zone:
            mark_tpu_bad(str(vm_name), str(zone), reason)
        run["status"] = "INFRA_RETRY"
        run["assigned_tpu"] = None
        run["worker"] = None
        run["current_stage"] = "Waiting for retry"
    elif status == "FAILED" and (trust_action := record_trusted_failure(run, reason)) != "not_trusted":
        if vm_name and zone:
            mark_tpu_bad(str(vm_name), str(zone), f"trusted failure: {reason}")
        if trust_action == "resume":
            run["status"] = "RESUME_PENDING"
            run["assigned_tpu"] = None
            run["worker"] = None
            run["next_retry_at_epoch"] = None
            run["next_retry_at"] = None
            run["current_stage"] = f"Waiting to resume ({trust_summary(run)})"
        else:
            run["status"] = "FAILED"
            run["current_stage"] = f"Failed ({trust_summary(run)})"
    else:
        run["status"] = status
        run["current_stage"] = "Finished" if status == "FINISHED" else "Failed"
    run["updated_at"] = now_ts()
    run["current_stage_at"] = run["updated_at"]
    run["current_stage_epoch"] = int(time.time())
    update_observations(run)
    atomic_write_json(run_path, run)
    append_event(run_dir, "worker_finished", exit_code=int(args.exit_code), status=run["status"], reason=reason)
    return 0


def cancel_run(run_id: str, no_kill: bool = False, verbose: bool = True) -> tuple[bool, str, Path | None]:
    rid, run_path = resolve_run(run_id)
    if not rid or not run_path:
        return False, "", None
    run_dir = run_path.parent
    run = read_json(run_path)
    worker = run.get("worker") or {}
    session = worker.get("tmux_session") if isinstance(worker, dict) else ""
    if session:
        subprocess.run(["tmux", "kill-session", "-t", str(session)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assigned = run.get("assigned_tpu") or {}
    run["status"] = "CANCELLED"
    run["last_error"] = "cancelled by user"
    run["updated_at"] = now_ts()
    atomic_write_json(run_path, run)
    append_event(run_dir, "cancelled", kill_tpu=not no_kill)
    if isinstance(assigned, dict) and assigned.get("vm_name") and assigned.get("zone") and not no_kill:
        kill_command = " ".join([
            "exec", shlex.quote(str(SCRIPT_ROOT / "main.sh")), "kill",
            shlex.quote(str(assigned["vm_name"])), shlex.quote(str(assigned["zone"])),
        ])
        try:
            proc = run_shell(worker_user_shell_command(kill_command), timeout=180)
            if proc.returncode != 0 and verbose:
                print((proc.stderr or proc.stdout or f"kill_tpu exited {proc.returncode}").strip(), file=sys.stderr)
        except subprocess.TimeoutExpired:
            run["last_error"] = "cancelled, but TPU kill timed out"
            atomic_write_json(run_path, run)
            append_event(run_dir, "kill_timeout", vm_name=assigned["vm_name"], zone=assigned["zone"])
            print(f"TPU kill timed out for {assigned['vm_name']}@{assigned['zone']}", file=sys.stderr)
            return False, rid, run_path
        release_lease(str(assigned["vm_name"]))
    if verbose:
        print(f"cancelled {rid}")
    return True, rid, run_path


def cancel(args: argparse.Namespace) -> int:
    ok, _, _ = cancel_run(args.run_id, no_kill=args.no_kill, verbose=True)
    return 0 if ok else 1


def unlink_log_file(path: Path) -> bool:
    try:
        if path.exists() and (path.is_file() or path.is_symlink()):
            path.unlink()
            return True
    except PermissionError:
        proc = run_shell(sudo_root_shell_command(f"rm -f {shlex.quote(str(path))}"), timeout=15)
        return proc.returncode == 0 and not path.exists()
    return False


def prune_empty_dirs(root: Path) -> None:
    if not root.exists() or not root.is_dir():
        return
    for path in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


def cleanup_deleted_run_logs(run: dict[str, Any], run_dir: Path) -> int:
    removed = 0
    explicit_keys = ("worker_launch_log", "current_stage_log")
    explicit_paths: set[Path] = set()
    for key in explicit_keys:
        value = run.get(key)
        if value:
            explicit_paths.add(Path(str(value)).expanduser())
    worker = run.get("worker") or {}
    if isinstance(worker, dict) and worker.get("launch_log"):
        explicit_paths.add(Path(str(worker["launch_log"])).expanduser())

    for path in explicit_paths:
        if unlink_log_file(path):
            removed += 1

    stage_dir_text = str(run.get("stage_dir") or "")
    logs_root = Path(stage_dir_text).expanduser() / "logs" if stage_dir_text else None
    if logs_root and logs_root.exists() and logs_root.is_dir():
        for path in logs_root.rglob("*.log"):
            if path.name == "output.log":
                continue
            if unlink_log_file(path):
                removed += 1
        prune_empty_dirs(logs_root)

    if run_dir.exists():
        for path in run_dir.rglob("*.log"):
            if unlink_log_file(path):
                removed += 1
    return removed


def delete_pending_requests_for_run_id(run_id: str) -> int:
    removed = 0
    for path in (CENTER_ROOT / "inbox").glob("*.json"):
        try:
            if str(read_json(path).get("run_id") or "") != run_id:
                continue
            path.unlink(missing_ok=True)
            removed += 1
        except Exception:
            continue
    return removed


def delete_run_path(rid: str, run_path: Path, verbose: bool = True) -> tuple[bool, str, int, Path | None]:
    run_dir = run_path.parent
    run = read_json(run_path)
    status = str(run.get("status") or "")
    assigned = run.get("assigned_tpu") or {}
    if status == "FINISHED":
        if isinstance(assigned, dict) and assigned.get("vm_name"):
            release_lease(str(assigned["vm_name"]))
    else:
        ok, rid, run_path = cancel_run(rid, no_kill=False, verbose=False)
        if not ok:
            print(f"cancel failed; keeping run {rid}", file=sys.stderr)
            return False, status, 0, run_dir
        if run_path is None:
            return False, status, 0, None
        run_dir = run_path.parent
        run = read_json(run_path)

    removed_logs = cleanup_deleted_run_logs(run, run_dir)
    try:
        shutil.rmtree(run_dir)
    except PermissionError:
        command = f"rm -rf {shlex.quote(str(run_dir))}"
        proc = run_shell(sudo_root_shell_command(command), timeout=30)
        if proc.returncode != 0:
            print((proc.stderr or proc.stdout or f"failed to remove {run_dir}").strip(), file=sys.stderr)
            return False, status, removed_logs, run_dir
    if verbose:
        print(f"Deleted run {ctext(BOLD, rid)}")
        print_kv("previous", status or "-")
        print_kv("cleaned_logs", removed_logs)
        print_kv("removed", run_dir)
    return True, status, removed_logs, run_dir


def delete(args: argparse.Namespace) -> int:
    rid, run_path = resolve_run(args.run_id, quiet=True)
    if not rid or not run_path:
        pending_rid, request_path = resolve_pending_request(args.run_id)
        if pending_rid and request_path:
            request_path.unlink(missing_ok=True)
            print(f"Deleted pending run {ctext(BOLD, pending_rid)}")
            print_kv("request", request_path)
            return 0
        print(f"run not found: {args.run_id}", file=sys.stderr)
        return 1

    ok, _, _, _ = delete_run_path(rid, run_path, verbose=True)
    if not ok:
        return 1
    return 0


def remote_run_config_path(stage_dir: Path) -> Path:
    candidates = [
        stage_dir / "configs" / "remote_run_config.yml",
        stage_dir / "configs" / "remote_run_configs.yml",
    ]
    for path in candidates:
        if path.exists():
            return path
    raise ValueError(f"remote run config not found under {stage_dir / 'configs'}")


def replace_training_num_steps(text: str, old_steps: int, new_steps: int) -> str:
    lines = text.splitlines(keepends=True)
    in_training = False
    training_indent = 0
    for idx, raw_line in enumerate(lines):
        line_without_newline = raw_line.rstrip("\r\n")
        code = line_without_newline.split("#", 1)[0].rstrip()
        if not code.strip():
            continue
        indent = len(code) - len(code.lstrip(" "))
        stripped = code.strip()
        if not in_training:
            if stripped == "training:":
                in_training = True
                training_indent = indent
            continue
        if indent <= training_indent:
            break
        match = re.match(r"^(\s*num_steps\s*:\s*)([-+]?\d+)([^\r\n]*)(\r?\n?)$", raw_line)
        if not match:
            continue
        found_steps = int(match.group(2))
        if found_steps != old_steps:
            raise ValueError(f"parsed num_steps={old_steps}, but config text contains {found_steps}")
        lines[idx] = f"{match.group(1)}{new_steps}{match.group(3)}{match.group(4)}"
        return "".join(lines)
    raise ValueError("training.num_steps not found in remote run config text")


def bump_remote_num_steps(config_path: Path, extra_steps: int) -> tuple[int, int]:
    text = config_path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
    except Exception as exc:
        raise ValueError(f"PyYAML is required to parse {config_path}: {exc}") from exc
    try:
        data = yaml.safe_load(text) or {}
    except Exception as exc:
        raise ValueError(f"failed to parse {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"remote run config must be a mapping: {config_path}")
    if data.get("eval_only") is not False:
        raise ValueError("continue expects eval_only: false")
    training = data.get("training")
    if not isinstance(training, dict):
        raise ValueError("continue expects training config")
    old_steps = training.get("num_steps")
    if isinstance(old_steps, bool) or not isinstance(old_steps, int):
        raise ValueError("continue expects integer training.num_steps")
    new_steps = old_steps + extra_steps
    mode = config_path.stat().st_mode & 0o777
    atomic_write_text(config_path, replace_training_num_steps(text, old_steps, new_steps), mode=mode)
    return old_steps, new_steps


def continue_run(args: argparse.Namespace) -> int:
    rid, run_path = resolve_run(args.run_id)
    if not rid or not run_path:
        return 1
    try:
        extra_steps = int(args.steps)
    except (TypeError, ValueError):
        print(f"steps must be a positive integer: {args.steps}", file=sys.stderr)
        return 1
    if extra_steps <= 0:
        print(f"steps must be a positive integer: {args.steps}", file=sys.stderr)
        return 1

    run_dir = run_path.parent
    run = read_json(run_path)
    status = str(run.get("status") or "")
    if status != "FINISHED":
        print(f"continue expects a FINISHED run; {rid} is {status or '-'}", file=sys.stderr)
        return 1
    stage_dir = Path(str(run.get("stage_dir") or "")).expanduser()
    if not stage_dir.exists() or not stage_dir.is_dir():
        print(f"stage dir not found: {stage_dir}", file=sys.stderr)
        return 1
    try:
        config_path = remote_run_config_path(stage_dir)
        old_steps, new_steps = bump_remote_num_steps(config_path, extra_steps)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    assigned = run.get("assigned_tpu") or {}
    vm_name = assigned.get("vm_name") if isinstance(assigned, dict) else ""
    if vm_name:
        release_lease(str(vm_name))
    worker = run.get("worker") or {}
    session = worker.get("tmux_session") if isinstance(worker, dict) else ""
    kill_tmux_session(str(session or ""))

    updated = now_ts()
    continuation = {
        "ts": updated,
        "by": getpass.getuser(),
        "config": str(config_path),
        "extra_steps": extra_steps,
        "num_steps_before": old_steps,
        "num_steps_after": new_steps,
    }
    existing_continuations = run.get("continuations")
    continuations = list(existing_continuations) if isinstance(existing_continuations, list) else []
    continuations.append(continuation)
    run["continuations"] = continuations
    run["status"] = "RESUME_PENDING"
    run["last_error"] = None
    run["assigned_tpu"] = None
    run["worker"] = None
    run["worker_missing_count"] = 0
    run["next_retry_at_epoch"] = None
    run["next_retry_at"] = None
    run["trusted"] = False
    run["trusted_at"] = None
    run["trusted_by"] = None
    run["trust_failed_count"] = 0
    run["trust_failure_limit"] = trust_failure_limit()
    run["trust_exhausted_at"] = None
    run["trust_last_failure_at"] = None
    run["trust_last_failure_reason"] = None
    run["current_stage"] = f"Waiting to continue (+{extra_steps} steps; num_steps {old_steps}->{new_steps})"
    run["current_stage_at"] = updated
    run["current_stage_epoch"] = int(time.time())
    run["updated_at"] = updated
    atomic_write_json(run_path, run)
    append_event(run_dir, "continued", **continuation)

    print(f"Continuing run {ctext(BOLD, rid)}")
    print_kv("status", format_status(run["status"]))
    print_kv("num_steps", f"{old_steps} -> {new_steps} (+{extra_steps})")
    print_kv("config", config_path)
    print_kv("tpu", format_tpu_text(run.get("requirements") if isinstance(run.get("requirements"), dict) else None))
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


def fmt_until(ts: int | None) -> str:
    if not ts:
        return "-"
    delta = max(0, int(ts) - int(time.time()))
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


def format_status(status: object) -> str:
    text = str(status or "-")
    if text in ("FINISHED", "RUNNING"):
        return ctext(GREEN + BOLD, text)
    if text in ("FAILED", "CANCELLED"):
        return ctext(RED + BOLD, text)
    if text in ("INFRA_RETRY",):
        return ctext(YELLOW + BOLD, text)
    if text in ("APPLYING", "QUEUED", "RESUME_PENDING"):
        return ctext(BLUE + BOLD, text)
    return text


def trust(args: argparse.Namespace) -> int:
    rid, run_path = resolve_run(args.run_id)
    if not rid or not run_path:
        return 1
    run_dir = run_path.parent
    run = read_json(run_path)
    status = str(run.get("status") or "")
    if status != "FAILED":
        print(f"trust expects a FAILED run; {rid} is {status or '-'}", file=sys.stderr)
        return 1

    assigned = run.get("assigned_tpu") or {}
    vm_name = assigned.get("vm_name") if isinstance(assigned, dict) else ""
    zone = assigned.get("zone") if isinstance(assigned, dict) else ""
    if vm_name:
        release_lease(str(vm_name))
    if vm_name and zone:
        mark_tpu_bad(str(vm_name), str(zone), "trusted by user; try another TPU")

    limit = trust_failure_limit()
    run["trusted"] = True
    run["trusted_at"] = now_ts()
    run["trusted_by"] = getpass.getuser()
    run["trust_generation"] = int(run.get("trust_generation", 0) or 0) + 1
    run["trust_failed_count"] = 0
    run["trust_failure_limit"] = limit
    run["trust_exhausted_at"] = None
    run["trust_last_failure_at"] = None
    run["trust_last_failure_reason"] = None
    run["status"] = "RESUME_PENDING"
    run["assigned_tpu"] = None
    run["worker"] = None
    run["worker_missing_count"] = 0
    run["next_retry_at_epoch"] = None
    run["next_retry_at"] = None
    run["current_stage"] = "Waiting to resume"
    run["current_stage_at"] = now_ts()
    run["current_stage_epoch"] = int(time.time())
    run["updated_at"] = now_ts()
    atomic_write_json(run_path, run)
    append_event(run_dir, "trusted", previous_status=status, limit=limit, vm_name=vm_name, zone=zone)

    print(f"Trusted run {ctext(BOLD, rid)}")
    print_kv("status", format_status(run["status"]))
    print_kv("trust", trust_summary(run))
    print_kv("tpu", format_tpu_text(run.get("requirements") if isinstance(run.get("requirements"), dict) else None))
    return 0


def status(_: argparse.Namespace) -> int:
    prune_probe_files()
    runs = load_runs()
    status_order = {name: i for i, name in enumerate(("RUNNING", "APPLYING", "INFRA_RETRY", "RESUME_PENDING", "QUEUED", "FAILED", "FINISHED", "CANCELLED"))}
    active_statuses = {"RUNNING", "APPLYING"}
    runs.sort(key=lambda r: (
        -int(r.get("priority", 0)),
        0 if str(r.get("status") or "") in active_statuses else 1,
        status_order.get(str(r.get("status")), 99),
        str(r.get("submitted_at", "")),
    ))
    if not runs:
        print("No centralized runs found.")
        return 0
    print(f"TPU center root: {CENTER_ROOT}")
    for run in runs:
        tpu = run.get("assigned_tpu") or run.get("requirements") or {}
        tpu_text = format_tpu_text(tpu if isinstance(tpu, dict) else None)
        desc = str(run.get("description") or "-").replace("\n", " ")
        print()
        print(f"{ctext(BOLD, str(run.get('run_id', '-')))} · {ctext(BOLD, desc)}")
        print_kv("status", format_status(run.get("status", "-")), indent="\t")
        print_kv("priority", int(run.get("priority", 0)), indent="\t")
        print_kv("tpu", tpu_text, indent="\t")
        trust_text = trust_summary(run)
        if trust_text:
            print_kv("trust", trust_text, indent="\t")
        if run.get("current_stage"):
            stage_text = str(run["current_stage"])
            if run.get("current_stage_epoch"):
                stage_text += f" ({fmt_age(int(run['current_stage_epoch']))} ago)"
            print_kv("stage", stage_text, indent="\t")
        if run.get("status") in QUEUE_STATUSES:
            print_kv("probe", probe_summary_for_run(run), indent="\t")
        print_kv("last_log", fmt_age(run.get("last_log_mtime")), indent="\t")
        if run.get("wandb_url"):
            print_kv("wandb", run["wandb_url"], indent="\t")
        if run.get("current_log_dir"):
            print_kv("log_dir", run["current_log_dir"], indent="\t")
        if run.get("last_error"):
            print_kv("last_error", run["last_error"], indent="\t")
        if run.get("next_retry_at_epoch"):
            print_kv("retry_in", fmt_until(int(run["next_retry_at_epoch"])), indent="\t")
        if run.get("worker_launch_log"):
            print_kv("worker_log", run["worker_launch_log"], indent="\t")
        print_kv("stage_dir", run.get("stage_dir", "-"), indent="\t")
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
        if args.check:
            available_tpus()
        else:
            itou_inventory()
        payload = read_json(CENTER_ROOT / "inventory" / "latest.json")
        items = payload.get("items", [])
        suffix = "with full checks" if args.check else "from itou pre-check"
        print(f"TPU inventory refreshed at {payload.get('ts', '-')} ({suffix})")
    if not items:
        print("No TPU candidates found.")
        return 0
    print(f"{'STATE':<10} {'TYPE':<8} {'VM_NAME':<48} {'ZONE':<18} REASON")
    for item in items:
        if item.get("available") == "true":
            state = "free"
        elif item.get("available") == "candidate":
            state = "candidate"
        else:
            state = "skip"
        typ = f"{item.get('class', '')}-{item.get('size', '')}".strip("-")
        print(f"{state:<10} {typ:<8} {item.get('vm_name', '-'):<48} {item.get('zone', '-'):<18} {item.get('reason', '')}")
    return 0


def change(args: argparse.Namespace) -> int:
    rid, run_path = resolve_run(args.run_id)
    if not rid or not run_path:
        return 1
    run = read_json(run_path)
    req = dict(run.get("requirements") or {})
    print(f"Editing run {ctext(BOLD, rid)}")
    print("Press Enter to keep the current value.")
    fields = (
        ("VM_NAME", "vm_name"),
        ("ZONE", "zone"),
        ("TPU_TYPES", "tpu_types"),
    )
    changed: dict[str, str] = {}
    for title, key in fields:
        current = str(req.get(key) or "")
        value = input(f"{DIM}{title} [{current}]: {RESET}").strip()
        if value:
            req[key] = value
            changed[key] = value
    current_priority = int(run.get("priority", 0) or 0)
    priority_value = input(f"{DIM}PRIORITY [{current_priority}]: {RESET}").strip()
    priority_changed = False
    if priority_value:
        if not re.match(r"^-?[0-9]+$", priority_value):
            print(f"priority must be an integer: {priority_value}", file=sys.stderr)
            return 1
        new_priority = int(priority_value)
        if new_priority != current_priority:
            run["priority"] = new_priority
            priority_changed = True
    if not changed:
        if not priority_changed:
            print("No changes.")
            return 0
    run["requirements"] = req
    run["updated_at"] = now_ts()
    atomic_write_json(run_path, run)
    event_fields: dict[str, Any] = {**changed}
    if priority_changed:
        event_fields["priority"] = run["priority"]
    append_event(run_path.parent, "run_changed", **event_fields)
    print(f"Updated {ctext(BOLD, rid)}")
    for _, key in fields:
        print_kv(key, req.get(key, ""))
    print_kv("priority", run.get("priority", 0))
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
                scheduled = schedule_once(verbose=args.verbose)
                if scheduled and not args.quiet:
                    print(f"scheduled {scheduled} run(s)")
            load_runs()
            time.sleep(args.interval)


def tick(args: argparse.Namespace) -> int:
    ingest_once(verbose=not args.quiet)
    reconciled = reconcile_active_runs()
    scheduled = 0 if args.no_schedule else schedule_once(verbose=not args.quiet)
    if not args.quiet:
        print(f"reconciled {reconciled} active run(s)")
        print(f"scheduled {scheduled} run(s)")
    return 0


def table(args: argparse.Namespace) -> int:
    interval = max(0.1, float(args.interval))
    use_alt_screen = sys.stdout.isatty() and not args.once
    if use_alt_screen:
        print("\033[?1049h\033[?25l", end="")
    try:
        while True:
            if use_alt_screen:
                print("\033[H\033[2J", end="")
            elif not args.once:
                print("\033[2J\033[H", end="")
            print(ctext(DIM, f"updated {now_ts()} | refresh every {interval:g}s | Ctrl-C to exit"))
            status(args)
            sys.stdout.flush()
            if args.once:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
        return 130
    finally:
        if use_alt_screen:
            print("\033[?25h\033[?1049l", end="")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zhh center", description="Centralized TPU distribution center")
    sub = parser.add_subparsers(dest="command", required=True)

    p_submit = sub.add_parser("submit-staged", help="submit an already staged directory via inbox")
    p_submit.add_argument("--stage-dir", required=True)
    p_submit.add_argument("--priority", type=int, default=0)
    p_submit.add_argument("--cwd", default="")
    p_submit.add_argument("extra_args", nargs=argparse.REMAINDER)
    p_submit.set_defaults(func=submit)

    p_submit_replace = sub.add_parser("submit-replace-staged", help="replace an existing run id with an already staged directory")
    p_submit_replace.add_argument("--run-id", required=True)
    p_submit_replace.add_argument("--stage-dir", required=True)
    p_submit_replace.add_argument("--cwd", default="")
    p_submit_replace.add_argument("--force", action="store_true")
    p_submit_replace.add_argument("extra_args", nargs=argparse.REMAINDER)
    p_submit_replace.set_defaults(func=submit_replace)

    p_ingest = sub.add_parser("ingest-once", help="ingest inbox requests once")
    p_ingest.set_defaults(func=lambda args: 0 if ingest_once(verbose=True) >= 0 else 1)

    p_tick = sub.add_parser("tick", help="ingest and schedule once")
    p_tick.add_argument("--quiet", action="store_true")
    p_tick.add_argument("--no-schedule", action="store_true")
    p_tick.set_defaults(func=tick)

    p_status = sub.add_parser("s", aliases=["status"], help="show centralized run status")
    p_status.set_defaults(func=status)

    p_table = sub.add_parser("table", help="refresh centralized run status repeatedly")
    p_table.add_argument("--interval", type=float, default=3.0)
    p_table.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    p_table.set_defaults(func=table)

    p_tpus = sub.add_parser("tpus", help="show center TPU inventory")
    p_tpus.add_argument("--cached", action="store_true")
    p_tpus.add_argument("--check", action="store_true", help="run slow cloud/remote checks after itou")
    p_tpus.set_defaults(func=tpus)

    p_start = sub.add_parser("start", help="start the center daemon loop")
    p_start.add_argument("--interval", type=float, default=5.0)
    p_start.add_argument("--quiet", action="store_true")
    p_start.add_argument("--verbose", action="store_true", help="print per-tick scheduling diagnostics")
    p_start.add_argument("--no-schedule", action="store_true")
    p_start.set_defaults(func=start)

    p_log = sub.add_parser("worker-log-dir", help=argparse.SUPPRESS)
    p_log.add_argument("--run-id", required=True)
    p_log.add_argument("--log-dir", required=True)
    p_log.set_defaults(func=worker_log_dir)

    p_stage = sub.add_parser("worker-stage", help=argparse.SUPPRESS)
    p_stage.add_argument("--run-id", required=True)
    p_stage.add_argument("--stage", required=True)
    p_stage.add_argument("--log-file", default="")
    p_stage.add_argument("--detail", default="")
    p_stage.set_defaults(func=worker_stage)

    p_done = sub.add_parser("worker-finished", help=argparse.SUPPRESS)
    p_done.add_argument("--run-id", required=True)
    p_done.add_argument("--exit-code", type=int, required=True)
    p_done.set_defaults(func=worker_finished)

    p_probe = sub.add_parser("probe-worker", help=argparse.SUPPRESS)
    p_probe.add_argument("--vm-name", required=True)
    p_probe.add_argument("--zone", required=True)
    p_probe.set_defaults(func=probe_worker)

    p_apply_ready = sub.add_parser("apply-ready", help=argparse.SUPPRESS)
    p_apply_ready.add_argument("--vm-name", required=True)
    p_apply_ready.add_argument("--zone", required=True)
    p_apply_ready.add_argument("--tpu-type", required=True)
    p_apply_ready.add_argument("--session", default="")
    p_apply_ready.set_defaults(func=apply_ready)

    p_cancel = sub.add_parser("cancel", help="cancel a run and kill its TPU if assigned")
    p_cancel.add_argument("run_id")
    p_cancel.add_argument("--no-kill", action="store_true")
    p_cancel.set_defaults(func=cancel)

    p_delete = sub.add_parser("delete", help="cancel if needed and remove a submitted run from center")
    p_delete.add_argument("run_id")
    p_delete.set_defaults(func=delete)

    p_continue = sub.add_parser("continue", help="continue a FINISHED training run for more steps")
    p_continue.add_argument("run_id")
    p_continue.add_argument("steps")
    p_continue.set_defaults(func=continue_run)

    p_trust = sub.add_parser("trust", help="trust a FAILED run and queue it for resume on another TPU")
    p_trust.add_argument("run_id")
    p_trust.set_defaults(func=trust)

    p_change = sub.add_parser("change", help="interactively edit TPU requirements for a run")
    p_change.add_argument("run_id")
    p_change.set_defaults(func=change)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
