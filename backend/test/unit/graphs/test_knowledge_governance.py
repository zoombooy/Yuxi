from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql.dml import Insert

from yuxi.knowledge.graphs.extractors import normalize_extraction_result
from yuxi.knowledge.graphs.extractors.base import (
    MAX_ENTITY_LABEL_LENGTH,
    MAX_ENTITY_NAME_LENGTH,
    MAX_RELATION_TYPE_LENGTH,
)
from yuxi.knowledge.graphs.milvus_graph_service import DEFAULT_AUTO_APPROVE_THRESHOLD, MilvusGraphService
from yuxi.repositories import knowledge_fact_repository as fact_repo_module
from yuxi.repositories.knowledge_fact_repository import (
    KnowledgeFactRepository,
    compute_fact_id,
    merge_fact_decisions,
)
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeFact,
    KnowledgeGraphEntity,
    KnowledgeGraphTriple,
)


# ─── normalize_extraction_result 的 confidence 透传 ─────────────────


def _minimal_relation(confidence) -> dict:
    return {
        "entities": [{"text": "张三"}, {"text": "公司"}],
        "relations": [
            {"source": "张三", "target": "公司", "text": "任职于", "confidence": confidence},
        ],
    }


@pytest.mark.parametrize(
    "raw, expected",
    [
        (0.9, 0.9),
        (1, 1.0),
        ("0.5", None),
        (True, None),
        (False, None),
        (None, None),
        (1.5, 1.0),
        (-0.2, 0.0),
    ],
)
def test_normalize_extraction_result_carries_confidence(raw, expected):
    result = normalize_extraction_result(_minimal_relation(raw), "llm")
    assert result["relations"][0]["confidence"] == expected


def test_normalize_extraction_result_confidence_absent_by_default():
    result = normalize_extraction_result(
        {
            "entities": [{"text": "张三"}, {"text": "公司"}],
            "relations": [{"source": "张三", "target": "公司", "text": "任职于"}],
        },
        "llm",
    )
    assert result["relations"][0]["confidence"] is None


# ─── 事实审核状态机（纯函数） ────────────────────────────────────────


def _fact_input(index: int, confidence=None) -> dict:
    return {
        "relation_index": index,
        "subject_text": "张三",
        "subject_label": "Person",
        "predicate": "WORKS_AT",
        "object_text": "公司",
        "object_label": "Organization",
        "evidence_text": "任职于",
        "confidence": confidence,
    }


def test_merge_fact_decisions_auto_approves_above_threshold():
    rows, approved = merge_fact_decisions({}, [_fact_input(0, 0.95), _fact_input(1, 0.5)], 0.8)
    assert [row["status"] for row in rows] == ["approved", "pending"]
    assert approved == {0}
    assert rows[0]["decided_by"] == "auto"
    assert rows[1]["decided_by"] is None


def test_merge_fact_decisions_keeps_terminal_decisions():
    existing = {0: "approved", 1: "rejected"}
    rows, approved = merge_fact_decisions(existing, [_fact_input(0, 0.1), _fact_input(1, 0.99)], 0.8)
    assert rows == []
    # 已批准的事实每次都要回到投影集合里（下游 MERGE 幂等）
    assert approved == {0}


def test_merge_fact_decisions_without_threshold_all_pending():
    rows, approved = merge_fact_decisions({}, [_fact_input(0, 1.0)], None)
    assert [row["status"] for row in rows] == ["pending"]
    assert approved == set()


def test_merge_fact_decisions_invalid_confidence_never_auto_approves():
    rows, approved = merge_fact_decisions({}, [_fact_input(0, None), _fact_input(1, "0.99")], 0.8)
    assert all(row["status"] == "pending" for row in rows)
    assert approved == set()


def test_compute_fact_id_is_stable():
    assert compute_fact_id("kb", "chunk", 3) == compute_fact_id("kb", "chunk", 3)
    assert compute_fact_id("kb", "chunk", 3) != compute_fact_id("kb", "chunk", 4)


