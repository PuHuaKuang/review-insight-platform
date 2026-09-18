#!/usr/bin/env python3
"""告警规则引擎（M2）。

规则按方案定义，全部带样本量下限，且不使用单日指标：
  R1 整体差评率劣化   7 个完整日滚动，> 基线+2σ，样本 ≥ 500
  R2 新版本早期劣化   发布 7 日内差评率 > 上一版本同期 +2pp，样本 ≥ 100
  R3 单一设备风险     30 日差评率 > 15%，样本 ≥ 50，独立作者 ≥ 20
  R4 单一市场风险     30 日差评率 > 15%，样本 ≥ 100
  R5 崩溃类词突增     7 日 vs 前 7 日，环比 +100% 且 ≥ 5 条
  R6 同步异常        最近一次同步失败，或近期单日 0 条
  R7 数据缺口        存在 > 2 天无数据

支持历史回放：--as-of YYYY-MM-DD 用当日视角评估，用于验证规则有效性。

查询统一走 SQLAlchemy engine（models.engine），不再直连 sqlite3 模块——
理由与 analytics.py 相同：切 Postgres 时连接层不用改，SQL 用 text() +
命名参数（:name）而非位置参数（?），跨数据库方言可移植。
sqlite3.Row 的字典式访问（row["field"]）改用 SQLAlchemy 的 .mappings() 结果集。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text

from models import AlertEvent, AlertRule, Base, engine

WORKDIR = Path(__file__).resolve().parent.parent

CRASH_WORDS = [
    "crash", "crashes", "crashing", "force close", "fc ",
    "崩溃", "闪退", "停止运行", "强行关闭",
    "se cierra", "se cuelga", "planta", "cierra solo",
    "bugue", "travando", "fecha sozinho",
    " abstürz", "stürzt ab", "s'arrête", "plante",
    "вылетает", "вылет", "падает",
    "落ちる", "強制終了", "꺼짐", "çöküyor", "crasha",
]

DEFAULT_RULES = {
    "R1": {"min_sample": 500, "sigma": 2.0, "baseline_days": 90},
    "R2": {"min_sample": 100, "delta": 0.02, "window_days": 7},
    "R3": {"min_sample": 50, "min_authors": 20, "rate": 0.15, "window_days": 30},
    "R4": {"min_sample": 100, "rate": 0.15, "window_days": 30},
    "R5": {"min_abs": 5, "growth": 1.0, "window_days": 7},
    "R6": {"window_days": 3},
    "R7": {"max_gap_days": 2},
}


def _ensure_tables() -> None:
    Base.metadata.create_all(engine, tables=[AlertRule.__table__, AlertEvent.__table__])


def _rules(c) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    for code, p in DEFAULT_RULES.items():
        exists = c.execute(
            text("select 1 from alert_rule where code=:code"), {"code": code}
        ).fetchone()
        if not exists:
            c.execute(
                text(
                    "insert into alert_rule(code,name,enabled,params,updated_at) "
                    "values(:code,'',1,:params,:now)"
                ),
                {"code": code, "params": json.dumps(p), "now": now},
            )
    out = {}
    for r in c.execute(text("select code, enabled, params from alert_rule")).mappings():
        try:
            out[r["code"]] = {**json.loads(r["params"] or "{}"), "enabled": bool(r["enabled"])}
        except json.JSONDecodeError:
            out[r["code"]] = {"enabled": bool(r["enabled"])}
    return out


def _complete_days(c, as_of: str, lookback: int = 120) -> list[tuple[str, int, int]]:
    """返回截至 as_of 的每日 (日期, 评论量, 差评数)，并剔除仍在写入的尾部。"""
    rows = c.execute(
        text(
            """select substr(submitted_at,1,10) d, count(*) n,
                      sum(case when star_rating<=2 then 1 else 0 end) neg
               from reviews where substr(submitted_at,1,10) <= :as_of
               group by 1 order by 1 desc limit :lb"""
        ),
        {"as_of": as_of, "lb": lookback},
    ).fetchall()
    rows = [(r[0], r[1], r[2]) for r in rows]
    if len(rows) < 5:
        return list(reversed(rows))
    counts = sorted(n for _, n, _ in rows)
    median = counts[len(counts) // 2]
    # 剔除尾部：连续低于中位数 50% 的近期日期视为"报告仍在写入"
    drop = 0
    for _, n, _ in rows:
        if median and n < median * 0.5:
            drop += 1
        else:
            break
    return list(reversed(rows[drop:]))


def _rate(n: int, neg: int) -> float:
    return (neg / n) if n else 0.0


def rule_r1(c, p, as_of: str) -> list[dict]:
    days = _complete_days(c, as_of)
    if len(days) < 7:
        return []
    win = days[-7:]
    n = sum(d[1] for d in win)
    neg = sum(d[2] for d in win)
    if n < p.get("min_sample", 500):
        return []
    base_days = days[-p.get("baseline_days", 90):]
    bn = sum(d[1] for d in base_days)
    bneg = sum(d[2] for d in base_days)
    baseline = _rate(bn, bneg)
    rate = _rate(n, neg)
    sigma = (baseline * (1 - baseline) / n) ** 0.5 if n else 0
    thr = baseline + p.get("sigma", 2.0) * sigma
    if rate > thr:
        return [{
            "code": "R1", "level": "P1", "stat_date": as_of, "subject": "整体",
            "metric": round(rate, 4), "threshold": round(thr, 4), "sample": n,
            "evidence": f"近 7 个完整日差评率 {rate:.2%}（{neg}/{n}），基线 {baseline:.2%}，阈值 {thr:.2%}",
        }]
    return []


def rule_r2(c, p, as_of: str) -> list[dict]:
    """新版本发布 7 日内 vs 上一版本同期。"""
    rel = c.execute(
        text("select version, released_at from release order by released_at desc limit 6")
    ).mappings().fetchall()
    out = []
    for i in range(len(rel) - 1):
        cur, prev = rel[i], rel[i + 1]
        cur_v, cur_d = cur["version"], cur["released_at"]
        prev_v, prev_d = prev["version"], prev["released_at"]
        if not cur_d or not prev_d or cur_d > as_of:
            continue
        if (date.fromisoformat(as_of) - date.fromisoformat(cur_d)).days > 14:
            continue

        def stat(v, d0):
            d1 = (date.fromisoformat(d0) + timedelta(days=p.get("window_days", 7))).isoformat()
            r = c.execute(
                text(
                    """select count(*) n, sum(case when star_rating<=2 then 1 else 0 end) neg
                       from reviews where app_version_name=:v and substr(submitted_at,1,10)>=:d0
                       and substr(submitted_at,1,10)<:d1"""
                ),
                {"v": v, "d0": d0, "d1": d1},
            ).fetchone()
            return (r[0] or 0, r[1] or 0)

        cn, cneg = stat(cur_v, cur_d)
        pn, pneg = stat(prev_v, prev_d)
        if cn < p.get("min_sample", 100) or pn < p.get("min_sample", 100):
            continue
        cr, pr = _rate(cn, cneg), _rate(pn, pneg)
        delta = p.get("delta", 0.02)
        if cr - pr > delta:
            out.append({
                "code": "R2", "level": "P0", "stat_date": as_of, "subject": cur_v,
                "metric": round(cr, 4), "threshold": round(pr + delta, 4), "sample": cn,
                "evidence": f"{cur_v[:20]} 发布 {p.get('window_days',7)} 日内差评率 {cr:.2%}（{cneg}/{cn}），"
                            f"上一版本 {prev_v[:20]} 同期 {pr:.2%}，超出 {cr-pr:.2%}",
            })
    return out


def _dim_rule(c, p, as_of: str, code: str, col: str, label: str) -> list[dict]:
    since = (date.fromisoformat(as_of) - timedelta(days=p.get("window_days", 30))).isoformat()
    rows = c.execute(
        text(
            f"""select {col} v, count(*) n, sum(case when star_rating<=2 then 1 else 0 end) neg,
                       count(distinct author_name) au
                from reviews where coalesce({col},'')<>'' and substr(submitted_at,1,10)>=:since
                group by 1"""
        ),
        {"since": since},
    ).fetchall()
    out = []
    for v, n, neg, au in rows:
        if n < p.get("min_sample", 50):
            continue
        if au < p.get("min_authors", 1):
            continue
        r = _rate(n, neg)
        if r > p.get("rate", 0.15):
            out.append({
                "code": code, "level": "P1", "stat_date": as_of, "subject": f"{label}:{v}",
                "metric": round(r, 4), "threshold": p.get("rate", 0.15), "sample": n,
                "evidence": f"近 {p.get('window_days',30)} 日 {v} 差评率 {r:.2%}（{neg}/{n}），独立作者 {au}",
            })
    return sorted(out, key=lambda x: -x["metric"])[:5]


def rule_r3(c, p, as_of):
    return _dim_rule(c, p, as_of, "R3", "device", "设备")


def rule_r4(c, p, as_of):
    return _dim_rule(c, p, as_of, "R4", "reviewer_lang", "市场")


def rule_r5(c, p, as_of: str) -> list[dict]:
    w = p.get("window_days", 7)
    d0 = (date.fromisoformat(as_of) - timedelta(days=w)).isoformat()
    dprev = (date.fromisoformat(as_of) - timedelta(days=2 * w)).isoformat()
    cond = " or ".join(f"lower(review_text) like :kw{i}" for i in range(len(CRASH_WORDS)))
    kw_params = {f"kw{i}": f"%{w_.lower()}%" for i, w_ in enumerate(CRASH_WORDS)}
    cur = c.execute(
        text(
            f"""select count(*) from reviews where substr(submitted_at,1,10)>=:d0
                and substr(submitted_at,1,10)<=:as_of and ({cond})"""
        ),
        {"d0": d0, "as_of": as_of, **kw_params},
    ).fetchone()[0]
    prev = c.execute(
        text(
            f"""select count(*) from reviews where substr(submitted_at,1,10)>=:dprev
                and substr(submitted_at,1,10)<:d0 and ({cond})"""
        ),
        {"dprev": dprev, "d0": d0, **kw_params},
    ).fetchone()[0]
    growth = p.get("growth", 1.0)
    if cur >= p.get("min_abs", 5) and prev > 0 and (cur - prev) / prev > growth:
        return [{
            "code": "R5", "level": "P0", "stat_date": as_of, "subject": "崩溃类关键词",
            "metric": float(cur), "threshold": float(prev * (1 + growth)), "sample": cur,
            "evidence": f"近 {w} 日崩溃类词命中 {cur} 条，前 {w} 日 {prev} 条，环比 +{(cur-prev)/prev:.0%}",
        }]
    if cur >= p.get("min_abs", 5) and prev == 0:
        return [{
            "code": "R5", "level": "P0", "stat_date": as_of, "subject": "崩溃类关键词",
            "metric": float(cur), "threshold": float(p.get("min_abs", 5)), "sample": cur,
            "evidence": f"近 {w} 日崩溃类词命中 {cur} 条，前 {w} 日为 0",
        }]
    return []


def rule_r6(c, p, as_of: str) -> list[dict]:
    row = c.execute(
        text("select status, finished_at from sync_log order by id desc limit 1")
    ).mappings().fetchone()
    out = []
    if row and row["status"] != "ok":
        out.append({
            "code": "R6", "level": "P0", "stat_date": as_of, "subject": "同步任务",
            "metric": 0, "threshold": 0, "sample": 0,
            "evidence": f"最近一次同步状态为 {row['status']}",
        })
    days = _complete_days(c, as_of, lookback=10)
    zeros = [d for d, n, _ in days if n == 0]
    if len(zeros) >= p.get("window_days", 3):
        out.append({
            "code": "R6", "level": "P0", "stat_date": as_of, "subject": "同步任务",
            "metric": float(len(zeros)), "threshold": float(p.get("window_days", 3)), "sample": len(zeros),
            "evidence": f"近期有 {len(zeros)} 天零评论：{', '.join(zeros[:5])}",
        })
    return out


def rule_r7(c, p, as_of: str) -> list[dict]:
    days = [d for d, _, _ in _complete_days(c, as_of, lookback=60)]
    if not days:
        return []
    gaps = []
    prev = date.fromisoformat(days[0])
    for d in days[1:]:
        cur = date.fromisoformat(d)
        gap = (cur - prev).days - 1
        if gap > 0:
            gaps.append((prev.isoformat(), cur.isoformat(), gap))
        prev = cur
    max_gap = p.get("max_gap_days", 2)
    big = [g for g in gaps if g[2] > max_gap]
    if big:
        a, b, g = max(big, key=lambda x: x[2])
        return [{
            "code": "R7", "level": "P1", "stat_date": as_of, "subject": "数据缺口",
            "metric": float(g), "threshold": float(max_gap), "sample": len(big),
            "evidence": f"{a} → {b} 缺失 {g} 天（共 {len(big)} 处缺口）",
        }]
    return []


RULES = {
    "R1": rule_r1, "R2": rule_r2, "R3": rule_r3,
    "R4": rule_r4, "R5": rule_r5, "R6": rule_r6, "R7": rule_r7,
}


def evaluate(as_of: str | None = None, persist: bool = True) -> list[dict]:
    """评估全部规则。持久化时按 code+subject 去重合并（同一问题只维护一行），

    并对不再触发的既有事件自动置为 resolved，避免同一问题每天重复插入、重复发信。
    回放模式（persist=False）不落库、不影响既有事件状态。
    """
    as_of = as_of or date.today().isoformat()
    _ensure_tables()

    with engine.connect() as c:
        rules = _rules(c)
        c.commit()
        events = []
        for code, fn in RULES.items():
            p = rules.get(code, {})
            if not p.get("enabled", True):
                continue
            try:
                events.extend(fn(c, p, as_of))
            except Exception as e:  # 单条规则失败不影响其他规则
                events.append({
                    "code": code, "level": "P2", "stat_date": as_of, "subject": "规则执行异常",
                    "metric": 0, "threshold": 0, "sample": 0,
                    "evidence": f"{type(e).__name__}: {str(e)[:150]}",
                })

        if persist:
            now = datetime.now(timezone.utc).isoformat()
            triggered_keys = {(e["code"], e["subject"]) for e in events}

            for e in events:
                row = c.execute(
                    text(
                        """select id, status, occurrences, mute_until from alert_event
                           where code=:code and subject=:subject and status in ('open','acked','muted')
                           order by id desc limit 1"""
                    ),
                    {"code": e["code"], "subject": e["subject"]},
                ).mappings().fetchone()
                if row is None:
                    c.execute(
                        text(
                            """insert into alert_event(code, level, stat_date, subject, metric,
                               threshold, sample, evidence, created_at, status, occurrences, last_seen_at)
                               values(:code,:level,:stat_date,:subject,:metric,
                               :threshold,:sample,:evidence,:now,'open',1,:now)"""
                        ),
                        {**e, "now": now},
                    )
                    continue

                expired_mute = row["status"] == "muted" and row["mute_until"] and row["mute_until"] <= now
                new_status = "open" if (row["status"] != "muted" or expired_mute) else "muted"
                # 静默到期后重新浮现视为需要再次提醒
                reset_notify = ", notified_at = null" if expired_mute else ""
                c.execute(
                    text(
                        f"""update alert_event set level=:level, stat_date=:stat_date, metric=:metric,
                            threshold=:threshold, sample=:sample, evidence=:evidence,
                            occurrences=occurrences+1, last_seen_at=:now,
                            status=:new_status{reset_notify}
                            where id=:id"""
                    ),
                    {**e, "now": now, "new_status": new_status, "id": row["id"]},
                )

            # 不再触发的既有问题自动置为已解决（静默中的也一并解决，问题已消失无需继续静默）
            open_rows = c.execute(
                text("select id, code, subject from alert_event where status in ('open','acked','muted')")
            ).mappings().fetchall()
            for r in open_rows:
                if (r["code"], r["subject"]) not in triggered_keys:
                    c.execute(
                        text("update alert_event set status='resolved', resolved_at=:now where id=:id"),
                        {"now": now, "id": r["id"]},
                    )
            c.commit()
    return events


def ack(event_id: int, by: str) -> bool:
    _ensure_tables()
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as c:
        cur = c.execute(
            text(
                "update alert_event set status='acked', acked_by=:by, acked_at=:now "
                "where id=:id and status in ('open','muted')"
            ),
            {"by": by, "now": now, "id": event_id},
        )
        ok = cur.rowcount > 0
    return ok


def assign(event_id: int, assignee: str) -> bool:
    _ensure_tables()
    with engine.begin() as c:
        cur = c.execute(
            text("update alert_event set assignee=:assignee where id=:id"),
            {"assignee": assignee, "id": event_id},
        )
        ok = cur.rowcount > 0
    return ok


def mute(event_id: int, days: int = 7) -> bool:
    _ensure_tables()
    until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    with engine.begin() as c:
        cur = c.execute(
            text(
                "update alert_event set status='muted', mute_until=:until "
                "where id=:id and status in ('open','acked')"
            ),
            {"until": until, "id": event_id},
        )
        ok = cur.rowcount > 0
    return ok


def resolve(event_id: int) -> bool:
    _ensure_tables()
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as c:
        cur = c.execute(
            text(
                "update alert_event set status='resolved', resolved_at=:now "
                "where id=:id and status<>'resolved'"
            ),
            {"now": now, "id": event_id},
        )
        ok = cur.rowcount > 0
    return ok


RULE_CATALOG = [
    {"code": "R1", "name": "整体差评率劣化", "level": "P1",
     "desc": "近 7 个完整日差评率 > 90 日基线 + 2σ", "window": "7 日滚动", "min_sample": 500},
    {"code": "R2", "name": "新版本早期劣化", "level": "P0",
     "desc": "发布 7 日内差评率 > 上一版本同期 + 2pp", "window": "版本切片", "min_sample": 100},
    {"code": "R3", "name": "单一设备风险", "level": "P1",
     "desc": "差评率 > 15% 且独立作者 ≥ 20", "window": "30 日", "min_sample": 50},
    {"code": "R4", "name": "单一市场风险", "level": "P1",
     "desc": "语言市场差评率 > 15%", "window": "30 日", "min_sample": 100},
    {"code": "R5", "name": "崩溃类词突增", "level": "P0",
     "desc": "7 日 vs 前 7 日环比 +100% 且 ≥ 5 条", "window": "7 日", "min_sample": 5},
    {"code": "R6", "name": "同步异常", "level": "P0",
     "desc": "最近同步失败，或近期零评论日 ≥ 3 天", "window": "近期", "min_sample": 0},
    {"code": "R7", "name": "数据缺口", "level": "P1",
     "desc": "存在 > 2 天无数据", "window": "近 60 日", "min_sample": 0},
]


def rule_catalog() -> list[dict]:
    _ensure_tables()
    with engine.connect() as c:
        _rules(c)
        c.commit()
        enabled = {
            r["code"]: bool(r["enabled"])
            for r in c.execute(text("select code, enabled from alert_rule")).mappings()
        }
    return [{**r, "enabled": enabled.get(r["code"], True)} for r in RULE_CATALOG]


def recent(limit: int = 60, status: str | None = None) -> list[dict]:
    """告警事件列表。status 为 None 时默认排除已解决（避免历史噪音淹没当前问题）。"""
    _ensure_tables()
    with engine.connect() as c:
        if status:
            rows = c.execute(
                text(
                    """select * from alert_event where status=:status
                       order by (status='open') desc, id desc limit :limit"""
                ),
                {"status": status, "limit": limit},
            ).mappings().fetchall()
        else:
            rows = c.execute(
                text(
                    """select * from alert_event where status<>'resolved'
                       order by (status='open') desc, id desc limit :limit"""
                ),
                {"limit": limit},
            ).mappings().fetchall()
    return [dict(r) for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", help="回放日期 YYYY-MM-DD")
    ap.add_argument("--no-persist", action="store_true", help="只评估不落库")
    args = ap.parse_args()

    events = evaluate(args.as_of, persist=not args.no_persist)
    tag = f"回放 {args.as_of}" if args.as_of else "当前"
    print(f"[{tag}] 触发 {len(events)} 条告警")
    for e in events:
        print(f"  [{e['level']}] {e['code']} {e['subject']}  "
              f"实际 {e['metric']} > 阈值 {e['threshold']}（样本 {e['sample']}）")
        print(f"        {e['evidence']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
