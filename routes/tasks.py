"""任务相关API路由"""
from __future__ import annotations

from flask import jsonify, request

from core.utils import _safe_positive_int

# Task log viewer: never return unbounded log text (default & cap: last N lines).
_TASK_LOG_VIEWER_MAX_LINES = 400
from core.conversation import get_conversation, update_conversation
from core.tasks import (
    get_conversation_jobs, diagnose_completed_jobs_once, update_task_alert_state,
    apply_running_display_overrides, normalize_task_status, is_running_like_task_status,
    is_terminal_task_status, is_failed_task_status, clear_task_unread_alert,
    clear_all_task_unread_alerts, resolve_task_output_log_path, snapshot_task_log_before_cancel
)
from core.tasks.operations import (
    zhh_run_job,
    zhh_resume_job,
    zhh_cancel_job,
    get_task_log_payload,
    persist_zhh_job_to_conversation,
)
from core.activity import record_agent_event


def _resolve_job_status(conv: dict, job_id: str) -> str:
    """解析任务状态"""
    task_meta = conv.get("task_meta")
    if isinstance(task_meta, dict):
        entry = task_meta.get(job_id)
        if isinstance(entry, dict):
            manual = str(entry.get("manual_status") or "").strip()
            if manual:
                return normalize_task_status(manual)
            forced = bool(entry.get("forced_error", False))
            if forced:
                return "error"
    return "unknown"


def _build_task_reference_payload(conversation_id: str, conv: dict, job_id: str, lines: int = 400) -> tuple[str, str]:
    """构建任务引用负载"""
    payload = get_task_log_payload(conv, job_id, lines=lines, prefer_pane=True)
    if not isinstance(payload, dict):
        return "", ""
    text = str(payload.get("log") or "")
    source = str(payload.get("source") or payload.get("log_path") or "")
    return text, source