# ─── 管线挂钩：事实入参映射与投影前过滤 ──────────────────────────────


def _normalized_result() -> dict:
    return normalize_extraction_result(
        {
            "entities": [{"text": "张三"}, {"text": "公司"}, {"text": "北京"}],
            "relations": [
                {"source": "张三", "target": "公司", "text": "任职于", "label": "WORKS_AT"},
                {"source": "公司", "target": "北京", "text": "位于", "label": "LOCATED_IN"},
            ],
        },
        "llm",
    )


def test_build_fact_inputs_maps_relations_in_order():
    inputs = MilvusGraphService._build_fact_inputs(_normalized_result())
    assert [item["relation_index"] for item in inputs] == [0, 1]
    assert inputs[0]["subject_text"] == "张三"
    assert inputs[0]["predicate"] == "WORKS_AT"
    assert inputs[1]["object_text"] == "北京"
    assert all(item["confidence"] is None for item in inputs)


def test_filter_extraction_result_prunes_unapproved_entities():
    result = _normalized_result()
    filtered = MilvusGraphService._filter_extraction_result(result, {0})
    assert [relation["label"] for relation in filtered["relations"]] == ["WORKS_AT"]
    entity_texts = {entity["text"] for entity in filtered["entities"]}
    assert entity_texts == {"张三", "公司"}
    assert filtered["metadata"]["extractor_type"] == "llm"


def test_filter_extraction_result_with_no_approval_is_empty():
    filtered = MilvusGraphService._filter_extraction_result(_normalized_result(), set())
    assert filtered["relations"] == []
    assert filtered["entities"] == []


# ─── 治理配置 ────────────────────────────────────────────────────────


def test_get_governance_config_defaults_to_disabled():
    kb = SimpleNamespace(additional_params=None)
    config = MilvusGraphService.get_governance_config(kb)
    assert config == {
        "enabled": False,
        "auto_approve_threshold": DEFAULT_AUTO_APPROVE_THRESHOLD,
    }


def test_get_governance_config_clamps_invalid_threshold():
    kb = SimpleNamespace(additional_params={"governance_config": {"enabled": True, "auto_approve_threshold": "bad"}})
    assert MilvusGraphService.get_governance_config(kb)["auto_approve_threshold"] == DEFAULT_AUTO_APPROVE_THRESHOLD
    kb = SimpleNamespace(additional_params={"governance_config": {"enabled": True, "auto_approve_threshold": 9.5}})
    assert MilvusGraphService.get_governance_config(kb)["auto_approve_threshold"] == 1.0


# ─── 列长度与图谱实体/三元组对齐（防止将来漂移） ─────────────────────


def test_knowledge_fact_column_lengths_match_extractor_limits():
    assert MAX_ENTITY_NAME_LENGTH == KnowledgeFact.subject_text.type.length
    assert MAX_ENTITY_NAME_LENGTH == KnowledgeFact.object_text.type.length
    assert MAX_ENTITY_LABEL_LENGTH == KnowledgeFact.subject_label.type.length
    assert MAX_ENTITY_LABEL_LENGTH == KnowledgeFact.object_label.type.length
    assert MAX_RELATION_TYPE_LENGTH == KnowledgeFact.predicate.type.length
    assert MAX_ENTITY_NAME_LENGTH == KnowledgeGraphEntity.name.type.length
    assert MAX_RELATION_TYPE_LENGTH == KnowledgeGraphTriple.relation_type.type.length


# ─── 仓库：入队幂等与裁决状态迁移 ────────────────────────────────────


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    def __init__(self, select_rows):
        self._select_rows = select_rows
        self.insert_values: list[dict] = []
        self.execute_count = 0

    async def execute(self, statement):
        self.execute_count += 1
        if isinstance(statement, Insert):
            compiled = statement.compile(dialect=postgresql.dialect())
            values = [value for value in compiled.params.values() if value in {"pending", "approved", "rejected"}]
            self.insert_values = values
            return FakeResult([])
        return FakeResult(self._select_rows)


