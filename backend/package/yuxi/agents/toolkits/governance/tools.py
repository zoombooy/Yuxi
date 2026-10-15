"""知识治理事实查询工具

让智能体具备回答"时点"与"时间线"问题的能力：
- query_knowledge_facts：查询知识库中在指定时点成立的事实（世界轴判定）
- knowledge_fact_history：查询某实体的知识演变历史（含被取代/失效的事实）
"""

from datetime import datetime, UTC
from typing import Any

from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import BaseModel, Field

from yuxi.agents.toolkits.registry import tool
from yuxi.repositories.knowledge_fact_repository import KnowledgeFactRepository
from yuxi.utils import logger

_PAGE_SIZE = 50


def _resolve_target_kb_id(kb_name: str, runtime: ToolRuntime | None) -> tuple[str | None, str | None]:
    """按名称在会话可见知识库中定位 kb_id，返回 (kb_id, error)。"""
    from yuxi.agents.toolkits.kbs.tools import _resolve_visible_knowledge_bases_for_query

    if not kb_name or not kb_name.strip():
        return None, "请提供知识库名称"
    visible_kbs = _resolve_visible_knowledge_bases_for_query(runtime)
    if not visible_kbs:
        return None, "无法获取当前会话可访问的知识库"
    target = next((kb for kb in visible_kbs if kb.get("name") == kb_name.strip()), None)
    if not target:
        return None, f"知识库 '{kb_name}' 不存在或当前会话未启用"
    return target["kb_id"], None


