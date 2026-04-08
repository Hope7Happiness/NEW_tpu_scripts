"""Agent交互相关API路由"""
from __future__ import annotations

from flask import jsonify, request

from core.config import ALLOWED_SESSION_EFFORTS, ZHH_SERVER_URL
from core.utils import utc_now
from core.utils import _compact_error_text
from core.conversation import (
    get_conversation, update_conversation, 
    build_prompt_with_memory, resolve_session_effort,
    parse_session_setting_command, maybe_autoname
)
from core.conversation.store import append_message
from core.activity import reset_agent_activity, finish_agent_activity, record_agent_event
from core.tasks import get_conversation_jobs, diagnose_completed_jobs_once, update_task_alert_state
from runtime.agent_action_protocol import extract_run_job_action, new_action_nonce, with_run_job_skill_instruction
from runtime.acp_runtime import acp_prompt_session, note_usage_limit_error
from runtime.tasks_runtime import build_prompt_with_task_refs
from core.global_agent_model import get_global_cli_model, get_global_llm_provider


def register_agent_routes(app, auto_fix_coordinator=None, get_agent_path_func=None):
    """注册Agent交互相关路由"""

    @app.route("/api/conversations/<conversation_id>/messages", methods=["POST"])
    def api_send_message(conversation_id: str):
        from core.conversation import get_conversation_lock
        
        data = request.get_json(force=True, silent=True) or {}
        text = (data.get("text") or "").strip()
        if not text:
            return jsonify({"error": "empty text"}), 400
        force_send = bool(data.get("force_send", False))

        task_refs = data.get("task_refs") or []
        if not isinstance(task_refs, list):
            return jsonify({"error": "task_refs must be a list"}), 400

        normalized_refs = []
        for ref in task_refs:
            if isinstance(ref, str) and ref.strip():
                normalized_refs.append(ref.strip())
        normalized_refs = list(dict.fromkeys(normalized_refs))

        conv = get_conversation(conversation_id)
        if not conv:
            return jsonify({"error": "not found"}), 404

        lowered_text = text.strip().lower()
        if lowered_text.startswith("/model"):
            return jsonify({
                "error": "Model is configured globally for all sessions. Use the Model dropdown in the sidebar.",
            }), 400

        if lowered_text.startswith("/effort"):
            if len(text.split()) != 2:
                return jsonify({"error": "invalid setting command. use /effort <low|medium|high|max>"}), 400

            setting_key, setting_value = parse_session_setting_command(text)
            if setting_key is None:
                return jsonify({"error": "invalid effort. allowed: low, medium, high, max"}), 400
            if setting_key != "effort":
                return jsonify({"error": "invalid effort. allowed: low, medium, high, max"}), 400
            status = str(conv.get("status") or "").strip().lower()
            if status in {"running", "debugging"}:
                return jsonify({"error": "session setting update is unavailable while agent is running"}), 409

            normalized = str(setting_value or "").strip().lower()
            if normalized not in ALLOWED_SESSION_EFFORTS:
                return jsonify({"error": "invalid effort. allowed: low, medium, high, max"}), 400

            append_message(conversation_id, "user", text)
            append_message(conversation_id, "assistant", f"Effort updated to `{normalized}` for this session.")
            updated = update_conversation(conversation_id, lambda c: c.update({
                "llm_effort": normalized,
                "current_effort": normalized,
            }))
            return jsonify({
                "conversation": updated,
                "assistant": f"Effort updated to `{normalized}` for this session.",
                "setting": {"key": "effort", "value": normalized},
            }), 200

        conv_job_ids = set(conv.get("job_ids", []) or [])
        invalid_refs = [ref for ref in normalized_refs if ref not in conv_job_ids]
        if invalid_refs:
            return jsonify({"error": "some task_refs do not belong to this conversation", "invalid_task_refs": invalid_refs}), 400

        lock = get_conversation_lock(conversation_id)
        if not lock.acquire(blocking=False):
            if force_send:
                return jsonify({"error": "conversation is actively processing; force send unavailable until current request finishes"}), 409
            return jsonify({"error": "conversation busy"}), 409

        try:
            session_model = get_global_cli_model()
            session_effort = resolve_session_effort(conv.get("llm_effort"))
            reset_agent_activity(conversation_id, None)
            update_conversation(conversation_id, lambda c: c.update({"status": "running"}))

            refs_payload = []
            ref_sources = {}
            
            # Build task reference payloads
            for job_id in normalized_refs:
                from routes.tasks import _build_task_reference_payload
                stdout_text, source = _build_task_reference_payload(conversation_id, conv, job_id, lines=400)
                ref_sources[job_id] = source
                refs_payload.append({"stdout": stdout_text})

            run_action_nonce = new_action_nonce()
            prompt_base = build_prompt_with_memory(conv, text)
            prompt_text = build_prompt_with_task_refs(with_run_job_skill_instruction(prompt_base, run_action_nonce), refs_payload)
            
            append_message(conversation_id, "user", text, {
                "task_refs": normalized_refs,
                "task_ref_sources": ref_sources,
            })

            result = acp_prompt_session(
                agent_path=(get_agent_path_func() if callable(get_agent_path_func) else "claude"),
                cwd=conv["cwd"],
                mode=conv.get("mode", "agent"),
                text=prompt_text,
                cursor_session_id=conv.get("cursor_session_id"),
                preferred_model=session_model,
                effort=session_effort,
                llm_provider=get_global_llm_provider(),
                on_progress_event=lambda event: record_agent_event(conversation_id, event),
            )

            model_used = str(result.get("model") or "").strip()
            effort_used = resolve_session_effort(result.get("effort") or session_effort)
            context_tokens = result.get("context_tokens")
            context_window = result.get("context_window")

            def set_runtime_metadata(c: dict):
                if not c.get("cursor_session_id"):
                    c["cursor_session_id"] = result["cursor_session_id"]
                c["llm_model"] = session_model
                c["llm_effort"] = resolve_session_effort(c.get("llm_effort") or session_effort)
                c["current_model"] = model_used or session_model
                c["current_effort"] = effort_used
                if isinstance(context_tokens, int) and context_tokens >= 0:
                    c["current_context_tokens"] = context_tokens
                if isinstance(context_window, int) and context_window > 0:
                    c["current_context_window"] = context_window
                if c.get("memory_summary_pending"):
                    c["memory_summary_pending"] = False

            update_conversation(conversation_id, set_runtime_metadata)

            assistant_raw_text = result["text"] or f"[No text returned; stopReason={result['stop_reason']}]"
            assistant_text, should_run_job = extract_run_job_action(assistant_raw_text, run_action_nonce)
            if not assistant_text:
                assistant_text = "[Action received: run job]"
            if not (should_run_job and assistant_text == "[Action received: run job]"):
                append_message(conversation_id, "assistant", assistant_text)

            triggered_job = None
            if should_run_job:
                append_message(conversation_id, "system", "action: run job", {
                    "system_event": "agent_action_run_job",
                })
                
                # Trigger run job
                from routes.tasks import zhh_run_job
                run_result = zhh_run_job(args="", cwd=conv["cwd"])
                if run_result.get("ok"):
                    triggered_job = run_result.get("job_id")
                    job_data = run_result.get("payload", {})
                    
                    def add_job(c: dict):
                        job_ids = c.setdefault("job_ids", [])
                        if triggered_job not in job_ids:
                            job_ids.append(triggered_job)
                        from core.tasks import normalize_task_status
                        task_meta = c.setdefault("task_meta", {})
                        entry = task_meta.get(triggered_job, {})
                        if not isinstance(entry, dict):
                            entry = {}
                        else:
                            entry = dict(entry)
                        entry["last_status"] = normalize_task_status(job_data.get("status") or "starting")
                        entry["updated_at"] = utc_now()
                        for key in ("zhh_args", "created_at", "final_log_file", "pane_log_file", "command", "cwd"):
                            value = job_data.get(key)
                            if value is not None and value != "":
                                entry[key] = value
                        task_meta[triggered_job] = entry

                    update_conversation(conversation_id, add_job)
                    append_message(conversation_id, "system", f"Runned job {triggered_job}", {
                        "system_event": "task_run",
                        "job_id": triggered_job,
                        "job_status": str(job_data.get("status") or "starting"),
                        "zhh_args": str(job_data.get("zhh_args") or ""),
                        "auto_run_by_agent": True,
                    })
                else:
                    run_err = run_result.get("error", "unknown error")
                    append_message(conversation_id, "system", f"Agent requested run job, but /run failed: {run_err}", {
                        "system_event": "task_run_failed",
                        "auto_run_by_agent": True,
                    })

            maybe_autoname(conversation_id)
            updated = update_conversation(conversation_id, lambda c: c.update({"status": "idle"}))
            finish_agent_activity(conversation_id)
            return jsonify({
                "conversation": updated,
                "assistant": assistant_text,
                "stop_reason": result["stop_reason"],
                "triggered_job_id": triggered_job,
            })
        except Exception as e:
            err_text = _compact_error_text(str(e).strip() or f"{type(e).__name__}: unknown error")
            note_usage_limit_error(err_text)
            update_conversation(conversation_id, lambda c: c.update({"status": "error", "last_error": err_text}))
            finish_agent_activity(conversation_id, f"Run failed: {err_text}")
            return jsonify({"error": err_text}), 500
        finally:
            lock.release()

    @app.route("/api/runtime/model-policy", methods=["GET"])
    def api_model_policy():
        from runtime.acp_runtime import get_model_policy_status
        return jsonify(get_model_policy_status())
