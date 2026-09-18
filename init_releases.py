#!/usr/bin/env python3
"""初始化发版记录表。

Play 的版本名形如 8.20.030_38303fc_260904_gp，其中 260904 即构建日期（YYMMDD），
可直接解析为发布日期；解析失败时退回"该版本首条评论日期"作为近似。

用法：
    python init_releases.py            # 建表并写入/更新自动推导的记录
    python init_releases.py --show     # 只查看
"""
from __future__ import annotations

import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

WORKDIR = Path(__file__).resolve().parent.parent
DB = WORKDIR / "work" / "reviews.db"

BUILD_DATE_RE = re.compile(r"_(\d{6})_[a-z]+$", re.I)

DDL = """
create table if not exists release (
    version       text primary key,
    released_at   text,
    rollout_pct   integer,
    note          text,
    source        text default 'auto',
    first_seen    text,
    review_count  integer,
    updated_at    text
);
"""


def parse_build_date(version: str) -> str | None:
    m = BUILD_DATE_RE.search(version.strip())
    if not m:
        return None
    s = m.group(1)
    try:
        yy, mm, dd = int(s[:2]), int(s[2:4]), int(s[4:6])
        return date(2000 + yy, mm, dd).isoformat()
    except ValueError:
        return None


def main() -> int:
    show = "--show" in sys.argv
    c = sqlite3.connect(DB)
    c.execute(DDL)

    rows = c.execute(
        """select app_version_name, count(*), min(substr(submitted_at,1,10)), max(substr(submitted_at,1,10))
           from reviews where coalesce(app_version_name,'')<>''
           group by 1 having count(*)>=30 order by 1"""
    ).fetchall()

    now = date.today().isoformat()
    parsed = fallback = 0
    for ver, n, first, last in rows:
        built = parse_build_date(ver)
        src = "auto:build" if built else "auto:first_seen"
        if built:
            parsed += 1
        else:
            fallback += 1
        c.execute(
            """insert into release(version, released_at, note, source, first_seen, review_count, updated_at)
               values(?,?,?,?,?,?,?)
               on conflict(version) do update set
                 first_seen=excluded.first_seen,
                 review_count=excluded.review_count,
                 updated_at=excluded.updated_at""",
            (ver, built or first, f"末现 {last}", src, first, n, now),
        )
    c.commit()

    if show:
        print(f"{'版本':<34}{'发布日':<12}{'首现':<12}{'评论量':>8}  来源")
        for ver, rel, src, fs, n in c.execute(
            "select version, released_at, source, first_seen, review_count from release order by released_at desc limit 30"
        ):
            print(f"{ver:<34}{rel:<12}{fs:<12}{n:>8}  {src}")
    else:
        total = c.execute("select count(*) from release").fetchone()[0]
        print(f"发版记录 {total} 条：构建日期解析 {parsed} 条，首现日期兜底 {fallback} 条")
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
