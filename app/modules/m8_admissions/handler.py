from flask import Blueprint, jsonify, request

from app import admission_pipeline, admission_store
from app.errors import DomainError
from app.modules.m8_admissions.evidence import build_query_evidence

bp = Blueprint("admissions", __name__)


def _payload():
    payload = request.get_json(silent=True) if request.method == "POST" else {}
    if payload is None or not isinstance(payload, dict):
        raise DomainError("BAD_BODY", "请求需要对象")
    return payload


def _raw_and_params(payload):
    params = payload.get("parameters", {})
    if not isinstance(params, dict):
        raise DomainError("BAD_PARAMETERS", "参数需要对象")
    if "csv" in payload:
        raw_text = payload["csv"]
        if not isinstance(raw_text, str):
            raise DomainError("BAD_CSV", "CSV需要文本")
        return raw_text.encode(), params
    return admission_pipeline.load_sample(payload.get("sample", "admissions.csv")), params


@bp.route("/api/admissions", methods=["GET", "POST"])
def admissions():
    """GET 用仓内样例即时演算（不落账）；POST 走完整流水线并幂等落账。"""
    if request.method == "GET":
        raw = admission_pipeline.load_sample("admissions.csv")
        return jsonify(admission_pipeline.calculate(raw))
    payload = _payload()
    raw, params = _raw_and_params(payload)
    return jsonify(admission_pipeline.execute(raw, params))


@bp.route("/api/admissions/upload", methods=["POST"])
def upload():
    file = request.files.get("file")
    if not file:
        raise DomainError("NO_FILE", "请选择查询CSV文件")
    return jsonify(admission_pipeline.execute(file.read()))


@bp.route("/api/admissions/runs")
def runs():
    return jsonify({"runs": admission_store.history()})


@bp.route("/api/admissions/runs/<run_id>")
def run_detail(run_id):
    return jsonify(admission_store.get_run(run_id))


@bp.route("/api/admissions/runs/<run_id>/review", methods=["POST"])
def review(run_id):
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload.get("version"), int):
        raise DomainError("BAD_VERSION", "请提供版本号")
    return jsonify(
        admission_store.review_run(run_id, payload["version"], payload.get("decision"))
    )


@bp.route("/api/admissions/runs/<run_id>/journal")
def run_journal(run_id):
    admission_store.get_run(run_id)  # 不存在则 404
    return jsonify({"journal": admission_store.journal(run_id)})


@bp.route("/api/admissions/runs/<run_id>/evidence")
def evidence(run_id):
    run = admission_store.get_run(run_id)
    from flask import current_app
    response = current_app.response_class(
        build_query_evidence(run), mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="admission_evidence.csv"'},
    )
    return response


@bp.route("/api/admissions/queries/<query_key>")
def query_detail(query_key):
    return jsonify(admission_store.get_query(query_key))
