from __future__ import annotations

from pathlib import Path
import re

from flask import jsonify, request


TARGET_YAML_FILES = {
    "remote_run_config.yml": Path("configs") / "remote_run_config.yml",
    "remote_eval_config.yml": Path("configs") / "remote_eval_config.yml",
}

SCRIPT_DIR = Path(__file__).parent.resolve()
SCRIPT_ROOT = SCRIPT_DIR.parent.resolve()
SCRIPT_WHO = SCRIPT_ROOT.name
SCRIPT_KA_FILE = SCRIPT_DIR / ".ka"


def _extract_wandb_api_key(text: str) -> str:
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(r"^export\s+WANDB_API_KEY\s*=\s*(.*)$", stripped)
        if not m:
            continue
        value = m.group(1).strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        return value
    return ""


def _load_default_wandb_api_key() -> str:
    try:
        if SCRIPT_KA_FILE.exists() and SCRIPT_KA_FILE.is_file():
            return _extract_wandb_api_key(SCRIPT_KA_FILE.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        pass
    return ""


def _ka_template_text() -> str:
    key = _load_default_wandb_api_key()
    lines = [
        "export VM_NAME=autov6",
        "export TPU_TYPES=64",
        "export ZONE=",
        f"export WANDB_API_KEY={key}",
        f"export WHO={SCRIPT_WHO}",
        "",
    ]
    return "\n".join(lines)


def _ka_file_path(cwd: Path) -> Path:
    return (cwd / ".ka").resolve()


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


def register_ka_editor_routes(app, get_conversation):
    @app.route("/api/conversations/<conversation_id>/ka/file", methods=["GET"])
    def api_ka_read(conversation_id: str):
        conversation = get_conversation(conversation_id)
        if not conversation:
            return jsonify({"error": "not found"}), 404

        try:
            cwd = _resolve_conversation_cwd(conversation)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        target_path = _ka_file_path(cwd)
        created = False
        if not target_path.exists():
            try:
                target_path.write_text(_ka_template_text(), encoding="utf-8")
                created = True
            except Exception as e:
                return jsonify({"error": f"failed to create .ka file: {e}"}), 500
        elif not target_path.is_file():
            return jsonify({"error": ".ka exists but is not a file"}), 400

        try:
            content = target_path.read_text(encoding="utf-8")
        except Exception as e:
            return jsonify({"error": f"failed to read .ka file: {e}"}), 500

        return jsonify({
            "conversation_id": conversation_id,
            "name": ".ka",
            "relative_path": ".ka",
            "content": content,
            "created": created,
        })

    @app.route("/api/conversations/<conversation_id>/ka/file", methods=["PUT"])
    def api_ka_write(conversation_id: str):
        conversation = get_conversation(conversation_id)
        if not conversation:
            return jsonify({"error": "not found"}), 404

        data = request.get_json(force=True, silent=True) or {}
        content = data.get("content")
        if not isinstance(content, str):
            return jsonify({"error": "content must be a string"}), 400

        try:
            cwd = _resolve_conversation_cwd(conversation)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        target_path = _ka_file_path(cwd)
        try:
            target_path.write_text(content, encoding="utf-8")
        except Exception as e:
            return jsonify({"error": f"failed to write .ka file: {e}"}), 500

        return jsonify({
            "conversation_id": conversation_id,
            "name": ".ka",
            "relative_path": ".ka",
            "bytes": len(content.encode("utf-8")),
            "saved": True,
        })
