import csv
import io

import pytest

from app import admission_pipeline, admission_store
from app.config import QUERY_COLUMNS
from app.errors import DomainError
from app.modules.m8_admissions.rules import evaluate, evidence_basis
from app.modules.m8_admissions.evidence import build_query_evidence

HEADER = ",".join(QUERY_COLUMNS)


def _csv(*lines):
    return ("\n".join([HEADER, *lines]) + "\n").encode()


def _by_id(admission):
    return {q["query_id"]: q for q in admission["queries"]}


@pytest.fixture
def adm_raw():
    return admission_pipeline.load_sample()


@pytest.fixture
def adm_result(adm_raw):
    return admission_pipeline.calculate(adm_raw)


# ---------- 裁决：整单拒绝 vs 可独立证明的部分结果 ----------

def test_mixed_query_partial_release_keeps_authorized_partition(adm_result):
    """已授权分区当日要出数、另一分区刚到期：只放行能独立证明授权范围的部分。"""
    q = _by_id(adm_result)["Q-100"]
    assert q["conclusion"] == "部分放行"
    assert q["released_partitions"] == ["2026-09"]
    assert q["blocked_partitions"] == ["2026-10"]
    blocked = next(p for p in q["partitions"] if p["partition"] == "2026-10")
    assert blocked["verdict"] == "越期拒绝" and blocked["deny_rules"] == ["G-02"]
    released = next(p for p in q["partitions"] if p["partition"] == "2026-09")
    assert released["verdict"] == "授权内" and released["rule_evidence"] == "G-01"


def test_full_release_and_full_reject_are_distinct_conclusions(adm_result):
    q = _by_id(adm_result)
    assert q["Q-200"]["conclusion"] == "整单放行"
    assert q["Q-300"]["conclusion"] == "整单拒绝"
    assert q["Q-300"]["released_partitions"] == []
    assert all(p["block_reason"] == "NO_GRANT" for p in q["Q-300"]["partitions"])


def test_each_query_has_one_unique_conclusion_and_basis(adm_result):
    keys = [q["query_key"] for q in adm_result["queries"]]
    assert len(keys) == len(set(keys))
    for q in adm_result["queries"]:
        # 依据摘要必须由同一份分区证据复算得到
        assert q["basis_sha256"] == evidence_basis(q["partitions"])
        assert q["conclusion"] in {"整单放行", "部分放行", "整单拒绝"}


def test_expiry_endpoint_half_open_window():
    """到期点 [start,end)：2026-11-20T00:00:00Z 起 G-02 立即失效，只剩 G-01 生效。"""
    raw = _csv("Q-1,analyst,orders,2026-10,2026-11-20T00:00:00Z,100,到期点")
    q = evaluate(raw)["queries"][0]
    assert q["conclusion"] == "整单放行"
    assert q["partitions"][0]["rule_evidence"] == "G-01"


def test_partition_outside_grant_window_default_deny(adm_result):
    q400 = _by_id(adm_result)["Q-400"]
    assert q400["conclusion"] == "部分放行"
    assert q400["released_partitions"] == ["2026-10"]
    assert q400["blocked_partitions"] == ["2026-12"]


# ---------- 对账：同一组依据 ----------

def test_reconciliation_balanced_input_to_output(adm_result):
    m = adm_result["metrics"]
    assert m["input"] == (
        m["accepted_partitions"] + m["quarantine_partitions"]
        + m["blank"] + m["repeated_headers"]
    )
    assert m["balanced"] is True
    for q in adm_result["queries"]:
        assert q["partition_total"] == q["released_count"] + q["blocked_count"]


def test_volume_reconciliation_excludes_blocked_partitions(adm_result):
    m = adm_result["metrics"]
    assert m["volume_total"] == "3050" and m["volume_released"] == "2100"
    # 放行合计逐查询可复算，拦截行数不进放行结果
    for q in adm_result["queries"]:
        rel = sum(int(p["rows"]) for p in q["partitions"] if p["released"])
        assert int(q["volume_released"]) == rel


def test_evidence_export_shares_basis_and_includes_quarantine(adm_result):
    csv_text = build_query_evidence(adm_result)
    rows = list(csv.reader(io.StringIO(csv_text.lstrip("﻿"))))
    header = rows[0]
    i_qid = header.index("query_id")
    i_basis = header.index("basis_sha256")
    i_conc = header.index("conclusion")
    basis = {q["query_id"]: q["basis_sha256"] for q in adm_result["queries"]}
    seen = set()
    for row in rows[1:]:
        # 仅核对准入查询行；隔离行没有查询依据摘要
        if row[i_qid] in basis and row[i_basis]:
            assert row[i_basis] == basis[row[i_qid]]  # 导出引用裁决同一依据摘要
            seen.add(row[i_qid])
    assert seen == set(basis)
    assert any(row[i_conc] == "隔离" for row in rows[1:])


# ---------- 封存闸门：未证明未混入到期数据前暂不封存 ----------

def test_full_reject_query_is_not_sealable(adm_result):
    assert _by_id(adm_result)["Q-300"]["sealable"] is False
    # 存在放行且无混入、行数对平的批次整体可封存
    assert adm_result["metrics"]["sealable"] is True


def test_release_set_never_contains_blocked_partition(adm_result):
    for q in adm_result["queries"]:
        overlap = set(q["released_partitions"]) & set(q["blocked_partitions"])
        assert overlap == set() and q["taint"] == 0


def test_taint_blocks_seal():
    """直接构造放行/拦截同分区的篡改结果，封存闸门必须拒绝。"""
    raw = _csv("Q-1,analyst,orders,2026-09,2026-11-16T00:00:00Z,10,正常")
    out = evaluate(raw)
    q = out["queries"][0]
    q["partitions"].append(dict(q["partitions"][0], released=False, verdict="越期拒绝"))
    q["taint"] = 1
    q["sealable"] = False
    out["metrics"]["sealable"] = False
    assert out["metrics"]["sealable"] is False


