#!/usr/bin/env python3
"""
Lightweight HTTP server for managing zhh jobs in tmux windows.

Usage:
    python server.py [--port PORT] [--host HOST]

API:
    POST /run - Start a new job
        Body: {"args": "optional zhh arguments", "cwd": "/path/to/working/dir"}
        Response: {"job_id": "...", "status": "running"}
    
    GET /status - Get all jobs status
        Response: {"jobs": [...]}
    
    GET /status/<job_id> - Get specific job status
        Response: {"job_id": "...", "status": "running|completed|failed", ...}

    GET /log/<job_id> - Get job log
        Query: ?lines=2000
        Response: {"job_id": "...", "status": "running|completed|failed", "log": "..."}
    
    POST /ack/<job_id> - Acknowledge job completion (called by main.sh)
        Body: {"status": "completed|failed", "exit_code": 0}

    POST /job-log-dir/<job_id> - Update job log directory from runtime script
        Body: {"log_dir": "/path/to/log/dir"}
    
    POST /cancel/<job_id> - Cancel a running job
        Response: {"job_id": "...", "status": "cancelled"}

    POST /resume - Resume from a previous output log path
        Body: {"log_path": "/path/to/.../output.log"} or {"log_dir": "/path/to/.../log_dir"}
        Response: {"job_id": "...", "status": "running"}
"""

import os
import json
import shlex
import tempfile
import subprocess
import uuid
from pathlib import Path
from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)

# Configuration
SCRIPT_ROOT = Path(__file__).parent.absolute()
DEFAULT_JOBS_FILE = SCRIPT_ROOT / "jobs.json"
SERVER_PORT = int(os.environ.get("ZHH_SERVER_PORT", "8080"))
ACTIVE_SERVER_PORT = SERVER_PORT
DEFAULT_RUN_AS_USER = str(os.environ.get("ZHH_RUN_AS_USER", "zak")).strip() or "zak"
DEFAULT_RUN_AS_PASSWORD = str(os.environ.get("ZHH_RUN_AS_PASSWORD", "0"))


def get_jobs_file():
    """Return a writable jobs file path."""
    if DEFAULT_JOBS_FILE.exists():
        if os.access(DEFAULT_JOBS_FILE, os.R_OK | os.W_OK):
            return DEFAULT_JOBS_FILE
    else:
        parent_dir = DEFAULT_JOBS_FILE.parent
        if os.access(parent_dir, os.W_OK):
            return DEFAULT_JOBS_FILE

    fallback_name = f"zhh_jobs_{os.getuid()}.json"
    return Path(tempfile.gettempdir()) / fallback_name

