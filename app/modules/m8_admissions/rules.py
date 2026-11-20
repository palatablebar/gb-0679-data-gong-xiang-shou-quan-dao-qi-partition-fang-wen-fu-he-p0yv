"""M8 查询准入裁决。

联合分析把多个分区合成一次聚合查询。本模块按查询分组，逐分区复算授权，
为每个查询形成唯一准入结论（整单放行 / 部分放行 / 整单拒绝）。

授权复算直接复用 M3 的授权定版与 M4 的冲突裁决，保证查询裁决、分区判定、
对账与证据导出引用同一组授权依据；拒绝优先、半开区间等规则不在此重写。
"""
import csv
import hashlib
import io
import json
from datetime import datetime, timezone

from app.config import QUERY_COLUMNS, ADMISSION_RULE_VERSION
from app.errors import DomainError
from app.reasons import reject
from app.values import instant, number, scalar
from app.modules.m1_accesses.rules import decode
from app.modules.m3_grants.rules import GRANTS, validate_grants
from app.modules.m4_conflicts.rules import decide as decide_partition

RELEASE_VERDICT = "授权内"
BLOCK_VERDICT = "越期拒绝"


def _valid_partition(part):
    return (
        len(part) == 7 and part[4] == "-"
        and part[:4].isdigit() and part[5:].isdigit()
        and 1 <= int(part[5:]) <= 12
    )


def _active_grants(grants, principal, resource, at):
    return [
        g for g in grants
        if g["principal"] == principal and g["resource"] == resource
        and instant(g["start"]) <= at < instant(g["end"])
    ]


def parse(raw, context):
    """采信查询分区行，返回 (good_rows, quarantine, intake_metrics)。"""
    text, encoding = decode(raw)
    if not text:
        raise DomainError("EMPTY_FILE", "文件为空")
    if not text.strip():
        raise DomainError("BLANK_ONLY", "全部为空行")
    stream = csv.reader(io.StringIO(text, newline=""))
    header = next(stream, None)
    if header != QUERY_COLUMNS:
        raise DomainError("BAD_HEADER", "查询表头不匹配")
    rows, bad = [], []
    seen = set()
    blank = repeated = count = 0
    try:
        for cells in stream:
            count += 1
            line = stream.line_num
            if not cells or not any(c.strip() for c in cells):
                blank += 1
                continue
            if cells == header:
                repeated += 1
                continue
            row = {"_line": line, "_sources": [line], "_raw": cells}
            if len(cells) != len(header):
                bad.append(reject(row, "BAD_COLUMNS", "列数不匹配"))
                continue
            row.update(dict(zip(header, [x.strip() for x in cells])))
            if not row["query_id"]:
                bad.append(reject(row, "EMPTY_VALUE", "查询号为空"))
                continue
            if not _valid_partition(row.get("partition", "")):
                bad.append(reject(row, "BAD_PARTITION", "分区必须为YYYY-MM"))
                continue
            try:
                at = instant(row["submitted_at"])
                volume = number(row["rows"])
                if volume < 0:
                    raise DomainError("INVALID_NUMBER", "分区行数不能为负")
            except DomainError as exc:
                bad.append(reject(row, exc.code, exc.message))
                continue
            dedupe_key = (row["query_id"], row["partition"])
            if dedupe_key in seen:
                bad.append(reject(row, "DUPLICATE_CONFLICT", "同一查询分区重复"))
                continue
            seen.add(dedupe_key)
            row["at"] = at.isoformat()
            row["rows"] = str(volume)
            rows.append(row)
    except csv.Error:
        raise DomainError("BAD_CSV", "引号未闭合或文件损坏")
    if count == 0:
        raise DomainError("HEADER_ONLY", "仅有表头")
    if not rows and not bad:
        raise DomainError("BLANK_ONLY", "全部为空行")
    metrics = {
        "input": count, "accepted": len(rows), "quarantine": len(bad),
        "blank": blank, "repeated_headers": repeated, "encoding": encoding,
    }
    return rows, bad, metrics


