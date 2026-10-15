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
from yuxi.knowledge.graphs.governance_time import fact_holds_at, normalize_time_expression
from yuxi.repositories.knowledge_fact_repository import (
    KnowledgeFactRepository,
    compute_fact_id,
    find_supersede_candidates,
    merge_fact_decisions,
)
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeFact,
    KnowledgeGraphEntity,
    KnowledgeGraphTriple,
)
from datetime import UTC


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

    def scalars(self):
        return self

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
        id=7,
        fact_id="f1",
        kb_id="kb",
        status=current_status,
        decided_at=None,
        decided_by=None,
        subject_text="张三",
        predicate="WORKS_AT",
        object_text="公司",
        recorded_at=None,
    )
    session = FakeSession(select_rows=[fact])

    @asynccontextmanager
    async def fake_session_context():
        yield session

    monkeypatch.setattr(fact_repo_module.pg_manager, "get_async_session_context", fake_session_context)

    result, changed, superseded = await KnowledgeFactRepository().decide_fact(
        kb_id="kb", fact_id="f1", decision=decision, decided_by="user-1"
    )

    assert changed is expect_changed
    assert superseded == 0
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

    result, changed, superseded = await KnowledgeFactRepository().decide_fact(
        kb_id="kb", fact_id="missing", decision="approve", decided_by="user-1"
    )
    assert result is None
    assert changed is False
    assert superseded == 0


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
        supersedes_id=None,
        created_at=None,
    )
    payload = KnowledgeFactRepository.serialize_fact(fact)
    assert payload["fact_id"] == "f1"
    assert payload["confidence"] == 0.42
    assert payload["status"] == "pending"
    assert payload["recorded_at"] is None


# ─── P2：时间归一化（世界轴） ────────────────────────────────────────


def test_normalize_time_month_precision_start():
    fields = normalize_time_expression("2023年3月", "start")
    assert fields["valid_from"].year == 2023
    assert fields["valid_from"].month == 3
    assert fields["valid_from_precision"] == "month"
    assert fields["valid_to"] is None
    assert fields["valid_to_precision"] is None


def test_normalize_time_day_precision_point():
    fields = normalize_time_expression("2023年5月10日", "point")
    assert fields["valid_from"].day == 10
    assert fields["valid_from_precision"] == "day"


def test_normalize_time_end_year_maps_to_period_end():
    fields = normalize_time_expression("2022年", "end")
    assert fields["valid_to"].year == 2022
    assert fields["valid_to"].month == 12
    assert fields["valid_to_precision"] == "year"
    assert fields["valid_from"] is None


def test_normalize_time_range_expression():
    fields = normalize_time_expression("2020年至2023年", "point")
    assert fields["valid_from"].year == 2020
    assert fields["valid_to"].year == 2023
    assert fields["valid_from_precision"] == "year"
    assert fields["valid_to_precision"] == "year"


def test_normalize_time_unknown_end():
    fields = normalize_time_expression("已离职", "end")
    assert fields["valid_to"] is None
    assert fields["valid_to_precision"] == "unknown"


def test_normalize_time_ongoing_stays_open():
    fields = normalize_time_expression("2020年至今", "start")
    assert fields["valid_from"].year == 2020
    assert fields["valid_to"] is None


def test_normalize_time_garbage_returns_none():
    assert normalize_time_expression("没有时间信息", "point") is None
    assert normalize_time_expression("", "start") is None
    assert normalize_time_expression(None, "start") is None


def test_normalize_time_invalid_month_returns_none():
    assert normalize_time_expression("2023年13月", "start") is None


def test_fact_holds_at_world_axis_judgement():
    from datetime import datetime

    def ts(text):
        return datetime.fromisoformat(text).replace(tzinfo=UTC)

    as_of = ts("2023-06-01T00:00:00")

    ongoing = {"invalidated_at": None, "valid_from": None, "valid_to": None, "valid_to_precision": None}
    assert fact_holds_at(ongoing, as_of) is True

    ended = dict(ongoing, valid_to=ts("2022-12-31T23:59:59"), valid_to_precision="year")
    assert fact_holds_at(ended, as_of) is False

    not_yet = dict(ongoing, valid_from=ts("2023-07-01T00:00:00"))
    assert fact_holds_at(not_yet, as_of) is False

    unknown_end = dict(ongoing, valid_to=ts("2021-01-01T00:00:00"), valid_to_precision="unknown")
    assert fact_holds_at(unknown_end, as_of) is True

    superseded = dict(ongoing, invalidated_at=ts("2023-05-01T00:00:00"))
    assert fact_holds_at(superseded, as_of) is False


# ─── P2：纠正链（同 SPO 取代） ───────────────────────────────────────


def _fact_ns(id, subject, predicate, object_text):
    return SimpleNamespace(
        id=id,
        subject_text=subject,
        predicate=predicate,
        object_text=object_text,
        recorded_at=None,
        decided_at=None,
    )


def test_find_supersede_candidates_matches_same_statement():
    new_fact = _fact_ns(2, "张三", "WORKS_AT", "公司")
    candidates = find_supersede_candidates(
        new_fact,
        [_fact_ns(1, "张三 ", "WORKS_AT", "公司"), _fact_ns(3, "张三", "WORKS_AT", "其他公司")],
    )
    assert [item.id for item in candidates] == [1]


def test_find_supersede_candidates_ignores_different_predicate_or_object():
    new_fact = _fact_ns(2, "张三", "WORKS_AT", "公司")
    candidates = find_supersede_candidates(
        new_fact,
        [_fact_ns(1, "张三", "LIVES_IN", "公司"), _fact_ns(3, "李四", "WORKS_AT", "公司")],
    )
    assert candidates == []


def test_find_supersede_candidates_excludes_itself():
    new_fact = _fact_ns(2, "张三", "WORKS_AT", "公司")
    assert find_supersede_candidates(new_fact, [new_fact]) == []


# ─── P2：抽取默认关闭思考模式 ────────────────────────────────────────


def test_resolve_model_params_defaults_to_no_thinking():
    from yuxi.knowledge.graphs.extractors.llm import LLMGraphExtractor

    merged = LLMGraphExtractor._resolve_model_params(None)
    assert merged["extra_body"] == {"enable_thinking": False}

    merged = LLMGraphExtractor._resolve_model_params({})
    assert merged["extra_body"] == {"enable_thinking": False}


def test_resolve_model_params_keeps_user_extra_body():
    from yuxi.knowledge.graphs.extractors.llm import LLMGraphExtractor

    user_params = {"extra_body": {"enable_thinking": True, "temperature": 0.2}}
    merged = LLMGraphExtractor._resolve_model_params(user_params)
    assert merged["extra_body"] == {"enable_thinking": True, "temperature": 0.2}
    assert merged is not user_params