def register_task_routes(app, zhh_server_url: str, auto_fix_coordinator=None):
    """注册任务相关路由"""

    @app.route("/api/conversations/<conversation_id>/tasks", methods=["GET"])
    def api_list_tasks(conversation_id: str):
        conv = get_conversation(conversation_id)
        if not conv:
            return jsonify({"error": "not found"}), 404
        try:
            jobs = get_conversation_jobs(conv)
            conv, jobs = diagnose_completed_jobs_once(conversation_id, conv, jobs)
            conv, jobs = update_task_alert_state(conversation_id, conv, jobs)
            jobs = apply_running_display_overrides(jobs)
            if auto_fix_coordinator:
                auto_fix_coordinator.maybe_schedule(conversation_id, conv, jobs)
            return jsonify({"conversation_id": conversation_id, "count": len(jobs), "jobs": jobs})
        except Exception as e:
            return jsonify({"error": str(e)}), 502

    @app.route("/api/conversations/<conversation_id>/tasks/run", methods=["POST"])
    def api_run_task(conversation_id: str):
        conv = get_conversation(conversation_id)
        if not conv:
            return jsonify({"error": "not found"}), 404

        data = request.get_json(force=True, silent=True) or {}
        zhh_args = (data.get("args") or "").strip()

        result = zhh_run_job(args=zhh_args, cwd=conv["cwd"])
        if not result.get("ok"):
            return jsonify({"error": result.get("error", "/run failed"), "detail": result.get("payload")}), 500

        job_id = result.get("job_id")
        run_data = result.get("payload", {})
        persist_zhh_job_to_conversation(
            conversation_id,
            job_id,
            run_data,
            auto_run_by_agent=False,
        )

        return jsonify({"conversation_id": conversation_id, "job": run_data}), 200

    @app.route("/api/conversations/<conversation_id>/tasks/<job_id>/cancel", methods=["POST"])
    def api_cancel_task(conversation_id: str, job_id: str):
        conv = get_conversation(conversation_id)
        if not conv:
            return jsonify({"error": "not found"}), 404

        job_ids = conv.get("job_ids", []) or []
        if job_id not in job_ids:
            return jsonify({"error": "job does not belong to this conversation"}), 404

        snapshot_task_log_before_cancel(conversation_id, job_id)
        result = zhh_cancel_job(job_id)
        
        if result.get("ok"):
            from core.tasks.operations import mark_task_status
            mark_task_status(conversation_id, job_id, "canceled")
            return jsonify(result.get("payload", {"ok": True})), 200
        return jsonify({"error": result.get("error", "cancel failed")}), 500

    @app.route("/api/conversations/<conversation_id>/tasks/<job_id>/mark-error", methods=["POST"])
    def api_mark_task_error(conversation_id: str, job_id: str):
        conv = get_conversation(conversation_id)
        if not conv:
            return jsonify({"error": "not found"}), 404

        job_ids = conv.get("job_ids", []) or []
        if job_id not in job_ids:
            return jsonify({"error": "job does not belong to this conversation"}), 404

        status = _resolve_job_status(conv, job_id)
        if not is_running_like_task_status(status):
            return jsonify({"error": f"task status is {status}, only running-like tasks can be marked as error"}), 409

        from core.tasks.operations import mark_task_error_forced
        mark_task_error_forced(conversation_id, job_id)
        snapshot_task_log_before_cancel(conversation_id, job_id)
        cancel_result = zhh_cancel_job(job_id)
        cancel_status = 200 if cancel_result.get("ok") else 500

        if cancel_result.get("ok"):
            return jsonify({
                "conversation_id": conversation_id,
                "job_id": job_id,
                "status": "error",
                "cancel_status": cancel_status,
                "cancel_payload": cancel_result.get("payload"),
            }), 200

        return jsonify({
            "conversation_id": conversation_id,
            "job_id": job_id,
            "status": "error",
            "cancel_status": cancel_status,
            "cancel_payload": cancel_result.get("payload"),
            "error": f"marked as error locally, but upstream cancel failed",
        }), 502

    @app.route("/api/conversations/<conversation_id>/tasks/<job_id>/resume", methods=["POST"])
    def api_resume_task(conversation_id: str, job_id: str):
        conv = get_conversation(conversation_id)
        if not conv:
            return jsonify({"error": "not found"}), 404

        job_ids = conv.get("job_ids", []) or []
        if job_id not in job_ids:
            return jsonify({"error": "job does not belong to this conversation"}), 404

        status = "unknown"
        try:
            jobs = get_conversation_jobs(conv)
            conv, jobs = diagnose_completed_jobs_once(conversation_id, conv, jobs)
            conv, jobs = update_task_alert_state(conversation_id, conv, jobs)
            for job in jobs:
                if isinstance(job, dict) and str(job.get("job_id") or "") == job_id:
                    status = normalize_task_status(job.get("status"))
                    break
        except Exception:
            status = _resolve_job_status(conv, job_id)

        if not is_failed_task_status(status):
            return jsonify({"error": f"task status is {status}, only failed/error-like tasks can be resumed"}), 409

        output_log_path = resolve_task_output_log_path(job_id)
        if not output_log_path:
            return jsonify({"error": "output.log path not found for this task"}), 404

        result = zhh_resume_job(output_log_path)
        if not result.get("ok"):
            return jsonify({"error": result.get("error", "/resume failed"), "detail": result.get("payload")}), 500

        run_data = result.get("payload", {})
        resumed_job_id = str(run_data.get("job_id") or "").strip()
        
        if resumed_job_id:
            from core.conversation.store import append_message
            from core.utils import utc_now
            
            def add_job(c: dict):
                ids = c.setdefault("job_ids", [])
                if resumed_job_id not in ids:
                    ids.append(resumed_job_id)

                task_meta = c.setdefault("task_meta", {})
                entry = task_meta.get(resumed_job_id, {})
                if not isinstance(entry, dict):
                    entry = {}
                else:
                    entry = dict(entry)

                entry["last_status"] = normalize_task_status(run_data.get("status") or "starting")
                entry["updated_at"] = utc_now()
                entry["resume_from_job_id"] = job_id
                entry["resume_log_path"] = output_log_path
                
                source_entry = task_meta.get(job_id)
                source_nickname = ""
                if isinstance(source_entry, dict):
                    source_nickname = str(source_entry.get("nickname") or "").strip()
                if source_nickname:
                    entry["nickname"] = f"resume · {source_nickname}"
                elif not str(entry.get("nickname") or "").strip():
                    entry["nickname"] = f"resume · {job_id[:8]}"
                
                for key in ("zhh_args", "created_at", "final_log_file", "pane_log_file", "command", "cwd", "mode"):
                    value = run_data.get(key)
                    if value is not None and value != "":
                        entry[key] = value
                task_meta[resumed_job_id] = entry

            update_conversation(conversation_id, add_job)
            append_message(conversation_id, "system", f"Resumed job {job_id} as {resumed_job_id}", {
                "system_event": "task_resume",
                "job_id": resumed_job_id,
                "job_status": str(run_data.get("status") or "starting"),
                "resume_from_job_id": job_id,
                "resume_log_path": output_log_path,
            })

        return jsonify({
            "conversation_id": conversation_id,
            "source_job_id": job_id,
            "log_path": output_log_path,
            "job": run_data,
        }), 200

    @app.route("/api/conversations/<conversation_id>/tasks/<job_id>", methods=["DELETE"])
    def api_remove_task(conversation_id: str, job_id: str):
        conv = get_conversation(conversation_id)
        if not conv:
            return jsonify({"error": "not found"}), 404

        job_ids = conv.get("job_ids", []) or []
        if job_id not in job_ids:
            return jsonify({"error": "job does not belong to this conversation"}), 404

        status = "unknown"
        try:
            jobs = get_conversation_jobs(conv)
            for job in jobs:
                if isinstance(job, dict) and job.get("job_id") == job_id:
                    status = normalize_task_status(job.get("status"))
                    break
        except Exception:
            pass

        if not is_terminal_task_status(status):
            return jsonify({"error": f"task status is {status}, only terminal tasks can be removed"}), 409

        def remove_job(c: dict):
            c["job_ids"] = [jid for jid in (c.get("job_ids", []) or []) if jid != job_id]
            task_meta = c.get("task_meta")
            if isinstance(task_meta, dict):
                task_meta.pop(job_id, None)

        updated = update_conversation(conversation_id, remove_job)
        return jsonify({
            "conversation_id": conversation_id,
            "removed": True,
            "job_id": job_id,
            "task_count": len(updated.get("job_ids", []) or []),
        }), 200

    @app.route("/api/conversations/<conversation_id>/tasks/<job_id>/nickname", methods=["POST"])
    def api_task_nickname(conversation_id: str, job_id: str):
        data = request.get_json(force=True, silent=True) or {}
        raw_nickname = data.get("nickname")
        if raw_nickname is None:
            return jsonify({"error": "nickname is required"}), 400

        nickname = str(raw_nickname).strip()
        from core.tasks.session_job_tools import set_job_nickname
        result = set_job_nickname(conversation_id, job_id, nickname)
        if not result.get("ok"):
            err = str(result.get("error") or "failed")
            if "not found" in err or "does not belong" in err:
                return jsonify({"error": err}), 404
            return jsonify({"error": err}), 400

        return jsonify({
            "conversation_id": conversation_id,
            "job_id": job_id,
            "nickname": result.get("nickname", nickname),
        }), 200

    @app.route("/api/conversations/<conversation_id>/tasks/<job_id>/log", methods=["GET"])
    def api_task_log(conversation_id: str, job_id: str):
        conv = get_conversation(conversation_id)
        if not conv:
            return jsonify({"error": "not found"}), 404

        job_ids = conv.get("job_ids", []) or []
        if job_id not in job_ids:
            return jsonify({"error": "job does not belong to this conversation"}), 404

        raw_lines = _safe_positive_int(
            request.args.get("lines", str(_TASK_LOG_VIEWER_MAX_LINES)),
            default=_TASK_LOG_VIEWER_MAX_LINES,
        )
        lines = raw_lines if raw_lines > 0 else _TASK_LOG_VIEWER_MAX_LINES
        lines = min(lines, _TASK_LOG_VIEWER_MAX_LINES)
        job_status = _resolve_job_status(conv, job_id)

        payload = get_task_log_payload(conv, job_id, lines=lines, prefer_pane=True)
        
        if payload is not None:
            clear_task_unread_alert(conversation_id, job_id)
            return jsonify(payload), 200

        # Fallback for terminal tasks
        if not is_running_like_task_status(job_status):
            return jsonify({
                "job_id": job_id,
                "lines": lines,
                "log": f"[{job_id}] Log not available.",
                "source": "synthetic",
            }), 200

        return jsonify({
            "error": f"Log not available for job {job_id}",
        }), 404

    @app.route("/api/conversations/<conversation_id>/tasks/<job_id>/wandb", methods=["GET"])
    def api_task_wandb(conversation_id: str, job_id: str):
        from core.tasks.operations import resolve_task_wandb_url
        
        conv = get_conversation(conversation_id)
        if not conv:
            return jsonify({"error": "not found"}), 404

        job_ids = conv.get("job_ids", []) or []
        if job_id not in job_ids:
            return jsonify({"error": "job does not belong to this conversation"}), 404

        url, source = resolve_task_wandb_url(conv, job_id)
        if not url:
            return jsonify({"error": "wandb link not found in task logs"}), 404

        clear_task_unread_alert(conversation_id, job_id)

        return jsonify({
            "job_id": job_id,
            "wandb_url": url,
            "source": source,
        }), 200

    @app.route("/api/conversations/<conversation_id>/tasks/mark-all-read", methods=["POST"])
    def api_mark_all_tasks_read(conversation_id: str):
        conv = get_conversation(conversation_id)
        if not conv:
            return jsonify({"error": "not found"}), 404
        clear_all_task_unread_alerts(conversation_id)
        return jsonify({"conversation_id": conversation_id, "ok": True}), 200