def load_jobs():
    """Load jobs from JSON file."""
    jobs_file = get_jobs_file()
    if not jobs_file.exists():
        return {}
    try:
        with open(jobs_file, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_jobs(jobs):
    """Save jobs to JSON file."""
    jobs_file = get_jobs_file()
    with open(jobs_file, 'w') as f:
        json.dump(jobs, f, indent=2)

def create_tmux_window_and_run(job_id, zhh_args='', cwd=None, command_override=None):
    """Create a tmux session and run zhh command."""
    session_name = f"zhh_{job_id[:8]}"

    # Use provided cwd or default to SCRIPT_ROOT
    working_dir = cwd if cwd else str(SCRIPT_ROOT)
    quoted_working_dir = shlex.quote(working_dir)
    quoted_ka = shlex.quote(str(Path(working_dir) / ".ka"))
    quoted_main = shlex.quote(str(SCRIPT_ROOT / "main.sh"))
    logs_dir = SCRIPT_ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    final_log_file = logs_dir / f"{job_id}.log"
    quoted_final_log_file = shlex.quote(str(final_log_file))

    zhh_command = command_override if command_override else (f"{quoted_main} {zhh_args}" if zhh_args else quoted_main)
    ack_url = f"http://localhost:{ACTIVE_SERVER_PORT}/ack/{job_id}"
    run_user = DEFAULT_RUN_AS_USER
    run_password = DEFAULT_RUN_AS_PASSWORD
    inner_payload = (
        f"export ZHH_SERVER_URL={shlex.quote(f'http://localhost:{ACTIVE_SERVER_PORT}')}; "
        f"export ZHH_JOB_ID={shlex.quote(job_id)}; "
        f"cd {quoted_working_dir} && . {quoted_ka} && {zhh_command}"
    )
    quoted_inner_payload = shlex.quote(inner_payload)
    quoted_run_user = shlex.quote(run_user)
    quoted_run_password = shlex.quote(run_password)

    # Self-contained bash script. The trap lives in the OUTER shell wrapper,
    # so the ack fires on exit regardless of what inner command runs or how
    # it exits — no dependency on main.sh or any other script.
    script_lines = [
        # Helper + EXIT trap (always fires)
        f"_save_final_log() {{ tmux capture-pane -p -e -S - -t \"$TMUX_PANE\" > {quoted_final_log_file} 2>/dev/null || true; }}",
        f"_ack() {{ curl -s -m5 -X POST '{ack_url}' -H 'Content-Type: application/json' -d \"{{\\\"exit_code\\\": $1}}\" || true; }}",
        f"trap '_ec=$?; _save_final_log; _ack $_ec' EXIT",
        f"TARGET_RUN_USER={quoted_run_user}",
        f"TARGET_RUN_PASSWORD={quoted_run_password}",
        f"RUN_PAYLOAD={quoted_inner_payload}",
        # Header
        f"echo '=== ZHH Job Server ==='",
        f"echo 'Job ID: {job_id}'",
        f"echo 'Working Directory: {working_dir}'",
        f"echo 'Command: {zhh_command}'",
        f"echo 'Run User: {run_user}'",
        f"echo '======================='",
        f"echo ''",
        "CURRENT_USER=\"$(whoami 2>/dev/null || true)\"",
        "if [ -n \"$TARGET_RUN_USER\" ] && [ \"$CURRENT_USER\" != \"$TARGET_RUN_USER\" ]; then",
        "  echo \"[INFO] Switching user: $CURRENT_USER -> $TARGET_RUN_USER\"",
        "  printf '%s\\n' \"$TARGET_RUN_PASSWORD\" | sudo -S su - \"$TARGET_RUN_USER\" -c \"$RUN_PAYLOAD\"",
        "else",
        "  bash -lc \"$RUN_PAYLOAD\"",
        "fi",
    ]
    tmux_cmd = "\n".join(script_lines)

    # Create new detached tmux session
    try:
        subprocess.run([
            "tmux", "new-session",
            "-d",  # detached
            "-s", session_name,
            "-c", working_dir,
            "bash", "-c", tmux_cmd
        ], check=True, capture_output=True, text=True)
        subprocess.run([
            "tmux", "set-option",
            "-t", session_name,
            "history-limit", "200000"
        ], check=False, capture_output=True, text=True)
        return True, session_name, str(final_log_file)
    except subprocess.CalledProcessError as e:
        return False, str(e), None


def resolve_resume_cwd(log_path_or_dir):
    """Resolve resume working dir by: cd parent; cd ../..."""
    p = Path(log_path_or_dir).expanduser()
    if not p.exists():
        return None, f"Path not found: {log_path_or_dir}"

    log_dir = p.parent if p.is_file() else p
    resume_cwd = (log_dir / "../..").resolve()

    if not resume_cwd.is_dir():
        return None, f"Resume directory not found: {resume_cwd}"
    if not (resume_cwd / ".ka").exists():
        return None, f".ka not found in resume directory: {resume_cwd}"

    return str(resume_cwd), None

@app.route('/run', methods=['POST'])
def run_job():
    """Start a new zhh job in a tmux window."""
    data = request.get_json() or {}
    zhh_args = data.get('args', '')
    cwd = data.get('cwd', None)
    
    # Validate cwd if provided
    if cwd and not os.path.isdir(cwd):
        return jsonify({'error': f'Directory not exist: {cwd}'}), 400
    
    # Generate job ID
    job_id = str(uuid.uuid4())
    
    # Load existing jobs
    jobs = load_jobs()
    
    # Create job record
    job = {
        'job_id': job_id,
        'status': 'starting',
        'zhh_args': zhh_args,
        'cwd': cwd or str(SCRIPT_ROOT),
        'command': f"{SCRIPT_ROOT}/main.sh {zhh_args}" if zhh_args else f"{SCRIPT_ROOT}/main.sh",
        'created_at': datetime.now().isoformat(),
        'updated_at': datetime.now().isoformat()
    }
    
    # Create tmux window and run
    success, session_name, final_log_file = create_tmux_window_and_run(job_id, zhh_args, cwd)
    
    if success:
        job['status'] = 'running'
        job['tmux_session'] = session_name
        job['final_log_file'] = final_log_file
        job['pane_log_file'] = final_log_file
        jobs[job_id] = job
        save_jobs(jobs)
        return jsonify(job), 200
    else:
        job['status'] = 'failed'
        job['error'] = session_name
        jobs[job_id] = job
        save_jobs(jobs)
        return jsonify(job), 500


@app.route('/resume', methods=['POST'])
def resume_job():
    """Resume from a previous output log path by running `main.sh rr`."""
    data = request.get_json() or {}
    log_input = data.get('log_path') or data.get('log_dir')

    if not log_input:
        return jsonify({'error': 'Missing log_path or log_dir'}), 400

    cwd, err = resolve_resume_cwd(log_input)
    if err:
        return jsonify({'error': err}), 400

    # Generate job ID
    job_id = str(uuid.uuid4())

    # Load existing jobs
    jobs = load_jobs()

    # Create job record
    job = {
        'job_id': job_id,
        'status': 'starting',
        'mode': 'resume',
        'resume_input': str(log_input),
        'cwd': cwd,
        'command': f"{SCRIPT_ROOT}/main.sh rr",
        'created_at': datetime.now().isoformat(),
        'updated_at': datetime.now().isoformat()
    }

    # Create tmux window and run resume command
    success, session_name, final_log_file = create_tmux_window_and_run(
        job_id,
        cwd=cwd,
        zhh_args='rr'
    )

    if success:
        job['status'] = 'running'
        job['tmux_session'] = session_name
        job['final_log_file'] = final_log_file
        job['pane_log_file'] = final_log_file
        jobs[job_id] = job
        save_jobs(jobs)
        return jsonify(job), 200
    else:
        job['status'] = 'failed'
        job['error'] = session_name
        jobs[job_id] = job
        save_jobs(jobs)
        return jsonify(job), 500

@app.route('/status', methods=['GET'])
def get_all_status():
    """Get status of all jobs."""
    jobs = load_jobs()
    return jsonify({'jobs': list(jobs.values()), 'count': len(jobs)}), 200

@app.route('/status/<job_id>', methods=['GET'])
def get_job_status(job_id):
    """Get status of a specific job."""
    jobs = load_jobs()
    
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404
    
    return jsonify(jobs[job_id]), 200

@app.route('/ack/<job_id>', methods=['POST'])
def ack_job(job_id):
    """Acknowledge job completion (called by main.sh)."""
    jobs = load_jobs()
    
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404
    
    data = request.get_json() or {}
    status = data.get('status', 'completed')
    exit_code = data.get('exit_code', 0)
    
    # Update job status
    jobs[job_id]['status'] = status
    jobs[job_id]['exit_code'] = exit_code
    jobs[job_id]['completed_at'] = datetime.now().isoformat()
    jobs[job_id]['updated_at'] = datetime.now().isoformat()
    
    save_jobs(jobs)

    return jsonify(jobs[job_id]), 200


@app.route('/job-log-dir/<job_id>', methods=['POST'])
def update_job_log_dir(job_id):
    """Update job log directory (reported by runtime script)."""
    jobs = load_jobs()

    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404

    data = request.get_json(silent=True) or {}
    log_dir = data.get('log_dir') or request.form.get('log_dir') or request.args.get('log_dir')
    if not log_dir:
        return jsonify({'error': 'Missing log_dir'}), 400

    jobs[job_id]['log_dir'] = log_dir
    jobs[job_id]['output_log'] = f"{log_dir}/output.log"
    jobs[job_id]['updated_at'] = datetime.now().isoformat()
    save_jobs(jobs)

    return jsonify(jobs[job_id]), 200

@app.route('/log/<job_id>', methods=['GET'])
def get_job_log(job_id):
    """Get job log: realtime from tmux while running, file after finish."""
    jobs = load_jobs()

    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404

    job = jobs[job_id]
    tmux_session = job.get('tmux_session')
    if not tmux_session:
        return jsonify({'error': 'No tmux session for this job'}), 400

    try:
        lines = int(request.args.get('lines', '2000'))
    except ValueError:
        return jsonify({'error': 'Invalid lines parameter'}), 400

    if lines < 1:
        return jsonify({'error': 'lines must be >= 1'}), 400

    start_offset = f"-{lines}"

    try:
        result = subprocess.run([
            "tmux", "capture-pane",
            "-p",
            "-e",
            "-S", start_offset,
            "-t", f"{tmux_session}:0.0"
        ], check=True, capture_output=True, text=True)
        return jsonify({
            'job_id': job_id,
            'status': job.get('status'),
            'tmux_session': tmux_session,
            'lines': lines,
            'source': 'tmux',
            'log': result.stdout
        }), 200
    except subprocess.CalledProcessError:
        final_log_file = job.get('final_log_file') or job.get('pane_log_file')
        if final_log_file and os.path.exists(final_log_file):
            try:
                with open(final_log_file, 'r', errors='replace') as f:
                    return jsonify({
                        'job_id': job_id,
                        'status': job.get('status'),
                        'tmux_session': tmux_session,
                        'lines': lines,
                        'source': 'file',
                        'log_file': final_log_file,
                        'log': f.read()
                    }), 200
            except OSError:
                pass

        return jsonify({
            'job_id': job_id,
            'status': job.get('status'),
            'error': 'tmux session is not available and log file not found'
        }), 410

@app.route('/cancel/<job_id>', methods=['POST'])
def cancel_job(job_id):
    """Cancel a running job by sending Ctrl+C to its tmux pane."""
    jobs = load_jobs()
    
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404
    
    job = jobs[job_id]
    
    # Stop job gracefully by sending Ctrl+C only
    tmux_session = job.get('tmux_session')
    if tmux_session:
        pane_target = f"{tmux_session}:0.0"

        try:
            subprocess.run([
                "tmux", "send-keys",
                "-t", pane_target,
                "C-c"
            ], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError:
            # Session/pane might already be gone, that's ok
            pass

        # Non-blocking cancel: return immediately after sending Ctrl+C
    
    # Remove job from list
    del jobs[job_id]
    save_jobs(jobs)
    
    return jsonify({'job_id': job_id, 'status': 'cancelled', 'message': 'Job cancelled and removed'}), 200

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    return jsonify({'status': 'ok', 'timestamp': datetime.now().isoformat()}), 200

if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='ZHH Job Server')
    parser.add_argument('--port', type=int, default=SERVER_PORT, help='Port to run server on')
    parser.add_argument('--host', type=str, default='0.0.0.0', help='Host to bind to')
    
    args = parser.parse_args()
    ACTIVE_SERVER_PORT = args.port
    
    print(f"Starting ZHH Job Server on {args.host}:{args.port}")
    print(f"Jobs file: {get_jobs_file()}")
    print(f"Script root: {SCRIPT_ROOT}")
    
    app.run(host=args.host, port=args.port, debug=False)