def _parse_query_time(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _period_text(fact: dict[str, Any]) -> str:
    def _fmt(value: str | None, precision: str | None) -> str:
        if not value:
            return ""
        prefix = value[:10]
        if precision == "year":
            return prefix[:4]
        if precision == "month":
            return prefix[:7]
        return prefix

    start = _fmt(fact.get("valid_from"), fact.get("valid_from_precision"))
    end = _fmt(fact.get("valid_to"), fact.get("valid_to_precision"))
    if fact.get("valid_to_precision") == "unknown":
        end = "未知时间结束"
    if start and end:
        return f"{start} ~ {end}"
    if start:
        return f"自 {start}"
    if end:
        return f"至 {end} 前有效"
    return "原文未给出时间"


class QueryKnowledgeFactsInput(BaseModel):
    """时点查询输入模型"""

    kb_name: str = Field(description="知识库名称")
    query_time: str = Field(
        default="",
        description="查询时点，ISO 格式如 2024-03-15 或 2024-03-15T00:00:00；留空表示查询当前成立的事实",
    )
    subject: str = Field(default="", description="按主语实体名称过滤（可选）")
    predicate: str = Field(default="", description="按关系类型过滤（可选）")


@tool(
    category="knowledge",
    tags=["知识库", "知识治理"],
    display_name="查询知识事实",
    args_schema=QueryKnowledgeFactsInput,
)
async def query_knowledge_facts(
    kb_name: str,
    query_time: str = "",
    subject: str = "",
    predicate: str = "",
    runtime: ToolRuntime = None,
) -> str:
    """查询知识库中在指定时点成立的事实

    回答"某个时间点 X 是什么状态"类问题时使用。只返回该时点仍成立（未被推翻、
    在有效期内）的已批准事实。例：查询 2024-03-15 时某公司的供应商是谁。

    Args:
        kb_name: 知识库名称
        query_time: 查询时点（ISO 格式），留空表示当前
        subject: 按主语实体过滤（可选）
        predicate: 按关系类型过滤（可选）
    """
    try:
        target_kb_id, error = _resolve_target_kb_id(kb_name, runtime)
        if error:
            return error
        as_of = _parse_query_time(query_time)
    except ValueError:
        return f"query_time 格式无效: {query_time}，请使用 ISO 格式如 2024-03-15"
    except Exception as e:
        logger.error(f"解析治理查询参数失败: {e}")
        return "查询参数解析失败"

    try:
        result = await KnowledgeFactRepository().list_facts(
            kb_id=target_kb_id,
            as_of=as_of,
            subject=subject or None,
            page=1,
            page_size=_PAGE_SIZE,
        )
    except Exception as e:
        logger.error(f"查询知识事实失败: {e}")
        return f"查询知识事实失败: {str(e)}"

    items = result.get("items") or []
    if predicate:
        wanted = predicate.strip().upper()
        items = [item for item in items if str(item.get("predicate") or "").upper() == wanted]

    if not items:
        time_label = query_time.strip() or "当前"
        return f"知识库 '{kb_name}' 中在 {time_label} 时点没有成立的事实记录。"

    lines = [f"知识库 '{kb_name}' 在 {as_of.date().isoformat()} 时点成立的事实（共 {len(items)} 条）："]
    for fact in items:
        line = f"- {fact['subject_text']} {fact['predicate']} {fact['object_text']}"
        period = _period_text(fact)
        if period and period != "原文未给出时间":
            line += f"（{period}）"
        if fact.get("evidence_text"):
            line += f" 依据: {fact['evidence_text']}"
        lines.append(line)
    return "\n".join(lines)


class KnowledgeFactHistoryInput(BaseModel):
    """事实历史输入模型"""

    kb_name: str = Field(description="知识库名称")
    subject: str = Field(description="主语实体名称，例如某个公司或人名")


@tool(
    category="knowledge",
    tags=["知识库", "知识治理"],
    display_name="知识事实历史",
    args_schema=KnowledgeFactHistoryInput,
)
async def knowledge_fact_history(kb_name: str, subject: str, runtime: ToolRuntime = None) -> str:
    """查询某实体的知识演变时间线

    回答"X 的信息发生过什么变化"、"我们关于 X 的认知如何演变"类问题时使用。
    返回该实体相关的全部事实（含已失效/被取代的），按进入系统的时间排序，并标注
    每条事实的状态与裁决记录。

    Args:
        kb_name: 知识库名称
        subject: 主语实体名称
    """
    target_kb_id, error = _resolve_target_kb_id(kb_name, runtime)
    if error:
        return error
    if not subject or not subject.strip():
        return "请提供要查询的实体名称"

    try:
        result = await KnowledgeFactRepository().list_facts(
            kb_id=target_kb_id,
            subject=subject,
            status="all",
            page=1,
            page_size=_PAGE_SIZE,
        )
    except Exception as e:
        logger.error(f"查询事实历史失败: {e}")
        return f"查询事实历史失败: {str(e)}"

    items = result.get("items") or []
    if not items:
        return f"知识库 '{kb_name}' 中没有关于 '{subject}' 的事实记录。"

    status_labels = {
        "approved": "已批准",
        "rejected": "已驳回",
        "pending": "待审核",
        "invalidated": "已失效",
    }
    lines = [f"知识库 '{kb_name}' 中关于 '{subject}' 的知识演变（按进入系统时间排序，共 {len(items)} 条）："]
    for fact in items:
        status = fact.get("status") or "pending"
        if status == "approved" and fact.get("invalidated_at"):
            status = "invalidated"
        line = f"- [{status_labels.get(status, status)}] {fact['recorded_at'][:16]} 记录: "
        line += f"{fact['subject_text']} {fact['predicate']} {fact['object_text']}"
        period = _period_text(fact)
        if period and period != "原文未给出时间":
            line += f"（{period}）"
        if fact.get("decided_at"):
            decider = fact.get("decided_by") or "未知"
            decision = "批准" if status in ("approved", "invalidated") else "驳回"
            line += f" | {fact['decided_at'][:16]} 由 {decider} {decision}"
        if status == "invalidated":
            line += " | 已被更新版本取代"
        if fact.get("evidence_text"):
            line += f" 依据: {fact['evidence_text']}"
        lines.append(line)
    return "\n".join(lines)
