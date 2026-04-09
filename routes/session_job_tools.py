"""HTTP surface for session job agent tools (RUN / global list / single job query)."""
from __future__ import annotations

from flask import jsonify, request

from core.tasks.session_job_tools import (
    global_query_session_jobs,
    query_session_job,
    run_job_with_config_path,
)


def register_session_job_tool_routes(app) -> None:
    @app.route("/api/conversations/<conversation_id>/session-job-tools/run", methods=["POST"])
    def api_session_job_run(conversation_id: str):
        data = request.get_json(force=True, silent=True) or {}
        config_path = str(data.get("config_path") or "").strip()
        description = data.get("description")
        nickname = str(data.get("nickname") or "").strip() or None
        if not config_path:
            return jsonify({"error": "config_path is required"}), 400
        if description is None or not str(description).strip():
            return jsonify({"error": "description is required"}), 400

        result = run_job_with_config_path(
            conversation_id, config_path, str(description).strip(), nickname=nickname
        )
        if not result.get("ok"):
            code = 500
            err = str(result.get("error") or "failed")
            if "not found" in err or "required" in err.lower() or "too long" in err.lower():
                code = 400
            if "conversation not found" in err:
                code = 404
            body = {"error": err}
            if result.get("detail") is not None:
                body["detail"] = result["detail"]
            return jsonify(body), code
        return "", 204

    @app.route("/api/conversations/<conversation_id>/session-job-tools/jobs", methods=["GET"])
    def api_session_job_list(conversation_id: str):
        status_param = request.args.get("status")
        result = global_query_session_jobs(conversation_id, status_param)
        if not result.get("ok"):
            err = str(result.get("error") or "failed")
            code = 404 if "not found" in err else 502
            return jsonify({"error": err}), code
        return jsonify({
            "conversation_id": conversation_id,
            "count": result["count"],
            "jobs": result["jobs"],
        }), 200

    @app.route("/api/conversations/<conversation_id>/session-job-tools/jobs/<job_id>", methods=["GET"])
    def api_session_job_detail(conversation_id: str, job_id: str):
        result = query_session_job(conversation_id, job_id)
        if not result.get("ok"):
            err = str(result.get("error") or "failed")
            if "not found" in err or "does not belong" in err:
                return jsonify({"error": err}), 404
            return jsonify({"error": err}), 400
        return jsonify({
            "conversation_id": conversation_id,
            "job_id": result["job_id"],
            "description": result["description"],
            "status": result["status"],
            "config_path": result["config_path"],
            "log_file": result["log_file"],
        }), 200