def evidence_basis(partitions):
    """分区依据的规范摘要：裁决、对账、证据导出共用同一函数。"""
    encoded = json.dumps(
        partitions, ensure_ascii=False, sort_keys=True, default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _query_key(query_id, basis):
    return hashlib.sha256(
        query_id.encode() + b"|" + basis.encode() + b"|" + ADMISSION_RULE_VERSION.encode()
    ).hexdigest()[:20]


def evaluate(raw, context=None):
    context = context or {}
    grants = validate_grants(context.get("parameters", {}).get("grants", GRANTS))
    rows, quarantine, intake = parse(raw, context)
    order = []
    grouped = {}
    for row in rows:
        at = instant(row["at"])
        candidates = _active_grants(grants, row["principal"], row["resource"], at)
        verdict = decide_partition(candidates, row["partition"])
        admitted = verdict["effect"] == "allow"
        partition_row = scalar({
            **row,
            "candidate_count": len(candidates),
            **verdict,
            "rule_evidence": " / ".join(verdict["matched_rules"]) or "NO_GRANT",
            "verdict": RELEASE_VERDICT if admitted else BLOCK_VERDICT,
            "released": admitted,
            "block_reason": (
                "" if admitted
                else "NO_GRANT" if not verdict["matched_rules"] else "EXPLICIT_DENY"
            ),
        })
        grouped.setdefault(row["query_id"], []).append(partition_row)
        if row["query_id"] not in order:
            order.append(row["query_id"])

    queries = []
    total_released = total_blocked = total_volume = released_volume = 0
    for query_id in order:
        partitions = grouped[query_id]
        released = [p for p in partitions if p["released"]]
        blocked = [p for p in partitions if not p["released"]]
        if blocked and released:
            conclusion = "部分放行"
        elif released:
            conclusion = "整单放行"
        else:
            conclusion = "整单拒绝"
        # 到期/未授权分区不得混入放行结果：两个集合按构造互斥，再显式复算一次。
        taint = len({p["partition"] for p in released} & {p["partition"] for p in blocked})
        basis = evidence_basis(partitions)
        volume = sum(number(p["rows"]) for p in partitions)
        out_volume = sum(number(p["rows"]) for p in released)
        balanced = len(partitions) == len(released) + len(blocked)
        # 只有存在可独立证明授权范围的分区，且证明放行结果未混入到期数据，才允许封存。
        sealable = bool(released) and taint == 0 and balanced
        queries.append({
            "query_id": query_id,
            "query_key": _query_key(query_id, basis),
            "conclusion": conclusion,
            "partitions": partitions,
            "released_partitions": [p["partition"] for p in released],
            "blocked_partitions": [p["partition"] for p in blocked],
            "partition_total": len(partitions),
            "released_count": len(released),
            "blocked_count": len(blocked),
            "volume_total": str(volume),
            "volume_released": str(out_volume),
            "taint": taint,
            "balanced": balanced,
            "sealable": sealable,
            "basis_sha256": basis,
            "source_lines": sorted(line for p in partitions for line in p["_sources"]),
        })
        total_released += len(released)
        total_blocked += len(blocked)
        total_volume += volume
        released_volume += out_volume

    balanced_overall = (
        intake["input"] == len(rows) + len(quarantine) + intake["blank"] + intake["repeated_headers"]
        and all(q["balanced"] for q in queries)
    )
    metrics = {
        **intake,
        "queries": len(queries),
        "accepted_partitions": len(rows),
        "quarantine_partitions": len(quarantine),
        "released_partitions": total_released,
        "blocked_partitions": total_blocked,
        "full_release": sum(q["conclusion"] == "整单放行" for q in queries),
        "partial_release": sum(q["conclusion"] == "部分放行" for q in queries),
        "full_reject": sum(q["conclusion"] == "整单拒绝" for q in queries),
        "volume_total": str(total_volume),
        "volume_released": str(released_volume),
        "taint_total": sum(q["taint"] for q in queries),
        "balanced": balanced_overall,
        # 未证明放行结果未混入到期数据前，整批不可封存。
        "sealable": balanced_overall and total_released > 0
        and sum(q["taint"] for q in queries) == 0,
        "rule_version": ADMISSION_RULE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return {"queries": queries, "quarantine": quarantine, "metrics": metrics}
