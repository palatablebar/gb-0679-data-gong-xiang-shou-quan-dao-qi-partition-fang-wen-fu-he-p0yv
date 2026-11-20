"""查询准入证据导出：裁决、对账、导出共用同一份分区依据。"""
import csv
import io
import json


def build_query_evidence(run):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow([
        "run_id", "input_sha256", "rule_version", "query_id", "conclusion",
        "partition", "verdict", "rule_evidence", "deny_rules", "block_reason",
        "rows", "basis_sha256", "source_lines", "evidence",
    ])
    for query in run["queries"]:
        for part in query["partitions"]:
            writer.writerow([
                run["run_id"], run["input_sha256"], run["rule_version"],
                query["query_id"], query["conclusion"], part["partition"],
                part["verdict"], part.get("rule_evidence", ""),
                json.dumps(part.get("deny_rules", []), ensure_ascii=False),
                part.get("block_reason", ""), part["rows"],
                query["basis_sha256"], json.dumps(part["_sources"]),
                json.dumps(part, ensure_ascii=False),
            ])
    for row in run["quarantine"]:
        writer.writerow([
            run["run_id"], run["input_sha256"], run["rule_version"],
            row.get("query_id", ""), "隔离", row.get("partition", ""),
            "QUARANTINE", "", "", row.get("reason", ""),
            row.get("rows", ""), "", json.dumps(row.get("_sources", [])),
            json.dumps(row, ensure_ascii=False),
        ])
    return "\ufeff" + stream.getvalue()
