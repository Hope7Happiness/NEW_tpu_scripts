"""对话相关API路由"""
from __future__ import annotations

from flask import jsonify, request

from core.config import SESSION_DEFAULT_EFFORT
from core.global_agent_model import get_global_cli_model
from core.utils import utc_now, _safe_positive_int
from core.workdir import normalize_workdir, workdir_base, create_workdir_by_clone, create_workdir_by_worktree, create_workdir_by_copy, list_workdir_children
from core.conversation import (
    list_conversations, get_conversation, find_conversation_by_cwd,
    create_conversation_record, delete_conversation, update_conversation,
    conversation_summary, maybe_autoname, build_conversation_memory_summary,
)
from core.conversation.store import get_conversation_lock
from core.activity import get_agent_activity_payload, reset_agent_activity
from core.tasks import clear_all_task_unread_alerts
from runtime.auto_fix_runtime import AutoFixCoordinator
from runtime.workspace_bootstrap import bootstrap_workspace_session, should_skip_workspace_bootstrap


def register_conversation_routes(app, get_agent_path_func, auto_fix_coordinator: AutoFixCoordinator):
    """注册对话相关路由"""
    
    @app.route("/api/conversations", methods=["GET"])
    def api_list_conversations():
        return jsonify({"conversations": list_conversations()})

    @app.route("/api/conversations", methods=["POST"])
    def api_create_conversation():
        data = request.get_json(force=True, silent=True) or {}
        create_type = str(data.get("create_type") or "directory").strip().lower()
        workdir = data.get("workdir")
        mode = data.get("mode") or "agent"

        if mode not in {"agent", "ask", "plan"}:
            return jsonify({"error": "invalid mode"}), 400

        try:
            if create_type == "directory":
                cwd = str(normalize_workdir(str(workdir or "")))
            elif create_type == "clone":
                cwd = str(create_workdir_by_clone(
                    str(data.get("parent_dir") or ""),
                    str(data.get("repo_url") or ""),
                    str(data.get("new_dir_name") or ""),
                ))
            elif create_type == "worktree":
                cwd = str(create_workdir_by_worktree(
                    str(data.get("source_dir") or ""),
                    str(data.get("branch_name") or ""),
                    str(data.get("new_dir_name") or ""),
                ))
            elif create_type == "copy":
                cwd = str(create_workdir_by_copy(
                    str(data.get("source_dir") or ""),
                    str(data.get("new_dir_name") or ""),
                ))
            else:
                return jsonify({"error": "invalid create_type"}), 400
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 502

        existing = find_conversation_by_cwd(cwd)
        if existing is not None:
            return jsonify({"conversation": conversation_summary(existing), "detail": existing, "reused": True})

        title = workdir_base(cwd)
        record = create_conversation_record(
            title, cwd, mode, None,
            llm_model=get_global_cli_model(),
            llm_effort=SESSION_DEFAULT_EFFORT,
        )
        conv_id = str(record.get("id") or "").strip()
        agent_path = (get_agent_path_func() if callable(get_agent_path_func) else None) or "claude"

        if should_skip_workspace_bootstrap():
            fresh = get_conversation(conv_id) or record
            return jsonify({
                "conversation": conversation_summary(fresh),
                "detail": fresh,
                "reused": False,
                "workspace_bootstrap_ok": True,
                "workspace_bootstrap_error": None,
                "workspace_bootstrap_skipped": True,
            })

        lock = get_conversation_lock(conv_id)
        if not lock.acquire(blocking=True):
            return jsonify({"error": "conversation lock unavailable"}), 503
        try:
            update_conversation(conv_id, lambda c: c.update({"status": "running"}))
            ok, err, skipped = bootstrap_workspace_session(conv_id, record, agent_path=agent_path)
        finally:
            update_conversation(conv_id, lambda c: c.update({"status": "idle"}))
            lock.release()

        fresh = get_conversation(conv_id) or record
        return jsonify({
            "conversation": conversation_summary(fresh),
            "detail": fresh,
            "reused": False,
            "workspace_bootstrap_ok": ok,
            "workspace_bootstrap_error": err,
            "workspace_bootstrap_skipped": skipped,
        })

    @app.route("/api/workdirs", methods=["GET"])
    def api_workdirs():
        try:
            allow_outside_root = str(request.args.get("allow_outside_root") or "").strip().lower() in {"1", "true", "yes", "on"}
            data = list_workdir_children(request.args.get("path"), allow_outside_root=allow_outside_root)
            return jsonify(data)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/conversations/<conversation_id>", methods=["GET"])
    def api_get_conversation(conversation_id: str):
        conv = get_conversation(conversation_id)
        if not conv:
            return jsonify({"error": "not found"}), 404
        return jsonify({"conversation": conv, "summary": conversation_summary(conv)})

    @app.route("/api/conversations/<conversation_id>/activity", methods=["GET"])
    def api_get_conversation_activity(conversation_id: str):
        conv = get_conversation(conversation_id)
        if not conv:
            return jsonify({"error": "not found"}), 404
        limit = _safe_positive_int(request.args.get("limit", "120"), default=120)
        return jsonify(get_agent_activity_payload(conversation_id, limit=limit))

    @app.route("/api/conversations/<conversation_id>/activity/reset", methods=["POST"])
    def api_reset_conversation_activity(conversation_id: str):
        conv = get_conversation(conversation_id)
        if not conv:
            return jsonify({"error": "not found"}), 404
        reset_agent_activity(conversation_id, None)
        return jsonify({"ok": True})

    @app.route("/api/conversations/<conversation_id>", methods=["DELETE"])
    def api_delete_conversation(conversation_id: str):
        conv = get_conversation(conversation_id)
        if not conv:
            return jsonify({"error": "not found"}), 404

        from core.tasks.operations import zhh_cancel_job
        cleanup_results = []
        job_ids = list(dict.fromkeys(conv.get("job_ids", []) or []))
        for job_id in job_ids:
            result = zhh_cancel_job(job_id)
            cleaned = result.get("ok", False)
            cleanup_results.append({
                "job_id": job_id,
                "cleaned": cleaned,
                "upstream_status": 200 if cleaned else 500,
                "detail": result,
            })

        deleted = delete_conversation(conversation_id)
        if not deleted:
            return jsonify({"error": "not found"}), 404
        
        from core.activity import clear_agent_activity
        clear_agent_activity(conversation_id)

        return jsonify({
            "deleted": True,
            "conversation": conversation_summary(deleted),
            "task_cleanup": {
                "count": len(cleanup_results),
                "results": cleanup_results,
            },
        })

    @app.route("/api/conversations/<conversation_id>/compact", methods=["POST"])
    def api_compact_conversation(conversation_id: str):
        from core.conversation import get_conversation_lock
        
        conv = get_conversation(conversation_id)
        if not conv:
            return jsonify({"error": "not found"}), 404

        lock = get_conversation_lock(conversation_id)
        if not lock.acquire(blocking=False):
            return jsonify({"error": "conversation busy"}), 409

        try:
            latest = get_conversation(conversation_id)
            if not latest:
                return jsonify({"error": "not found"}), 404

            summary = build_conversation_memory_summary(latest)
            updated = update_conversation(conversation_id, lambda c: c.update({
                "memory_summary": summary,
                "memory_summary_pending": True,
                "cursor_session_id": None,
                "status": "idle",
                "compaction_count": int(c.get("compaction_count") or 0) + 1,
                "compacted_at": utc_now(),
            }))

            from core.conversation.store import append_message
            append_message(conversation_id, "system", "Context compacted. Session context reset with memory summary.", {
                "system_event": "conversation_compacted",
            })

            return jsonify({
                "conversation": updated,
                "summary_chars": len(summary),
                "compaction_count": int(updated.get("compaction_count") or 0),
            }), 200
        finally:
            lock.release()

    @app.route("/api/conversations/<conversation_id>/auto-fix/stop", methods=["POST"])
    def api_stop_auto_fix(conversation_id: str):
        conv = get_conversation(conversation_id)
        if not conv:
            return jsonify({"error": "not found"}), 404

        status = str(conv.get("status") or "").strip().lower()
        if status != "debugging":
            return jsonify({"error": "auto fix stop is only available while debugging"}), 409

        stopped, job_id = auto_fix_coordinator.request_stop(conversation_id)
        if not stopped:
            return jsonify({"error": "no active auto fix worker"}), 409

        return jsonify({
            "ok": True,
            "conversation_id": conversation_id,
            "job_id": str(job_id or ""),
        }), 202
