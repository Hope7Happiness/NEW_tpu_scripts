"""Agent交互相关API路由"""
from __future__ import annotations

from flask import jsonify, request

from core.config import ALLOWED_SESSION_EFFORTS, ZHH_SERVER_URL
from core.utils import _compact_error_text
from core.conversation import (
    get_conversation, update_conversation,
    resolve_session_effort,
    parse_session_setting_command, maybe_autoname
)
from core.conversation.store import append_message
from core.activity import reset_agent_activity, finish_agent_activity, record_agent_event
from core.tasks import get_conversation_jobs, diagnose_completed_jobs_once, update_task_alert_state
from runtime.agent_action_protocol import (
    extract_session_job_actions,
    format_session_job_parse_errors_message,
    new_action_nonce,
)
from core.tasks.session_job_tools import (
    execute_session_job_actions,
    format_session_jobs_user_message_content,
)
from runtime.acp_runtime import acp_prompt_session, note_usage_limit_error
from runtime.agent_prompt_compose import compose_cli_turn_prompt
from runtime.agent_prompts import (
    session_job_followup_core_content,
    session_job_parse_error_autofix_core_content,
)
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

            refs_payload = [{"job_id": jid} for jid in normalized_refs]
            ref_sources = {jid: "session_job query" for jid in normalized_refs}

            append_message(conversation_id, "user", text, {
                "task_refs": normalized_refs,
                "task_ref_sources": ref_sources,
            })
            conv = get_conversation(conversation_id) or conv

            agent_path_resolved = (get_agent_path_func() if callable(get_agent_path_func) else "claude")
            llm_provider = get_global_llm_provider()
            tuples_for_next: list[tuple[str, dict]] | None = None
            triggered_job: str | None = None
            session_triggered_job: str | None = None
            assistant_text = ""
            last_stop = "success"
            followup_count = 0
            first_agent_round = True
            all_session_job_parse_errors: list[str] = []

            def _append_assistant_session_job_turn(at: str, sa: list) -> str:
                """Normalize placeholder and append assistant when appropriate. Returns text for API."""
                t = at
                if not t.strip() and sa:
                    t = "[Action received: session job]"
                skip = bool(sa and t == "[Action received: session job]")
                if not skip and (t.strip() or sa):
                    append_message(conversation_id, "assistant", t)
                return t

            while True:
                if not first_agent_round:
                    if not tuples_for_next:
                        break
                    inj_body = format_session_jobs_user_message_content(tuples_for_next)
                    results_meta = [{"op": o, "payload": p} for o, p in tuples_for_next]
                    append_message(
                        conversation_id,
                        "user",
                        inj_body,
                        {
                            "session_job_injection": True,
                            "session_job_results": results_meta,
                        },
                    )
                    followup_count += 1
                    conv = get_conversation(conversation_id)
                    if not conv:
                        break
                    core_request = session_job_followup_core_content(inj_body)
                    refs_for_round: list = []
                else:
                    core_request = text
                    refs_for_round = refs_payload
                    first_agent_round = False

                session_job_nonce = new_action_nonce()
                prompt_text = compose_cli_turn_prompt(
                    core_request,
                    session_job_nonce,
                    refs_for_round,
                )

                result = acp_prompt_session(
                    agent_path=agent_path_resolved,
                    cwd=conv["cwd"],
                    mode=conv.get("mode", "agent"),
                    text=prompt_text,
                    cursor_session_id=conv.get("cursor_session_id"),
                    preferred_model=session_model,
                    effort=session_effort,
                    llm_provider=llm_provider,
                    on_progress_event=lambda event: record_agent_event(conversation_id, event),
                )
                last_stop = str(result.get("stop_reason") or "success")

                def set_runtime_metadata(c: dict, res: dict):
                    model_used = str(res.get("model") or "").strip()
                    effort_used = resolve_session_effort(res.get("effort") or session_effort)
                    context_tokens = res.get("context_tokens")
                    context_window = res.get("context_window")
                    if not c.get("cursor_session_id"):
                        c["cursor_session_id"] = res["cursor_session_id"]
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

                update_conversation(conversation_id, lambda c: set_runtime_metadata(c, result))
                conv = get_conversation(conversation_id) or conv

                assistant_raw_text = result["text"] or f"[No text returned; stopReason={result['stop_reason']}]"
                at1, sa1, pe1 = extract_session_job_actions(assistant_raw_text, session_job_nonce)

                if pe1:
                    all_session_job_parse_errors.extend(pe1)
                    _append_assistant_session_job_turn(at1, sa1)
                    repair_core = session_job_parse_error_autofix_core_content(
                        format_session_job_parse_errors_message(pe1),
                    )
                    append_message(
                        conversation_id,
                        "user",
                        repair_core,
                        {
                            "session_job_parse_error_autofix": True,
                            "errors": pe1,
                        },
                    )
                    conv = get_conversation(conversation_id) or conv
                    repair_nonce = new_action_nonce()
                    repair_prompt = compose_cli_turn_prompt(repair_core, repair_nonce, [])
                    result = acp_prompt_session(
                        agent_path=agent_path_resolved,
                        cwd=conv["cwd"],
                        mode=conv.get("mode", "agent"),
                        text=repair_prompt,
                        cursor_session_id=conv.get("cursor_session_id"),
                        preferred_model=session_model,
                        effort=session_effort,
                        llm_provider=llm_provider,
                        on_progress_event=lambda event: record_agent_event(conversation_id, event),
                    )
                    last_stop = str(result.get("stop_reason") or "success")
                    update_conversation(conversation_id, lambda c: set_runtime_metadata(c, result))
                    conv = get_conversation(conversation_id) or conv
                    assistant_raw_text = result["text"] or f"[No text returned; stopReason={result['stop_reason']}]"
                    assistant_text, session_actions, pe2 = extract_session_job_actions(
                        assistant_raw_text, repair_nonce,
                    )
                    if pe2:
                        all_session_job_parse_errors.extend(pe2)
                        append_message(
                            conversation_id,
                            "system",
                            format_session_job_parse_errors_message(pe2),
                            {"system_event": "session_job_parse_error", "errors": pe2},
                        )
                    assistant_text = _append_assistant_session_job_turn(assistant_text, session_actions)
                    tuples1, stj1 = execute_session_job_actions(conversation_id, sa1)
                    tuples2, stj2 = execute_session_job_actions(conversation_id, session_actions)
                    tuples_for_next = tuples1 + tuples2
                    stj = stj2 or stj1
                else:
                    assistant_text = _append_assistant_session_job_turn(at1, sa1)
                    tuples_for_next, stj = execute_session_job_actions(conversation_id, sa1)

                if stj:
                    session_triggered_job = stj

                if not tuples_for_next:
                    break

            if triggered_job is None and session_triggered_job:
                triggered_job = session_triggered_job

            maybe_autoname(conversation_id)
            updated = update_conversation(conversation_id, lambda c: c.update({"status": "idle"}))
            finish_agent_activity(conversation_id)
            return jsonify({
                "conversation": updated,
                "assistant": assistant_text,
                "stop_reason": last_stop,
                "triggered_job_id": triggered_job,
                "session_job_followups": followup_count,
                "session_job_parse_errors": all_session_job_parse_errors,
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