@pytest.mark.asyncio
async def test_enqueue_chunk_facts_reports_approved_indexes(monkeypatch):
    session = FakeSession(select_rows=[])

    @asynccontextmanager
    async def fake_session_context():
        yield session

    monkeypatch.setattr(fact_repo_module.pg_manager, "get_async_session_context", fake_session_context)

    approved = await KnowledgeFactRepository().enqueue_chunk_facts(
        kb_id="kb",
        file_id="file",
        chunk_id="chunk",
        fact_inputs=[_fact_input(0, 0.95), _fact_input(1, 0.5)],
        auto_approve_threshold=0.8,
    )

    assert approved == {0}
    assert sorted(session.insert_values) == ["approved", "pending"]


@pytest.mark.asyncio
async def test_enqueue_chunk_facts_skips_decided_relations(monkeypatch):
    session = FakeSession(select_rows=[(0, "rejected"), (1, "approved")])

    @asynccontextmanager
    async def fake_session_context():
        yield session

    monkeypatch.setattr(fact_repo_module.pg_manager, "get_async_session_context", fake_session_context)

    approved = await KnowledgeFactRepository().enqueue_chunk_facts(
        kb_id="kb",
        file_id="file",
        chunk_id="chunk",
        fact_inputs=[_fact_input(0, 0.99), _fact_input(1, 0.99), _fact_input(2, 0.5)],
        auto_approve_threshold=0.8,
    )

    # 0 保持 rejected 不重写，1 保持 approved 继续投影，2 新增 pending
    assert approved == {1}
    assert session.insert_values == ["pending"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "current_status, decision, expect_changed",
    [
        ("pending", "approve", True),
        ("pending", "reject", True),
        ("approved", "approve", False),
        ("rejected", "reject", False),
        ("approved", "reject", False),
        ("rejected", "approve", False),
    ],
)
async def test_decide_fact_is_terminal_and_idempotent(monkeypatch, current_status, decision, expect_changed):
    fact = SimpleNamespace(
        fact_id="f1",
        kb_id="kb",
        status=current_status,
        decided_at=None,
        decided_by=None,
    )
    session = FakeSession(select_rows=[fact])

    @asynccontextmanager
    async def fake_session_context():
        yield session

    monkeypatch.setattr(fact_repo_module.pg_manager, "get_async_session_context", fake_session_context)

    result, changed = await KnowledgeFactRepository().decide_fact(
        kb_id="kb", fact_id="f1", decision=decision, decided_by="user-1"
    )

    assert changed is expect_changed
    if expect_changed:
        assert result.status == ("approved" if decision == "approve" else "rejected")
        assert result.decided_by == "user-1"
    else:
        assert result.status == current_status


@pytest.mark.asyncio
async def test_decide_fact_missing_fact_returns_none(monkeypatch):
    session = FakeSession(select_rows=[])

    @asynccontextmanager
    async def fake_session_context():
        yield session

    monkeypatch.setattr(fact_repo_module.pg_manager, "get_async_session_context", fake_session_context)

    result, changed = await KnowledgeFactRepository().decide_fact(
        kb_id="kb", fact_id="missing", decision="approve", decided_by="user-1"
    )
    assert result is None
    assert changed is False


def test_serialize_fact_shapes_api_payload():
    fact = SimpleNamespace(
        fact_id="f1",
        kb_id="kb",
        file_id="file",
        chunk_id="chunk",
        relation_index=0,
        subject_text="张三",
        subject_label="Person",
        predicate="WORKS_AT",
        object_text="公司",
        object_label="Organization",
        evidence_text="任职于",
        confidence=0.42,
        status="pending",
        decided_at=None,
        decided_by=None,
        valid_from=None,
        valid_from_precision=None,
        valid_to=None,
        valid_to_precision=None,
        recorded_at=None,
        invalidated_at=None,
        created_at=None,
    )
    payload = KnowledgeFactRepository.serialize_fact(fact)
    assert payload["fact_id"] == "f1"
    assert payload["confidence"] == 0.42
    assert payload["status"] == "pending"
    assert payload["recorded_at"] is None
