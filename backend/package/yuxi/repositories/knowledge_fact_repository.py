from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from yuxi.knowledge.graphs.graph_utils import normalize_entity_name
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import KnowledgeFact
from yuxi.utils import hashstr

FACT_STATUSES = (KnowledgeFact.STATUS_PENDING, KnowledgeFact.STATUS_APPROVED, KnowledgeFact.STATUS_REJECTED)
LIST_STATUS_FILTERS = (*FACT_STATUSES, "invalidated", "all")


def compute_fact_id(kb_id: str, chunk_id: str, relation_index: int) -> str:
    return hashstr(f"{kb_id}:{chunk_id}:{relation_index}", length=32)


def find_supersede_candidates(new_fact, other_approved: list) -> list:
    """纯函数：找出会被新批准事实取代的既有批准事实。

    保守规则：同一知识库内，归一化主语、谓词、归一化宾语都相同（同一条陈述的
    重新断言）且尚未失效的批准事实。宾语不同的并行事实（如多个参会单位）不是
    冲突，不取代——功能性谓词的冲突裁决需要本体基数（后续治理阶段）。
    """
    subject = normalize_entity_name(new_fact.subject_text or "")
    predicate = new_fact.predicate
    object_key = normalize_entity_name(new_fact.object_text or "")
    candidates = []
    for other in other_approved:
        if other.id == new_fact.id:
            continue
        if (
            normalize_entity_name(other.subject_text or "") == subject
            and other.predicate == predicate
            and normalize_entity_name(other.object_text or "") == object_key
        ):
            candidates.append(other)
    return candidates


def _is_valid_confidence(confidence: Any) -> bool:
    return isinstance(confidence, int | float) and not isinstance(confidence, bool)


def merge_fact_decisions(
    existing_status_by_index: dict[int, str],
    fact_inputs: list[dict[str, Any]],
    auto_approve_threshold: float | None,
) -> tuple[list[dict[str, Any]], set[int]]:
    """纯函数：决定一个 chunk 各 relation 的落库状态。

    - 已裁决的 relation（approved/rejected）保持裁决不变，-approved 的照常返回，
      让管线每次都投影全部已批准事实（下游 MERGE 幂等）；
    - pending 或新增的 relation 重新计算：confidence >= threshold 直接 approved
      （decided_by='auto'），否则进入人审队列。

    返回 (待写入行, 本轮 approved 的 relation_index 集合)。
    """
    rows: list[dict[str, Any]] = []
    approved_indexes: set[int] = set()
    for item in fact_inputs:
        index = int(item["relation_index"])
        current_status = existing_status_by_index.get(index)
        if current_status in (KnowledgeFact.STATUS_APPROVED, KnowledgeFact.STATUS_REJECTED):
            if current_status == KnowledgeFact.STATUS_APPROVED:
                approved_indexes.add(index)
            continue

        confidence = item.get("confidence")
        auto_approved = (
            auto_approve_threshold is not None
            and _is_valid_confidence(confidence)
            and float(confidence) >= float(auto_approve_threshold)
        )
        rows.append(
            {
                **item,
                "status": KnowledgeFact.STATUS_APPROVED if auto_approved else KnowledgeFact.STATUS_PENDING,
                "decided_at": datetime.now(UTC) if auto_approved else None,
                "decided_by": "auto" if auto_approved else None,
            }
        )
        if auto_approved:
            approved_indexes.add(index)
    return rows, approved_indexes