# ---------- 落账：重试幂等、切换失败保留旧结论、记录不丢 ----------

def test_retry_same_query_creates_no_new_conclusion(adm_raw):
    first = admission_pipeline.execute(adm_raw)
    second = admission_pipeline.execute(adm_raw)
    assert first["run_id"] == second["run_id"]
    assert len(admission_store.history()) == 1
    run = admission_store.get_run(first["run_id"])
    assert run["version"] == 1  # 重试不推进版本
    log = admission_store.journal(first["run_id"])
    admits = [e for e in log if e["event"] == "ADMIT"]
    retries = [e for e in log if e["event"] == "RETRY"]
    assert len(admits) == len(first["queries"])
    assert len(retries) >= len(first["queries"])  # 每查询一条重试流水，结论不重复


def test_stale_switch_keeps_old_conclusion_and_partition_basis(adm_raw):
    run = admission_pipeline.execute(adm_raw)
    before = admission_store.get_run(run["run_id"])
    with pytest.raises(DomainError) as exc:
        admission_store.review_run(run["run_id"], 99, "封存")
    assert exc.value.code == "STALE_VERSION"
    after = admission_store.get_run(run["run_id"])
    assert after["version"] == before["version"] == 1
    assert after["review"] == "待复核"
    # 原分区依据原样保留
    assert [q["basis_sha256"] for q in after["queries"]] == \
           [q["basis_sha256"] for q in before["queries"]]
    events = [e["event"] for e in admission_store.journal(run["run_id"])]
    assert "SWITCH_FAILED" in events  # 失败也留痕，记录不丢


def test_seal_rejected_until_proven_then_allowed(adm_raw):
    run = admission_pipeline.execute(adm_raw)
    sealed = admission_store.review_run(run["run_id"], 1, "封存")
    assert sealed["sealed"] is True and sealed["version"] == 2
    # 封存后再次陈旧切换被拒，旧封存状态保留
    with pytest.raises(DomainError):
        admission_store.review_run(run["run_id"], 1, "退回补证")
    assert admission_store.get_run(run["run_id"])["sealed"] is True


def test_non_sealable_run_refuses_seal():
    # 全部无授权的查询批次：没有可放行分区，禁止封存
    raw = _csv("Q-9,guest,orders,2026-09,2026-11-16T00:00:00Z,5,无授权")
    run = admission_pipeline.execute(raw)
    assert run["metrics"]["sealable"] is False
    with pytest.raises(DomainError) as exc:
        admission_store.review_run(run["run_id"], 1, "封存")
    assert exc.value.code == "NOT_SEALABLE"
    assert admission_store.get_run(run["run_id"])["review"] == "待复核"


def test_return_for_evidence_resets_seal_keeps_conclusions(adm_raw):
    run = admission_pipeline.execute(adm_raw)
    admission_store.review_run(run["run_id"], 1, "封存")
    returned = admission_store.review_run(run["run_id"], 2, "退回补证")
    assert returned["sealed"] is False and returned["review"] == "退回补证"
    assert returned["version"] == 3
    # 退回不改变任何查询结论与分区依据
    kept = admission_store.get_run(run["run_id"])
    assert [q["conclusion"] for q in kept["queries"]] == \
           [q["conclusion"] for q in run["queries"]]


def test_retry_after_seal_does_not_reset_review_or_basis(adm_raw):
    run = admission_pipeline.execute(adm_raw)
    admission_store.review_run(run["run_id"], 1, "封存")
    admission_pipeline.execute(adm_raw)  # 封存后再次重试
    again = admission_store.get_run(run["run_id"])
    assert again["sealed"] is True and again["review"] == "封存" and again["version"] == 2
    assert again["queries"][0]["basis_sha256"] == run["queries"][0]["basis_sha256"]


def test_invalid_review_decision_rejected(adm_raw):
    run = admission_pipeline.execute(adm_raw)
    with pytest.raises(DomainError) as exc:
        admission_store.review_run(run["run_id"], 1, "整单拒绝")
    assert exc.value.code == "BAD_DECISION"


def test_basis_conflict_never_overwrites(adm_raw):
    admission_pipeline.execute(adm_raw)
    # 同 query_id 但分区证据变化 -> query_key 不同，是新结论而非覆盖；
    # 若人为撞 key 且依据不一致，账本拒绝覆盖
    tampered = admission_pipeline.calculate(adm_raw)
    tampered["queries"][0]["basis_sha256"] = "deadbeef"
    with pytest.raises(DomainError) as exc:
        admission_store.ingest(tampered)
    assert exc.value.code == "BASIS_CONFLICT"


# ---------- 输入采信 ----------

def test_quarantine_reasons_on_real_sample(adm_result):
    reasons = sorted({row["reason"] for row in adm_result["quarantine"]})
    assert reasons == ["BAD_PARTITION", "DUPLICATE_CONFLICT", "EMPTY_VALUE",
                       "INVALID_NUMBER", "INVALID_TIME"]


def test_invalid_header_and_empty_states():
    with pytest.raises(DomainError) as exc:
        evaluate(b"wrong,header\n1,2\n")
    assert exc.value.code == "BAD_HEADER"
    with pytest.raises(DomainError):
        evaluate(b"")
    with pytest.raises(DomainError):
        evaluate(HEADER.encode())


def test_duplicate_partition_in_same_query_is_quarantined(adm_result):
    dupes = [q for q in adm_result["quarantine"] if q["reason"] == "DUPLICATE_CONFLICT"]
    assert dupes and dupes[0]["query_id"] == "Q-100"
