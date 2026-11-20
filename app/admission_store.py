"""查询准入的追加式账本。

结论（admission_queries）与操作流水（admission_journal）均只增不改：
- 同一查询重试不新增结论，仅追加 RETRY 流水；
- 复核切换失败（陈旧版本/不可封存）不触碰旧结论，旧分区依据原样保留；
- 任何写入都在同一事务内完成，失败整体回滚，不丢记录。
"""
import json
import sqlite3

from app.config import DATA_DIR, REVIEW_DECISIONS
from app.errors import DomainError


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA_DIR / "records.db", timeout=20)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        "CREATE TABLE IF NOT EXISTS admission_runs ("
        "run_id TEXT PRIMARY KEY,payload TEXT NOT NULL,"
        "version INTEGER NOT NULL DEFAULT 1,review TEXT NOT NULL DEFAULT '待复核',"
        "sealed INTEGER NOT NULL DEFAULT 0)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS admission_queries ("
        "query_key TEXT PRIMARY KEY,run_id TEXT NOT NULL,query_id TEXT NOT NULL,"
        "conclusion TEXT NOT NULL,basis_sha256 TEXT NOT NULL,"
        "payload TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,"
        "review TEXT NOT NULL DEFAULT '待复核',sealed INTEGER NOT NULL DEFAULT 0)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS admission_journal ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,query_key TEXT,run_id TEXT,"
        "event TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL)"
    )
    return db


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def ingest(result):
    """幂等落账：新查询写结论，已存在的查询只记重试，绝不覆盖原依据。"""
    run_id = result["run_id"]
    with connect() as db:
        existing_run = db.execute(
            "SELECT 1 FROM admission_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if not existing_run:
            db.execute(
                "INSERT INTO admission_runs(run_id,payload) VALUES (?,?)",
                (run_id, json.dumps(result, ensure_ascii=False)),
            )
        for query in result["queries"]:
            hit = db.execute(
                "SELECT conclusion,basis_sha256 FROM admission_queries WHERE query_key=?",
                (query["query_key"],),
            ).fetchone()
            if hit is None:
                db.execute(
                    "INSERT INTO admission_queries"
                    "(query_key,run_id,query_id,conclusion,basis_sha256,payload)"
                    " VALUES (?,?,?,?,?,?)",
                    (query["query_key"], run_id, query["query_id"], query["conclusion"],
                     query["basis_sha256"], json.dumps(query, ensure_ascii=False)),
                )
                db.execute(
                    "INSERT INTO admission_journal(query_key,run_id,event,detail,created_at)"
                    " VALUES (?,?,?,?,?)",
                    (query["query_key"], run_id, "ADMIT",
                     f"形成唯一准入结论：{query['conclusion']}", _now()),
                )
            else:
                # 同一查询重试：结论与依据保持不变，只追加流水。
                if hit[0] != query["conclusion"] or hit[1] != query["basis_sha256"]:
                    raise DomainError(
                        "BASIS_CONFLICT", "查询依据与既有结论不一致，拒绝覆盖", 409
                    )
                db.execute(
                    "INSERT INTO admission_journal(query_key,run_id,event,detail,created_at)"
                    " VALUES (?,?,?,?,?)",
                    (query["query_key"], run_id, "RETRY",
                     "同一查询重试，复用既有结论与原分区依据", _now()),
                )
        if existing_run:
            db.execute(
                "INSERT INTO admission_journal(query_key,run_id,event,detail,created_at)"
                " VALUES (NULL,?,?,?,?)",
                (run_id, "RETRY", "同一批次重试，未新增任何查询结论", _now()),
            )
        else:
            db.execute(
                "INSERT INTO admission_journal(query_key,run_id,event,detail,created_at)"
                " VALUES (NULL,?,?,?,?)",
                (run_id, "INGEST", f"批次落账，含{len(result['queries'])}个查询", _now()),
            )
    return get_run(run_id)


def get_run(run_id):
    with connect() as db:
        row = db.execute(
            "SELECT payload,version,review,sealed FROM admission_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
    if not row:
        raise DomainError("NOT_FOUND", "准入批次不存在", 404)
    payload = json.loads(row[0])
    payload["version"] = row[1]
    payload["review"] = row[2]
    payload["sealed"] = bool(row[3])
    return payload


def latest_run():
    with connect() as db:
        row = db.execute(
            "SELECT run_id FROM admission_runs ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    return get_run(row[0]) if row else None


def history():
    with connect() as db:
        return [
            {"run_id": r, "version": v, "review": s, "sealed": bool(z)}
            for r, v, s, z in db.execute(
                "SELECT run_id,version,review,sealed FROM admission_runs ORDER BY rowid DESC"
            )
        ]


def get_query(query_key):
    with connect() as db:
        row = db.execute(
            "SELECT payload,version,review,sealed FROM admission_queries WHERE query_key=?",
            (query_key,),
        ).fetchone()
    if not row:
        raise DomainError("NOT_FOUND", "查询结论不存在", 404)
    payload = json.loads(row[0])
    payload["version"] = row[1]
    payload["review"] = row[2]
    payload["sealed"] = bool(row[3])
    return payload


def review_run(run_id, version, decision):
    """条件更新版本；切换失败保留旧结论和原分区依据，失败留痕独立提交不丢失。"""
    if decision not in REVIEW_DECISIONS:
        raise DomainError("BAD_DECISION", "复核动作无效，仅支持 封存/退回补证")
    db = connect()
    try:
        current = db.execute(
            "SELECT version,sealed,payload FROM admission_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if not current:
            raise DomainError("NOT_FOUND", "准入批次不存在", 404)
        if current[0] != version:
            # 先在同一连接提交留痕，再抛错：结论不动，失败记录也不回滚丢失。
            db.execute(
                "INSERT INTO admission_journal(query_key,run_id,event,detail,created_at)"
                " VALUES (NULL,?,?,?,?)",
                (run_id, "SWITCH_FAILED",
                 f"复核版本{version}陈旧，保留旧结论（当前版本{current[0]}）", _now()),
            )
            db.commit()
            raise DomainError("STALE_VERSION", "版本已更新，请刷新", 409)
        if decision == "封存":
            payload = json.loads(current[2])
            if not payload["metrics"]["sealable"]:
                db.execute(
                    "INSERT INTO admission_journal(query_key,run_id,event,detail,created_at)"
                    " VALUES (NULL,?,?,?,?)",
                    (run_id, "SEAL_BLOCKED",
                     "放行结果未证明未混入到期数据或行数未对平，拒绝封存", _now()),
                )
                db.commit()
                raise DomainError("NOT_SEALABLE", "未证明未混入到期数据，暂不封存", 422)
        sealed = 1 if decision == "封存" else 0
        cursor = db.execute(
            "UPDATE admission_runs SET version=version+1,review=?,sealed=?"
            " WHERE run_id=? AND version=?",
            (decision, sealed, run_id, version),
        )
        if cursor.rowcount != 1:
            raise DomainError("STALE_VERSION", "版本已更新，请刷新", 409)
        db.execute(
            "UPDATE admission_queries SET version=version+1,review=?,sealed=? WHERE run_id=?",
            (decision, sealed, run_id),
        )
        db.execute(
            "INSERT INTO admission_journal(query_key,run_id,event,detail,created_at)"
            " VALUES (NULL,?,?,?,?)",
            (run_id, "REVIEW", f"复核切换为{decision}", _now()),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return get_run(run_id)


def journal(run_id=None):
    """追加式操作流水。默认返回全部；传入 run_id 只看该批次及其查询的流水。"""
    with connect() as db:
        if run_id is None:
            rows = db.execute(
                "SELECT id,query_key,run_id,event,detail,created_at "
                "FROM admission_journal ORDER BY id"
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id,query_key,run_id,event,detail,created_at FROM admission_journal "
                "WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
    return [
        {"seq": seq, "query_key": qk, "run_id": rid, "event": ev, "detail": d, "at": at}
        for seq, qk, rid, ev, d, at in rows
    ]