class KnowledgeFactRepository:
    async def enqueue_chunk_facts(
        self,
        *,
        kb_id: str,
        file_id: str,
        chunk_id: str,
        fact_inputs: list[dict[str, Any]],
        auto_approve_threshold: float | None,
    ) -> set[int]:
        """把一个 chunk 的抽取结果写入事实队列，返回 approved 的 relation_index 集合。

        幂等语义：同 chunk 重跑时，已裁决的事实不被覆盖；pending 事实按最新抽取结果刷新。
        """
        if not fact_inputs:
            return set()
        async with pg_manager.get_async_session_context() as session:
            existing_rows = (
                await session.execute(
                    select(KnowledgeFact.relation_index, KnowledgeFact.status).where(
                        KnowledgeFact.kb_id == kb_id,
                        KnowledgeFact.chunk_id == chunk_id,
                    )
                )
            ).all()
            existing_status_by_index = {int(index): str(status) for index, status in existing_rows}
            rows, approved_indexes = merge_fact_decisions(existing_status_by_index, fact_inputs, auto_approve_threshold)
            if rows:
                values = []
                for row in rows:
                    relation_index = int(row["relation_index"])
                    values.append(
                        {
                            "fact_id": compute_fact_id(kb_id, chunk_id, relation_index),
                            "kb_id": kb_id,
                            "file_id": file_id,
                            "chunk_id": chunk_id,
                            **row,
                        }
                    )
                stmt = insert(KnowledgeFact).values(values)
                await session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["kb_id", "chunk_id", "relation_index"],
                        set_={
                            "subject_text": stmt.excluded.subject_text,
                            "subject_label": stmt.excluded.subject_label,
                            "predicate": stmt.excluded.predicate,
                            "object_text": stmt.excluded.object_text,
                            "object_label": stmt.excluded.object_label,
                            "evidence_text": stmt.excluded.evidence_text,
                            "confidence": stmt.excluded.confidence,
                            "status": stmt.excluded.status,
                            "decided_at": stmt.excluded.decided_at,
                            "decided_by": stmt.excluded.decided_by,
                            "updated_at": func.now(),
                        },
                    )
                )
            return approved_indexes

    async def get_fact(self, kb_id: str, fact_id: str) -> KnowledgeFact | None:
        async with pg_manager.get_async_session_context() as session:
            return (
                await session.execute(
                    select(KnowledgeFact).where(KnowledgeFact.kb_id == kb_id, KnowledgeFact.fact_id == fact_id)
                )
            ).scalar_one_or_none()

    async def decide_fact(
        self,
        *,
        kb_id: str,
        fact_id: str,
        decision: str,
        decided_by: str,
    ) -> tuple[KnowledgeFact | None, bool, int]:
        """裁决一条 pending 事实。终态语义：approved/rejected 不可再改，重复裁决是幂等 no-op。

        批准时应用纠正链：同一条陈述（同归一化主语+谓词+宾语）此前已批准且未失效的
        事实被新版本取代——旧事实打上 invalidated_at（删除是事件，不覆盖行），新事实
        通过 supersedes_id 链接到被取代的最近版本。

        返回 (事实, 是否发生状态变化, 被取代的事实数)；事实不存在返回 (None, False, 0)。
        """
        target_status = KnowledgeFact.STATUS_APPROVED if decision == "approve" else KnowledgeFact.STATUS_REJECTED
        async with pg_manager.get_async_session_context() as session:
            fact = (
                await session.execute(
                    select(KnowledgeFact).where(KnowledgeFact.kb_id == kb_id, KnowledgeFact.fact_id == fact_id)
                )
            ).scalar_one_or_none()
            if fact is None:
                return None, False, 0
            if fact.status != KnowledgeFact.STATUS_PENDING:
                return fact, False, 0
            fact.status = target_status
            fact.decided_at = datetime.now(UTC)
            fact.decided_by = decided_by
            superseded_count = 0
            if target_status == KnowledgeFact.STATUS_APPROVED:
                other_approved = (
                    (
                        await session.execute(
                            select(KnowledgeFact)
                            .where(
                                KnowledgeFact.kb_id == kb_id,
                                KnowledgeFact.status == KnowledgeFact.STATUS_APPROVED,
                                KnowledgeFact.invalidated_at.is_(None),
                                KnowledgeFact.id != fact.id,
                            )
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .all()
                )
                candidates = find_supersede_candidates(fact, list(other_approved))
                if candidates:
                    newest = max(candidates, key=lambda item: item.decided_at or item.recorded_at)
                    fact.supersedes_id = newest.id
                    invalidated_at = datetime.now(UTC)
                    for candidate in candidates:
                        candidate.invalidated_at = invalidated_at
                    superseded_count = len(candidates)
            return fact, True, superseded_count

    async def list_facts(
        self,
        *,
        kb_id: str,
        status: str | None = None,
        file_id: str | None = None,
        page: int = 1,
        page_size: int = 20,
        as_of: datetime | None = None,
        subject: str | None = None,
    ) -> dict[str, Any]:
        """列出事实。

        - status: pending/approved/rejected；'invalidated' 已失效（approved 且被
          取代/推翻）；'all' 不过滤；
        - as_of: 给定时点做世界轴判定——只返回该时点为真的已批准事实
          （未被推翻、已生效、未终结；precision='unknown' 的结束端无法证伪，按成立处理）；
        - subject: 按归一化主语过滤（历史/时间线查询入口）。
        """
        conditions = [KnowledgeFact.kb_id == kb_id]
        if as_of is not None:
            conditions.append(KnowledgeFact.status == KnowledgeFact.STATUS_APPROVED)
            conditions.append(or_(KnowledgeFact.invalidated_at.is_(None), KnowledgeFact.invalidated_at > as_of))
            conditions.append(or_(KnowledgeFact.valid_from.is_(None), KnowledgeFact.valid_from <= as_of))
            conditions.append(
                or_(
                    KnowledgeFact.valid_to.is_(None),
                    and_(
                        KnowledgeFact.valid_to_precision == "unknown",
                    ),
                    KnowledgeFact.valid_to > as_of,
                )
            )
        elif status and status not in ("all",):
            if status == "invalidated":
                conditions.append(KnowledgeFact.status == KnowledgeFact.STATUS_APPROVED)
                conditions.append(KnowledgeFact.invalidated_at.is_not(None))
            else:
                conditions.append(KnowledgeFact.status == status)
                if status == KnowledgeFact.STATUS_APPROVED:
                    conditions.append(KnowledgeFact.invalidated_at.is_(None))
        if file_id:
            conditions.append(KnowledgeFact.file_id == file_id)
        if subject:
            conditions.append(func.lower(KnowledgeFact.subject_text) == normalize_entity_name(subject))
        order_by = KnowledgeFact.recorded_at.desc() if as_of is not None or subject else KnowledgeFact.id.desc()
        async with pg_manager.get_async_session_context() as session:
            total = await session.scalar(select(func.count()).select_from(KnowledgeFact).where(*conditions))
            rows = (
                (
                    await session.execute(
                        select(KnowledgeFact)
                        .where(*conditions)
                        .order_by(order_by)
                        .offset(max(page - 1, 0) * page_size)
                        .limit(page_size)
                    )
                )
                .scalars()
                .all()
            )
            count_rows = (
                await session.execute(
                    select(KnowledgeFact.status, func.count())
                    .where(KnowledgeFact.kb_id == kb_id)
                    .group_by(KnowledgeFact.status)
                )
            ).all()
        status_counts = {status_name: 0 for status_name in FACT_STATUSES}
        for status_name, count in count_rows:
            status_counts[str(status_name)] = int(count or 0)
        invalidated_count = (
            await session.scalar(
                select(func.count())
                .select_from(KnowledgeFact)
                .where(
                    KnowledgeFact.kb_id == kb_id,
                    KnowledgeFact.status == KnowledgeFact.STATUS_APPROVED,
                    KnowledgeFact.invalidated_at.is_not(None),
                )
            )
            or 0
        )
        status_counts["invalidated"] = int(invalidated_count)
        return {
            "items": [self.serialize_fact(row) for row in rows],
            "total": int(total or 0),
            "status_counts": status_counts,
            "page": page,
            "page_size": page_size,
        }

    async def count_facts_by_status(self, kb_id: str) -> dict[str, int]:
        async with pg_manager.get_async_session_context() as session:
            rows = (
                await session.execute(
                    select(KnowledgeFact.status, func.count())
                    .where(KnowledgeFact.kb_id == kb_id)
                    .group_by(KnowledgeFact.status)
                )
            ).all()
        counts = {status_name: 0 for status_name in FACT_STATUSES}
        for status_name, count in rows:
            counts[str(status_name)] = int(count or 0)
        return counts

    async def delete_by_kb_id(self, kb_id: str) -> int:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(delete(KnowledgeFact).where(KnowledgeFact.kb_id == kb_id))
            return int(result.rowcount or 0)

    @staticmethod
    def serialize_fact(fact: KnowledgeFact) -> dict[str, Any]:
        def _iso(value: datetime | None) -> str | None:
            return value.isoformat() if value is not None else None

        return {
            "fact_id": fact.fact_id,
            "kb_id": fact.kb_id,
            "file_id": fact.file_id,
            "chunk_id": fact.chunk_id,
            "relation_index": fact.relation_index,
            "subject_text": fact.subject_text,
            "subject_label": fact.subject_label,
            "predicate": fact.predicate,
            "object_text": fact.object_text,
            "object_label": fact.object_label,
            "evidence_text": fact.evidence_text,
            "confidence": fact.confidence,
            "status": fact.status,
            "decided_at": _iso(fact.decided_at),
            "decided_by": fact.decided_by,
            "valid_from": _iso(fact.valid_from),
            "valid_from_precision": fact.valid_from_precision,
            "valid_to": _iso(fact.valid_to),
            "valid_to_precision": fact.valid_to_precision,
            "recorded_at": _iso(fact.recorded_at),
            "invalidated_at": _iso(fact.invalidated_at),
            "supersedes_id": fact.supersedes_id,
            "created_at": _iso(fact.created_at),
        }
