#!/usr/bin/env python3
"""
cursor_server_refactored.py — 重构后的CurChat服务器主入口

原cursor_server.py的重构版本，将代码分散到多个模块以提高可维护性。
功能保持不变，仅进行了代码组织优化。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import shlex
import threading
from pathlib import Path

from flask import Flask, send_from_directory

# 初始化Flask应用
app = Flask(__name__)

# 导入核心配置
from core.config import (
    APP_ROOT, CONFIG_PATH, STORE_PATH, UI_TEMPLATE_PATH,
    DEFAULT_HOST, DEFAULT_PORT, DEFAULT_AGENT, DEFAULT_CWD,
    ZHH_SERVER_URL, SESSION_DEFAULT_MODEL, SESSION_DEFAULT_EFFORT,
    ALLOWED_SESSION_MODELS, ALLOWED_SESSION_EFFORTS,
    AUTO_FIX_SCHEDULER_INTERVAL_SECONDS, CURCHAT_USER
)

# 导入核心模块
from core.utils import utc_now
from core.workdir import relative_workdir

# 导入路由
from routes.conversations import register_conversation_routes
from routes.tasks import register_task_routes
from routes.agent import register_agent_routes

# 导入外部模块
from runtime.yaml_editor_api import register_ka_editor_routes, register_yaml_editor_routes
from runtime.auto_fix_runtime import AutoFixCoordinator
from runtime.acp_runtime import acp_prompt_session, get_model_policy_status, note_usage_limit_error
from runtime.agent_action_protocol import extract_run_job_action, new_action_nonce, with_run_job_skill_instruction
from runtime.tasks_runtime import zhh_request, build_prompt_with_task_refs

# 全局变量（将在main中初始化）
SERVER_CWD = DEFAULT_CWD
AGENT_PATH = DEFAULT_AGENT


def bootstrap_model_policy_from_store() -> None:
    """从存储加载模型策略"""
    try:
        if not STORE_PATH.exists():
            return
        payload = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return

    conversations = payload.get("conversations") if isinstance(payload, dict) else None
    if not isinstance(conversations, dict):
        return

    for conv in conversations.values():
        if not isinstance(conv, dict):
            continue
        err = str(conv.get("last_error") or "").strip()
        if err:
            note_usage_limit_error(err)


# 注册路由
@app.route("/")
def index():
    """首页"""
    policy = get_model_policy_status()
    html = UI_TEMPLATE_PATH.read_text(encoding="utf-8")
    html = html.replace("__WORKDIR_ROOT__", str(Path(SERVER_CWD).parent if SERVER_CWD else APP_ROOT))
    html = html.replace("__DEFAULT_SESSION_MODEL__", SESSION_DEFAULT_MODEL)
    html = html.replace("__DEFAULT_SESSION_EFFORT__", SESSION_DEFAULT_EFFORT)
    html = html.replace("__DEFAULT_MODEL__", str(policy.get("effective_model") or "default"))
    html = html.replace("__CONFIGURED_MODEL__", str(policy.get("configured_model") or "default"))
    return html, 200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }


@app.route("/assets/<path:filename>")
def serve_ui_asset(filename: str):
    """服务UI资源"""
    allowed_suffixes = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico"}
    requested = Path(filename)
    if requested.suffix.lower() not in allowed_suffixes:
        return {"error": "unsupported asset type"}, 404
    asset_root = APP_ROOT / "ui" / "assets"
    asset_path = (asset_root / requested).resolve()
    try:
        asset_path.relative_to(asset_root)
    except ValueError:
        return {"error": "invalid asset path"}, 404
    if not asset_path.exists() or not asset_path.is_file():
        return {"error": "asset not found"}, 404
    return send_from_directory(asset_root, str(requested))


@app.route("/favicon.ico")
def favicon_alias():
    """favicon别名"""
    assets_root = APP_ROOT / "ui" / "assets"
    favicon_path = assets_root / "favicon.ico"
    if favicon_path.exists() and favicon_path.is_file():
        return send_from_directory(assets_root, "favicon.ico")
    icon_path = assets_root / "curchat-64.png"
    if icon_path.exists() and icon_path.is_file():
        return send_from_directory(assets_root, "curchat-64.png")
    icon_path = assets_root / "curchat.png"
    if icon_path.exists() and icon_path.is_file():
        return send_from_directory(assets_root, "curchat.png")
    return {"error": "favicon not found"}, 404


# 导入对话管理相关函数
from core.conversation import get_conversation


def run_auto_fix_scheduler_loop(interval_seconds: int = AUTO_FIX_SCHEDULER_INTERVAL_SECONDS) -> None:
    """运行自动修复调度器循环"""
    from core.tasks import diagnose_completed_jobs_once, update_task_alert_state, get_conversation_jobs
    from core.conversation import list_conversations, get_conversation as get_conv
    
    interval = max(2, int(interval_seconds or 10))
    while True:
        try:
            conv_items = list_conversations()
            for item in conv_items:
                conversation_id = str((item or {}).get("id") or "").strip()
                if not conversation_id:
                    continue
                conv = get_conv(conversation_id)
                if not conv:
                    continue
                try:
                    jobs = get_conversation_jobs(conv)
                    conv, jobs = diagnose_completed_jobs_once(conversation_id, conv, jobs)
                    conv, jobs = update_task_alert_state(conversation_id, conv, jobs)
                    # 注意：auto_fix_coordinator将在main中初始化
                except Exception:
                    continue
        except Exception:
            pass
        import time
        time.sleep(interval)


def main():
    """主入口函数"""
    global SERVER_CWD, AGENT_PATH

    parser = argparse.ArgumentParser(description="CurChat conversation server (Claude Code backbone) - Refactored")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--cwd", default=DEFAULT_CWD, help="Default working directory for new sessions")
    parser.add_argument("--agent-path", default=DEFAULT_AGENT)
    args = parser.parse_args()

    SERVER_CWD = str(Path(args.cwd).expanduser())
    requested_agent = str(args.agent_path or "").strip()
    requested_path = Path(requested_agent).expanduser()
    if requested_path.is_absolute() or "/" in requested_agent:
        AGENT_PATH = str(requested_path)
    else:
        AGENT_PATH = requested_agent

    resolved_agent = ""
    requested_parts = shlex.split(AGENT_PATH) if AGENT_PATH else []
    if requested_parts and len(requested_parts) > 1:
        lead = requested_parts[0]
        lead_path = Path(lead).expanduser()
        if lead_path.exists():
            resolved_agent = AGENT_PATH
        else:
            lead_which = shutil.which(lead) or ""
            if lead_which:
                requested_parts[0] = lead_which
                resolved_agent = " ".join(requested_parts)
    else:
        if Path(AGENT_PATH).exists():
            resolved_agent = AGENT_PATH
        else:
            resolved_agent = shutil.which(AGENT_PATH) or ""
    if resolved_agent:
        AGENT_PATH = resolved_agent

    print("=" * 60)
    print("CurChat server (Claude Code) - Refactored")
    print("=" * 60)
    print(f"curchat user: {CURCHAT_USER}")
    print(f"agent path : {AGENT_PATH}")
    print(f"default cwd: {SERVER_CWD}")
    print(f"store file : {STORE_PATH}")
    print(f"task server: {ZHH_SERVER_URL}")
    print(f"url : http://{args.host}:{args.port}")
    print("=" * 60)

    if not AGENT_PATH:
        raise SystemExit(f"agent not found: {AGENT_PATH}")
    agent_parts = shlex.split(AGENT_PATH)
    if not agent_parts:
        raise SystemExit(f"agent not found: {AGENT_PATH}")
    lead_cmd = agent_parts[0]
    lead_cmd_path = Path(lead_cmd).expanduser()
    if not lead_cmd_path.exists() and not shutil.which(lead_cmd):
        raise SystemExit(f"agent not found: {AGENT_PATH}")
    if not Path(SERVER_CWD).exists():
        raise SystemExit(f"cwd does not exist: {SERVER_CWD}")
    if not UI_TEMPLATE_PATH.exists():
        raise SystemExit(f"ui template not found: {UI_TEMPLATE_PATH}")

    # 初始化AutoFixCoordinator
    from core.conversation import get_conversation_lock, update_conversation, append_message, maybe_autoname
    from core.tasks import is_failed_task_status, normalize_task_status
    from core.activity import record_agent_event
    from routes.tasks import _build_task_reference_payload, _resolve_job_status
    from core.tasks.operations import zhh_run_job

    def trigger_run_job_for_conversation(conversation_id: str, auto_run_by_agent: bool = False) -> tuple[str | None, str | None]:
        conv = get_conversation(conversation_id)
        if not conv:
            return None, "conversation not found"

        run_result = zhh_run_job(args="", cwd=conv.get("cwd") or "")
        if not run_result.get("ok"):
            return None, str(run_result.get("error") or "run failed")

        run_data = run_result.get("payload") if isinstance(run_result.get("payload"), dict) else {}
        job_id = str(run_data.get("job_id") or run_result.get("job_id") or "").strip()
        if not job_id:
            return None, "run succeeded but job_id missing"

        def add_job(c: dict):
            job_ids = c.setdefault("job_ids", [])
            if job_id not in job_ids:
                job_ids.append(job_id)
            task_meta = c.setdefault("task_meta", {})
            entry = task_meta.get(job_id)
            if not isinstance(entry, dict):
                entry = {}
            else:
                entry = dict(entry)
            entry["last_status"] = normalize_task_status(run_data.get("status") or "starting")
            entry["updated_at"] = utc_now()
            for key in ("zhh_args", "created_at", "final_log_file", "pane_log_file", "command", "cwd"):
                value = run_data.get(key)
                if value is not None and value != "":
                    entry[key] = value
            task_meta[job_id] = entry

        update_conversation(conversation_id, add_job)
        append_message(conversation_id, "system", f"Runned job {job_id}", {
            "system_event": "task_run",
            "job_id": job_id,
            "job_status": str(run_data.get("status") or "starting"),
            "zhh_args": str(run_data.get("zhh_args") or ""),
            "auto_run_by_agent": bool(auto_run_by_agent),
        })
        return job_id, None

    auto_fix_coordinator = AutoFixCoordinator(
        get_conversation=get_conversation,
        get_conversation_lock=get_conversation_lock,
        update_conversation=update_conversation,
        append_message=append_message,
        build_task_reference_payload=lambda conversation_id, conv, job_id: _build_task_reference_payload(
            conversation_id, conv, job_id, lines=400
        ),
        resolve_job_status=_resolve_job_status,
        is_failed_task_status=is_failed_task_status,
        normalize_task_status=normalize_task_status,
        maybe_autoname=maybe_autoname,
        acp_prompt_session=acp_prompt_session,
        agent_path_getter=lambda: AGENT_PATH,
        trigger_run_job=trigger_run_job_for_conversation,
        utc_now=utc_now,
        report_agent_event=lambda conversation_id, event: record_agent_event(conversation_id, event),
    )

    # 注册路由
    register_conversation_routes(app, lambda: AGENT_PATH, auto_fix_coordinator)
    register_task_routes(app, ZHH_SERVER_URL, auto_fix_coordinator)
    register_agent_routes(app, auto_fix_coordinator, lambda: AGENT_PATH)
    
    # 注册YAML编辑器路由
    register_yaml_editor_routes(app, get_conversation)
    register_ka_editor_routes(app, get_conversation)

    bootstrap_model_policy_from_store()

    # 启动自动修复调度器
    scheduler_thread = threading.Thread(
        target=run_auto_fix_scheduler_loop,
        args=(AUTO_FIX_SCHEDULER_INTERVAL_SECONDS,),
        daemon=True,
        name="auto-fix-scheduler",
    )
    scheduler_thread.start()
    print(f"auto-fix scheduler interval: {AUTO_FIX_SCHEDULER_INTERVAL_SECONDS}s")
    print("=" * 60)

    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
