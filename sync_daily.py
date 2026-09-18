#!/usr/bin/env python3
"""每日增量同步。

通道优先级：
  1) GCS 批量报告（数据完整，权威口径）—— 需要桶可读
     凭据优先用人类账号 OAuth token（Plan B），其次用服务账号 JSON
  2) Reviews API（内容监控用，比例指标不可信）—— 自动兜底

同步完成后会重算 daily_metric / topic_daily 两张预聚合表（近 400 天），
供未来跨天/跨维度历史趋势对比使用；失败不影响本次同步结果（仅打印警告）。

用法：
    python sync_daily.py                # 自动选通道
    python sync_daily.py --dry-run      # 只探测通道可用性，不写库
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

WORKDIR = Path(__file__).resolve().parent.parent
DB = WORKDIR / "work" / "reviews.db"
SKILL = Path(os.environ.get(
    "REVIEW_SKILL_DIR",
    Path.home() / ".workbuddy" / "skills" / "app-review-insight" / "scripts",
))
PY = Path(os.environ.get("REVIEW_PY", sys.executable)).resolve()

PACKAGE = "com.tcl.browser"
BUCKET = os.environ.get("PLAY_BUCKET", "pubsite_prod_rev_16693200349579533994")
OAUTH_TOKEN = Path(
    os.environ.get("REVIEW_OAUTH_TOKEN")
    or (Path.home() / ".workbuddy" / "secrets" / "play_report_oauth.json")
)
SA_KEY = Path(os.environ["PLAY_SA_KEY"]) if os.environ.get("PLAY_SA_KEY") else None


def db_count() -> tuple[int, str]:
    c = sqlite3.connect(DB)
    n, last = c.execute("select count(*), max(submitted_at) from reviews").fetchone()
    c.close()
    return n, last or ""


def pick_credential() -> tuple[Path | None, str]:
    """返回 (凭证路径, 类型)。OAuth token 优先。"""
    if OAUTH_TOKEN.exists():
        return OAUTH_TOKEN, "oauth"
    if SA_KEY and SA_KEY.exists():
        return SA_KEY, "service_account"
    return None, "none"


def gcs_readable(cred: Path) -> tuple[bool, str]:
    """探测 GCS 桶是否可读（列出 reviews/ 前缀）。"""
    env = dict(os.environ, GOOGLE_APPLICATION_CREDENTIALS=str(cred))
    code = """
import sys
from google.cloud import storage
from google.api_core import exceptions as ge
try:
    c = storage.Client()
    blobs = list(c.list_blobs(c.bucket(sys.argv[1]), prefix='reviews/'))
    print('OK', len(blobs))
except ge.Forbidden as e:
    print('403', str(e)[:120])
except Exception as e:
    print('ERR', type(e).__name__, str(e)[:120])
"""
    r = subprocess.run(
        [str(PY), "-c", code, BUCKET],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=120,
    )
    out = (r.stdout or "").strip()
    if out.startswith("OK"):
        return True, out
    return False, out or (r.stderr or "")[:200]


def run(cmd: list[str], env_extra: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ, **env_extra)
    return subprocess.run(
        [str(PY)] + cmd, capture_output=True, text=True, encoding="utf-8",
        env=env, timeout=900,
    )


def refresh_aggregates() -> None:
    """重算预聚合表；失败仅打印警告，不影响同步主流程的返回值。"""
    try:
        import analytics
        n1 = analytics.refresh_daily_metric(days=400)
        n2 = analytics.refresh_topic_daily(days=400)
        print(f"预聚合表已刷新：daily_metric {n1} 行，topic_daily {n2} 行")
    except Exception as e:
        print(f"预聚合表刷新失败（不影响本次同步结果）：{type(e).__name__}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    before, last_before = db_count()
    print(f"同步前：{before:,} 条，最新 {last_before[:19]}")

    cred, cred_type = pick_credential()
    print(f"凭证：{cred_type}" + (f"（{cred.name}）" if cred else ""))

    channel = None
    detail = ""

    if cred:
        ok, info = gcs_readable(cred)
        print(f"GCS 探测：{info}")
        if ok:
            channel = "gcs"
        else:
            detail = info

    if args.dry_run:
        print(f"[dry-run] 将使用通道：{channel or 'api（GCS 不可用）'}")
        return 0

    if channel == "gcs":
        ym = date.today().strftime("%Y%m")
        prev = (date.today().replace(day=1) - __import__("datetime").timedelta(days=1)).strftime("%Y%m")
        r = run(
            [str(SKILL / "fetch_gcs.py"), "--bucket", BUCKET, "--package", PACKAGE,
             "--since", prev, "--db", str(DB)],
            {"GOOGLE_APPLICATION_CREDENTIALS": str(cred)},
        )
        print(r.stdout[-1500:])
        if r.returncode != 0:
            print("GCS 采集失败，降级到 API：", r.stderr[-400:])
            channel = None

    if channel is None:
        r = run(
            [str(SKILL / "run_all.py"), "--source", "api", "--package", PACKAGE, "--db", str(DB)],
            {"GOOGLE_APPLICATION_CREDENTIALS": str(cred)} if cred else {},
        )
        if r.returncode != 0:
            print("API 采集失败：", r.stderr[-600:])
            return 1
        channel = "api"

    after, last_after = db_count()
    print()
    print(f"通道：{channel}")
    print(f"同步后：{after:,} 条（新增 {after - before:,}），最新 {last_after[:19]}")

    summary = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "channel": channel,
        "before": before,
        "after": after,
        "added": after - before,
        "last_review": last_after,
        "credential": cred_type,
        "gcs_detail": detail,
    }
    log = WORKDIR / "work" / "sync_history.jsonl"
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")
    print(f"已记录：{log.name}")

    refresh_aggregates()
    return 0


if __name__ == "__main__":
    sys.exit(main())
