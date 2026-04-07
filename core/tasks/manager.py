"""任务状态管理器。"""
from __future__ import annotations

from core.config import ZHH_SERVER_URL
from runtime.tasks_runtime import get_conversation_jobs as get_conversation_jobs_runtime


def get_conversation_jobs(conversation: dict) -> list[dict]:
    """获取单个对话的任务列表（按conversation.job_ids回放并合并实时状态）。"""
    return get_conversation_jobs_runtime(ZHH_SERVER_URL, conversation)
