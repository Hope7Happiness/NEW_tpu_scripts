"""API for global agent model (all sessions)."""
from __future__ import annotations

from flask import jsonify, request

from core.global_agent_model import (
    get_active_option,
    list_global_agent_model_options_public,
    load_selection_id,
    save_selection_id,
)


def register_global_agent_model_routes(app) -> None:
    @app.route("/api/settings/global-agent-model", methods=["GET"])
    def api_get_global_agent_model():
        current_id = load_selection_id()
        opt = get_active_option()
        return jsonify({
            "options": list_global_agent_model_options_public(),
            "current_id": current_id,
            "current": {
                "id": str(opt.get("id") or ""),
                "label": str(opt.get("label") or ""),
                "cli_model": str(opt.get("cli_model") or ""),
                "llm_provider": str(opt.get("llm_provider") or ""),
            },
        })

    @app.route("/api/settings/global-agent-model", methods=["PUT", "POST"])
    def api_put_global_agent_model():
        data = request.get_json(force=True, silent=True) or {}
        raw_id = data.get("id") if data.get("id") is not None else data.get("selection_id")
        sel_id = str(raw_id or "").strip()
        if not sel_id:
            return jsonify({"error": "missing id"}), 400
        try:
            save_selection_id(sel_id)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        opt = get_active_option()
        return jsonify({
            "ok": True,
            "current_id": str(opt.get("id") or ""),
            "current": {
                "id": str(opt.get("id") or ""),
                "label": str(opt.get("label") or ""),
                "cli_model": str(opt.get("cli_model") or ""),
                "llm_provider": str(opt.get("llm_provider") or ""),
            },
        })
