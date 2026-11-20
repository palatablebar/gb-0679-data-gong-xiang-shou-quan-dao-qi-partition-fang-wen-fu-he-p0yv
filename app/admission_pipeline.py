"""查询准入流水线：裁决 -> 落账（幂等重试）。"""
import hashlib
import json
import time

from app.config import ROOT, ADMISSION_RULE_VERSION
from app.errors import DomainError
from app import admission_store
from app.modules.m8_admissions.rules import evaluate


def load_sample(name="admissions.csv"):
    if name != "admissions.csv":
        raise DomainError("BAD_SAMPLE", "样例不存在", 404)
    return (ROOT / "sample_data" / name).read_bytes()


def _run_digest(raw, params):
    return hashlib.sha256(
        raw + json.dumps(params or {}, sort_keys=True, ensure_ascii=False).encode()
        + ADMISSION_RULE_VERSION.encode()
    ).hexdigest()[:20]


def calculate(raw, params=None):
    params = params or {}
    started = time.monotonic()
    result = evaluate(raw, {"parameters": params, "raw": raw})
    result["run_id"] = _run_digest(raw, params)
    result["input_sha256"] = hashlib.sha256(raw).hexdigest()
    result["rule_version"] = ADMISSION_RULE_VERSION
    result["parameters"] = params
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
    return result


def execute(raw, params=None):
    result = calculate(raw, params)
    # 同一输入+参数+规则版本的重试不新增结论，只追加一条重试日志。
    return admission_store.ingest(result)
