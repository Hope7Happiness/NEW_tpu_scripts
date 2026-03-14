from __future__ import annotations

from pathlib import Path

from flask import jsonify, request


TARGET_YAML_FILES = {
    "remote_run_config.yml": Path("configs") / "remote_run_config.yml",
    "remote_eval_config.yml": Path("configs") / "remote_eval_config.yml",
}


def _resolve_conversation_cwd(conversation: dict) -> Path:
    cwd = conversation.get("cwd")
    if not cwd:
        raise ValueError("conversation has no cwd")
    path = Path(str(cwd)).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        raise ValueError(f"conversation cwd not available: {path}")
    return path


def _get_target_path(cwd: Path, target_name: str) -> Path:
    rel = TARGET_YAML_FILES.get(target_name)
    if rel is None:
        raise ValueError(f"unsupported yaml target: {target_name}")
    path = (cwd / rel).resolve()
    return path


def _list_available_yaml_files(cwd: Path) -> list[dict]:
    items: list[dict] = []
    for name, rel in TARGET_YAML_FILES.items():
        abs_path = (cwd / rel).resolve()
        if abs_path.exists() and abs_path.is_file():
            items.append({
                "name": name,
                "relative_path": str(rel),
                "absolute_path": str(abs_path),
            })
    return items


def register_yaml_editor_routes(app, get_conversation):
    @app.route("/api/conversations/<conversation_id>/yaml/files", methods=["GET"])
    def api_yaml_files(conversation_id: str):
        conversation = get_conversation(conversation_id)
        if not conversation:
            return jsonify({"error": "not found"}), 404
        try:
            cwd = _resolve_conversation_cwd(conversation)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        files = _list_available_yaml_files(cwd)
        return jsonify({"conversation_id": conversation_id, "cwd": str(cwd), "files": files})

    @app.route("/api/conversations/<conversation_id>/yaml/file", methods=["GET"])
    def api_yaml_read(conversation_id: str):
        conversation = get_conversation(conversation_id)
        if not conversation:
            return jsonify({"error": "not found"}), 404

        target_name = request.args.get("name", "").strip()
        if not target_name:
            return jsonify({"error": "name is required"}), 400

        try:
            cwd = _resolve_conversation_cwd(conversation)
            target_path = _get_target_path(cwd, target_name)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        if not target_path.exists() or not target_path.is_file():
            return jsonify({"error": f"yaml file not found: {target_name}"}), 404

        try:
            content = target_path.read_text(encoding="utf-8")
        except Exception as e:
            return jsonify({"error": f"failed to read yaml file: {e}"}), 500

        return jsonify({
            "conversation_id": conversation_id,
            "name": target_name,
            "relative_path": str(TARGET_YAML_FILES[target_name]),
            "content": content,
        })

    @app.route("/api/conversations/<conversation_id>/yaml/file", methods=["PUT"])
    def api_yaml_write(conversation_id: str):
        conversation = get_conversation(conversation_id)
        if not conversation:
            return jsonify({"error": "not found"}), 404

        data = request.get_json(force=True, silent=True) or {}
        target_name = str(data.get("name") or "").strip()
        content = data.get("content")

        if not target_name:
            return jsonify({"error": "name is required"}), 400
        if not isinstance(content, str):
            return jsonify({"error": "content must be a string"}), 400

        try:
            cwd = _resolve_conversation_cwd(conversation)
            target_path = _get_target_path(cwd, target_name)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        if not target_path.exists() or not target_path.is_file():
            return jsonify({"error": f"yaml file not found: {target_name}"}), 404

        try:
            target_path.write_text(content, encoding="utf-8")
        except Exception as e:
            return jsonify({"error": f"failed to write yaml file: {e}"}), 500

        return jsonify({
            "conversation_id": conversation_id,
            "name": target_name,
            "relative_path": str(TARGET_YAML_FILES[target_name]),
            "bytes": len(content.encode("utf-8")),
            "saved": True,
        })
