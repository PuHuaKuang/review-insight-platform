"""指标计算与查询层。

所有比例类指标只基于全量口径（CSV / GCS），不混用 Reviews API 样本——
API 返回的是"有正文评论"的近完整采样，其差评率系统性高估约 1.76 倍。

查询统一走 SQLAlchemy engine（models.engine），不再直连 sqlite3 模块：
这样切换 REVIEW_DB_URL 到 Postgres 时，本文件的连接层不用改，
SQL 一律用 text() + 命名参数（:name），不用位置参数（?），因为
不同数据库驱动的位置参数占位符风格不同，命名参数经 SQLAlchemy 方言层
统一转换，才具备跨库可移植性。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from config import WORKDIR
from models import engine

REPORT_JSON = WORKDIR / "work" / "report.json"

DIM_COLUMN = {
    "version": "app_version_name",
    "device": "device",
    "lang": "reviewer_lang",
}


def _month_of(col: str) -> str:
    return f"substr({col},1,7)"


def _day_of(col: str) -> str:
    return f"substr({col},1,10)"


def overview() -> dict:
    with engine.connect() as c:
        total, avg, neg = c.execute(text(
            "select count(*), avg(star_rating), "
            "sum(case when star_rating<=2 then 1 else 0 end) from reviews"
        )).fetchone()
        txt, txt_neg = c.execute(text(
            "select count(*), sum(case when star_rating<=2 then 1 else 0 end) "
            "from reviews where review_text<>''"
        )).fetchone()
        last = c.execute(text("select max(submitted_at) from reviews")).fetchone()[0]
        first = c.execute(text("select min(submitted_at) from reviews")).fetchone()[0]
        src = dict(c.execute(text("select source, count(*) from reviews group by 1")).fetchall())
    return {
        "total": total,
        "avg_rating": round(avg or 0, 3),
        "negative": neg,
        "negative_rate": round((neg or 0) / total, 4) if total else 0,
        "text_total": txt,
        "text_negative_rate": round((txt_neg or 0) / txt, 4) if txt else 0,
        "text_share": round(txt / total, 4) if total else 0,
        "first_review": first,
        "last_review": last,
        "by_source": src,
    }


def monthly_trend() -> list[dict]:
    """月度趋势，含完整月判定与截断标记。"""
    with engine.connect() as c:
        rows = c.execute(text(
            f"""select {_month_of('submitted_at')} m,
                       count(*) n,
                       avg(star_rating) a,
                       sum(case when star_rating<=2 then 1 else 0 end) neg,
                       count(distinct {_day_of('submitted_at')}) covered,
                       max({_day_of('submitted_at')}) last_day
                from reviews group by 1 order by 1"""
        )).fetchall()

    today = date.today()
    out = []
    for m, n, a, neg, covered, last_day in rows:
        if not m:
            continue
        y, mm = int(m[:4]), int(m[5:7])
        days_in_month = (
            (date(y + (mm == 12), (mm % 12) + 1, 1) - date(y, mm, 1)).days
        )
        is_current = (y == today.year and mm == today.month)
        partial = is_current or covered < days_in_month
        out.append(
            {
                "month": m,
                "count": n,
                "avg_rating": round(a or 0, 3),
                "negative": neg,
                "negative_rate": round((neg or 0) / n, 4) if n else 0,
                "covered_days": covered,
                "days_in_month": days_in_month,
                "partial": partial,
                "current": is_current,
            }
        )
    return out


def daily_trend(days: int = 60) -> list[dict]:
    with engine.connect() as c:
        rows = c.execute(text(
            f"""select {_day_of('submitted_at')} d, count(*) n,
                       sum(case when star_rating<=2 then 1 else 0 end) neg,
                       avg(star_rating) a
                from reviews group by 1 order by 1 desc limit :n"""
        ), {"n": days}).fetchall()
    return [
        {
            "date": d,
            "count": n,
            "negative": neg,
            "negative_rate": round(neg / n, 4) if n else 0,
            "avg_rating": round(a or 0, 3),
        }
        for d, n, neg, a in reversed(rows)
    ]


def dimension_risk(dim: str, min_n: int = 100, since: str | None = None) -> list[dict]:
    col = DIM_COLUMN.get(dim)
    if not col:
        return []
    where = [f"coalesce({col},'')<>''"]
    params: dict = {"min_n": min_n}
    if since:
        where.append("submitted_at>=:since")
        params["since"] = since
    with engine.connect() as c:
        rows = c.execute(text(
            f"""select {col} v, count(*) n, avg(star_rating) a,
                       sum(case when star_rating<=2 then 1 else 0 end) neg,
                       count(distinct author_name) authors
                from reviews where {' and '.join(where)}
                group by 1 having n>=:min_n order by 1.0*neg/n desc, n desc limit 25"""
        ), params).fetchall()
    return [
        {
            "value": v,
            "count": n,
            "avg_rating": round(a or 0, 3),
            "negative": neg,
            "negative_rate": round(neg / n, 4),
            "authors": authors,
        }
        for v, n, a, neg, authors in rows
    ]


def version_health(limit: int = 15) -> list[dict]:
    """版本健康矩阵：按版本聚合，附首发日期。"""
    with engine.connect() as c:
        rows = c.execute(text(
            """select app_version_name v, count(*) n, avg(star_rating) a,
                      sum(case when star_rating<=2 then 1 else 0 end) neg,
                      min(substr(submitted_at,1,10)) first_day,
                      max(substr(submitted_at,1,10)) last_day
               from reviews where coalesce(app_version_name,'')<>''
               group by 1 having n>=50 order by last_day desc, n desc limit :n"""
        ), {"n": limit}).fetchall()
    return [
        {
            "version": v,
            "count": n,
            "avg_rating": round(a or 0, 3),
            "negative_rate": round(neg / n, 4),
            "first_day": fd,
            "last_day": ld,
        }
        for v, n, a, neg, fd, ld in rows
    ]


def _search_where(
    star_max: int | None,
    version: str | None,
    device: str | None,
    lang: str | None,
    keyword: str | None,
    has_text: bool,
    since: str | None,
) -> tuple[str, dict]:
    """构造筛选条件，全部走命名绑定参数，避免 SQL 注入。供检索与导出复用同一口径。"""
    where: list[str] = []
    params: dict = {}
    if has_text:
        where.append("coalesce(review_text,'')<>''")
    if star_max is not None:
        where.append("star_rating<=:star_max")
        params["star_max"] = int(star_max)
    if version:
        where.append("app_version_name like :version")
        params["version"] = f"%{version}%"
    if device:
        where.append("device like :device")
        params["device"] = f"%{device}%"
    if lang:
        where.append("reviewer_lang=:lang")
        params["lang"] = lang
    if since:
        where.append("submitted_at>=:since")
        params["since"] = since
    if keyword:
        where.append("(review_text like :keyword or app_version_name like :keyword)")
        params["keyword"] = f"%{keyword}%"
    w = (" where " + " and ".join(where)) if where else ""
    return w, params


def search_reviews(
    star_max: int | None = None,
    version: str | None = None,
    device: str | None = None,
    lang: str | None = None,
    keyword: str | None = None,
    has_text: bool = True,
    since: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    w, params = _search_where(star_max, version, device, lang, keyword, has_text, since)

    with engine.connect() as c:
        total = c.execute(text(f"select count(*) from reviews{w}"), params).fetchone()[0]
        rows = c.execute(text(
            f"""select substr(submitted_at,1,10), star_rating, reviewer_lang, device,
                       app_version_name, substr(review_text,1,300)
                from reviews{w} order by submitted_at desc limit :limit offset :offset"""
        ), {**params, "limit": limit, "offset": offset}).fetchall()
    items = [
        {
            "date": d,
            "star": s,
            "lang": lg,
            "device": dv,
            "version": v,
            "text": t,
        }
        for d, s, lg, dv, v, t in rows
    ]
    return items, total


EXPORT_MAX_ROWS = 20000


def export_reviews(
    star_max: int | None = None,
    version: str | None = None,
    device: str | None = None,
    lang: str | None = None,
    keyword: str | None = None,
    has_text: bool = True,
    since: str | None = None,
) -> tuple[list[dict], int, bool]:
    """导出用，筛选口径与 search_reviews 一致，不分页但设上限，返回正文全文。

    返回 (rows, total, truncated)：truncated 表示命中数超过导出上限，仅导出前 N 条。
    """
    w, params = _search_where(star_max, version, device, lang, keyword, has_text, since)
    with engine.connect() as c:
        total = c.execute(text(f"select count(*) from reviews{w}"), params).fetchone()[0]
        rows = c.execute(text(
            f"""select substr(submitted_at,1,10), star_rating, reviewer_lang, device,
                       android_version, app_version_name, review_text, source
                from reviews{w} order by submitted_at desc limit :limit"""
        ), {**params, "limit": EXPORT_MAX_ROWS}).fetchall()
    items = [
        {
            "date": d, "star": s, "lang": lg, "device": dv,
            "android_version": av, "version": v, "text": t, "source": src,
        }
        for d, s, lg, dv, av, v, t, src in rows
    ]
    return items, total, total > EXPORT_MAX_ROWS


def releases(limit: int = 40) -> list[dict]:
    """发版记录。released_at 取自版本名中的构建日期（YYMMDD），比首现日期可靠。"""
    try:
        with engine.connect() as c:
            rows = c.execute(text(
                """select version, released_at, rollout_pct, note, source, first_seen, review_count
                   from release order by released_at desc limit :n"""
            ), {"n": limit}).fetchall()
    except OperationalError:
        return []
    keys = [
        "version", "released_at", "rollout_pct", "note",
        "source", "first_seen", "review_count",
    ]
    return [dict(zip(keys, r)) for r in rows]


def data_freshness() -> dict:
    """数据截止日、缺口与来源分布。"""
    with engine.connect() as c:
        last = c.execute(text("select max(submitted_at) from reviews")).fetchone()[0]
        recent = c.execute(text(
            f"""select {_day_of('submitted_at')} d, count(*) n from reviews
                where submitted_at >= date('now','-14 days') group by 1 order by 1"""
        )).fetchall()
        cov = c.execute(text(
            """select round(1.0*sum(case when coalesce(app_version_name,'')<>'' then 1 else 0 end)/count(*),3),
                      round(1.0*sum(case when coalesce(review_text,'')<>'' then 1 else 0 end)/count(*),3),
                      count(*) from reviews"""
        )).fetchone()

    recent = [tuple(r) for r in recent]
    counts = [n for _, n in recent]
    median = sorted(counts)[len(counts) // 2] if counts else 0
    tail_low = [d for d, n in recent[-3:]] if median and all(
        n < median * 0.5 for _, n in recent[-3:]
    ) else []

    return {
        "last_review": last,
        "recent_daily": recent,
        "median_daily": median,
        "truncated_days": tail_low,
        "version_coverage": cov[0],
        "text_coverage": cov[1],
        "total": cov[2],
    }


def sync_log(limit: int = 20) -> list[dict]:
    with engine.connect() as c:
        rows = c.execute(text(
            """select id, source, started_at, finished_at, rows_seen, rows_upsert, status, detail
               from sync_log order by id desc limit :n"""
        ), {"n": limit}).fetchall()
    keys = [
        "id", "source", "started_at", "finished_at",
        "rows_seen", "rows_upsert", "status", "detail",
    ]
    return [dict(zip(keys, r)) for r in rows]


def topics() -> list[dict]:
    """主题来自技能已生成 report.json（确定性关键词口径）。"""
    if not REPORT_JSON.exists():
        return []
    data = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    return data.get("topics", [])


def _complete_daily_rows(lookback: int = 220) -> list[tuple[str, int, int]]:
    """按日聚合（升序），剔除报告仍在写入的尾部（口径与 alerts._complete_days 一致）。"""
    with engine.connect() as c:
        rows = c.execute(text(
            f"""select {_day_of('submitted_at')} d, count(*) n,
                       sum(case when star_rating<=2 then 1 else 0 end) neg
                from reviews where {_day_of('submitted_at')} is not null
                group by 1 order by 1 desc limit :n"""
        ), {"n": lookback}).fetchall()
    rows = [(d, n, neg) for d, n, neg in rows if d]
    if len(rows) < 5:
        return list(reversed(rows))
    counts = sorted(n for _, n, _ in rows)
    median = counts[len(counts) // 2]
    drop = 0
    for _, n, _ in rows:  # rows 为按日期倒序（最近的在前）
        if median and n < median * 0.5:
            drop += 1
        else:
            break
    return list(reversed(rows[drop:]))  # 升序，最近的在末尾


def _agg(seg: list[tuple[str, int, int]]) -> tuple[int, int, float | None]:
    n = sum(x[1] for x in seg)
    neg = sum(x[2] for x in seg)
    return n, neg, (neg / n if n else None)


def summary(window_days: int = 7, min_sample: int = 200) -> dict:
    """首页一句话结论：当前窗口 vs 上周同长度窗口 / 上月同长度窗口 / 历史分位 / 90 日基线。

    口径与告警 R1 一致：滚动窗口取近 N 个完整日，已剔除报告延迟尾部；
    基线为过去 90 个完整日（含当前窗口）的差评率，按二项分布近似算 2σ 阈值。
    """
    complete = _complete_daily_rows()
    if len(complete) < window_days:
        return {
            "ok": False,
            "verdict": "数据不足，暂无法给出结论",
            "level": "info",
        }

    cur = complete[-window_days:]
    cur_n, cur_neg, cur_rate = _agg(cur)

    prev_week = complete[-2 * window_days : -window_days] if len(complete) >= 2 * window_days else []
    pw_n, pw_neg, pw_rate = _agg(prev_week)

    mom_start, mom_end = -(28 + window_days), -28
    prev_month = complete[mom_start:mom_end] if len(complete) >= -mom_start else []
    pm_n, pm_neg, pm_rate = _agg(prev_month)

    base_days = complete[-90:]
    bn, bneg, baseline = _agg(base_days)
    sigma = ((baseline or 0) * (1 - (baseline or 0)) / cur_n) ** 0.5 if cur_n and baseline is not None else 0
    threshold = (baseline or 0) + 2 * sigma

    # 历史分位：过去窗口（不含当前）滚动 7 日差评率分布，样本不足的窗口跳过
    hist_rates = []
    for i in range(window_days, len(complete)):
        seg = complete[i - window_days : i]
        n = sum(x[1] for x in seg)
        if n < min_sample:
            continue
        neg = sum(x[2] for x in seg)
        hist_rates.append(neg / n)
    hist_rates = hist_rates[-180:]
    percentile = (
        round(100 * sum(1 for r in hist_rates if r <= cur_rate) / len(hist_rates))
        if hist_rates and cur_rate is not None
        else None
    )

    wow_delta = (cur_rate - pw_rate) if (cur_rate is not None and pw_rate is not None) else None
    mom_delta = (cur_rate - pm_rate) if (cur_rate is not None and pm_rate is not None) else None

    level, verdict = _verdict(
        cur_n, cur_rate, min_sample, baseline, threshold, wow_delta, mom_delta, percentile
    )

    return {
        "ok": True,
        "level": level,
        "verdict": verdict,
        "window_days": window_days,
        "current": {"count": cur_n, "negative": cur_neg, "negative_rate": _r(cur_rate)},
        "prev_week": {"count": pw_n, "negative_rate": _r(pw_rate)} if pw_n else None,
        "prev_month": {"count": pm_n, "negative_rate": _r(pm_rate)} if pm_n else None,
        "wow_delta": _r(wow_delta),
        "mom_delta": _r(mom_delta),
        "baseline": {"days": len(base_days), "sample": bn, "negative_rate": _r(baseline), "threshold": _r(threshold)},
        "percentile": percentile,
        "last_day": complete[-1][0] if complete else None,
    }


def _r(v: float | None, nd: int = 4) -> float | None:
    return round(v, nd) if v is not None else None


def _verdict(
    cur_n: int,
    cur_rate: float | None,
    min_sample: int,
    baseline: float | None,
    threshold: float,
    wow_delta: float | None,
    mom_delta: float | None,
    percentile: int | None,
) -> tuple[str, str]:
    if cur_n < min_sample or cur_rate is None:
        return "info", f"近 7 日样本仅 {cur_n} 条，样本不足以给出可信结论"

    pct_txt = f"，处于近半年 {percentile}% 分位" if percentile is not None else ""

    if baseline is not None and cur_rate > threshold:
        delta_pp = (cur_rate - baseline) * 100
        return (
            "warn",
            f"需要关注：近 7 日差评率 {cur_rate:.2%}，较 90 日基线 {baseline:.2%} 高出 {delta_pp:.1f}pp，超出正常波动区间{pct_txt}",
        )

    if wow_delta is not None and wow_delta > 0.02:
        return (
            "watch",
            f"较上周上升 {wow_delta*100:.1f}pp（{cur_rate:.2%} vs {cur_rate-wow_delta:.2%}），建议留意{pct_txt}",
        )

    if mom_delta is not None and mom_delta > 0.02:
        return (
            "watch",
            f"较上月同期上升 {mom_delta*100:.1f}pp（{cur_rate:.2%} vs {cur_rate-mom_delta:.2%}），建议留意{pct_txt}",
        )

    wow_txt = f"，较上周 {'上升' if wow_delta and wow_delta>0 else '下降' if wow_delta else '基本持平'} {abs(wow_delta)*100:.1f}pp" if wow_delta is not None else ""
    return "ok", f"健康：近 7 日差评率 {cur_rate:.2%}，处于正常区间{wow_txt}{pct_txt}"


# ---------------------------------------------------------------------------
# 预聚合表写入（daily_metric / topic_daily）
#
# 之前这两张表在 models.py 里定义了 schema，但从未被写入过任何数据——
# 大盘和分析工作台的所有查询都是直接对 reviews 全表现算，数据量还小
# （13万行级）所以现算也没有性能问题，写这两张表纯粹是"填坑"：
# 为将来数据量上到百万级、或者要做跨天/跨维度的历史趋势对比时留好预聚合层，
# 现有 API 行为不受影响（analytics.py 的查询函数都不读这两张表）。
# ---------------------------------------------------------------------------

TOPIC_MODULE_HINT = (
    Path.home() / ".workbuddy" / "skills" / "app-review-insight" / "scripts" / "analyze.py"
)


def refresh_daily_metric(days: int = 400) -> int:
    """重算并写入 daily_metric：stat_date x dim_type(all/version/device/lang) x dim_value。

    全量重算最近 N 天（幂等，delete+insert），不做增量 diff——
    数据量级（13万行/400天）全表聚合耗时在百毫秒级，没必要做增量判断的复杂度。
    """
    with engine.begin() as c:
        since = c.execute(text(
            f"select min({_day_of('submitted_at')}) from reviews"
        )).fetchone()[0]
        rows_all = c.execute(text(
            f"""select {_day_of('submitted_at')} d, count(*) n, avg(star_rating) a,
                       sum(case when star_rating<=2 then 1 else 0 end) neg
                from reviews where {_day_of('submitted_at')} is not null
                group by 1 order by 1 desc limit :n"""
        ), {"n": days}).fetchall()
        dates = [d for d, *_ in rows_all]
        if not dates:
            return 0
        min_d = min(dates)

        c.execute(text("delete from daily_metric where stat_date>=:since"), {"since": min_d})

        written = 0
        for d, n, a, neg in rows_all:
            c.execute(text(
                """insert into daily_metric(stat_date, dim_type, dim_value, total, avg_rating, negative, negative_rate)
                   values(:d,'all','all',:n,:a,:neg,:rate)"""
            ), {"d": d, "n": n, "a": round(a or 0, 3), "neg": neg, "rate": round((neg or 0) / n, 4) if n else 0})
            written += 1

        for dim_type, col in DIM_COLUMN.items():
            rows = c.execute(text(
                f"""select {_day_of('submitted_at')} d, {col} v, count(*) n, avg(star_rating) a,
                           sum(case when star_rating<=2 then 1 else 0 end) neg
                    from reviews
                    where {_day_of('submitted_at')} >= :since and coalesce({col},'')<>''
                    group by 1, 2"""
            ), {"since": min_d}).fetchall()
            for d, v, n, a, neg in rows:
                c.execute(text(
                    """insert into daily_metric(stat_date, dim_type, dim_value, total, avg_rating, negative, negative_rate)
                       values(:d,:dt,:v,:n,:a,:neg,:rate)"""
                ), {
                    "d": d, "dt": dim_type, "v": v, "n": n,
                    "a": round(a or 0, 3), "neg": neg,
                    "rate": round((neg or 0) / n, 4) if n else 0,
                })
                written += 1
    return written


def refresh_topic_daily(days: int = 400) -> int:
    """重算并写入 topic_daily：调用技能 analyze.py 里的 TOPIC_RE 词典，逐日重新扫描正文。

    动态 import 技能脚本而不是复制词典到平台目录，避免主题口径出现两份定义漂移。
    """
    import importlib.util
    import sys as _sys

    if not TOPIC_MODULE_HINT.exists():
        return 0
    spec = importlib.util.spec_from_file_location("_skill_analyze", TOPIC_MODULE_HINT)
    mod = importlib.util.module_from_spec(spec)
    _sys.modules.setdefault("_skill_analyze", mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    topic_re: dict = mod.TOPIC_RE

    with engine.begin() as c:
        since_row = c.execute(text(
            f"select min({_day_of('submitted_at')}) from reviews "
            f"where {_day_of('submitted_at')} >= (select max({_day_of('submitted_at')}) from reviews)"
        )).fetchone()
        max_d = c.execute(text(f"select max({_day_of('submitted_at')}) from reviews")).fetchone()[0]
        if not max_d:
            return 0
        rows = c.execute(text(
            f"""select {_day_of('submitted_at')} d, review_text, star_rating from reviews
                where {_day_of('submitted_at')} is not null and coalesce(review_text,'')<>''
                order by submitted_at desc limit :lim"""
        ), {"lim": days * 400}).fetchall()

        by_day: dict[str, dict[str, list[int]]] = {}
        min_d = None
        seen_days = set()
        for d, txt, star in rows:
            seen_days.add(d)
            if len(seen_days) > days:
                continue
            bucket = by_day.setdefault(d, {})
            for name, rx in topic_re.items():
                if rx.search(txt or ""):
                    lst = bucket.setdefault(name, [0, 0])
                    lst[0] += 1
                    if star is not None and star <= 2:
                        lst[1] += 1
        if by_day:
            min_d = min(by_day.keys())
            c.execute(text("delete from topic_daily where stat_date>=:since"), {"since": min_d})

        written = 0
        for d, topics_map in by_day.items():
            for name, (mentions, neg_mentions) in topics_map.items():
                c.execute(text(
                    """insert into topic_daily(stat_date, topic, mentions, negative_mentions, negative_ratio)
                       values(:d,:t,:m,:nm,:r)"""
                ), {
                    "d": d, "t": name, "m": mentions, "nm": neg_mentions,
                    "r": round(neg_mentions / mentions, 4) if mentions else 0,
                })
                written += 1
    return written
