#!/usr/bin/env python3
"""告警邮件通知。

配置（环境变量）：
    SMTP_HOST     如 smtp.office365.com
    SMTP_PORT     默认 587
    SMTP_USER     发件账号
    SMTP_PASS     密码或应用专用密码
    ALERT_TO      收件人，多个用逗号分隔（建议配置，不要把邮箱写进代码）
    ALERT_FROM    发件人显示，默认取 SMTP_USER

未配置 SMTP 时不报错，改为把邮件正文写入 work/outbox/，供其他通道（如 Agent Mail）发送。

去重口径：按 alert_event.notified_at 字段判断是否已通知过，而不是按时间戳游标。
同一问题（code+subject）在 evaluate() 中会合并为一行，只要该行还未标记 notified_at
就会被本脚本捞出发送；发送成功后写回 notified_at，同一问题不会被重复发信。
问题被静默（muted）或已解决（resolved）期间不发信；静默到期后 evaluate() 会重置
notified_at=null，问题若仍存在会重新提醒。

用法：
    python notify.py                # 发送尚未通知过的 open/acked 告警
    python notify.py --dry-run      # 只渲染不发送，不标记 notified_at
    python notify.py --all          # 忽略 notified_at，重发最近 20 条（人工排查用）
"""
from __future__ import annotations

import argparse
import smtplib
import sys
from datetime import datetime, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from sqlalchemy import text

from config import WORKDIR
from models import engine

OUTBOX = WORKDIR / "work" / "outbox"
PACKAGE = "com.tcl.browser"
DEFAULT_TO = ""


def fetch_events(all_recent: bool = False, limit: int = 20) -> list[dict]:
    with engine.connect() as c:
        if all_recent:
            rows = c.execute(
                text("select * from alert_event order by id desc limit :limit"),
                {"limit": limit},
            ).mappings().fetchall()
        else:
            rows = c.execute(
                text(
                    """select * from alert_event
                       where status in ('open','acked') and notified_at is null
                       order by id desc limit :limit"""
                ),
                {"limit": limit},
            ).mappings().fetchall()
    return [dict(r) for r in rows]


def mark_notified(ids: list[int]) -> None:
    if not ids:
        return
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as c:
        c.execute(
            text("update alert_event set notified_at=:now where id=:id"),
            [{"now": now, "id": i} for i in ids],
        )


def render(events: list[dict], as_of: str) -> tuple[str, str, str]:
    p0 = [e for e in events if e["level"] == "P0"]
    subject = f"【评价观测】{PACKAGE} 新告警 {len(events)} 条（P0 {len(p0)} 条）· {as_of}"

    rows = "".join(
        f"""<tr>
      <td style="padding:6px 8px;border-bottom:1px solid #eee">
        <b style="color:{'#c0392b' if e['level']=='P0' else '#b9770e'}">{e['level']}</b></td>
      <td style="padding:6px 8px;border-bottom:1px solid #eee">{e['code']}</td>
      <td style="padding:6px 8px;border-bottom:1px solid #eee">{e['subject']}</td>
      <td style="padding:6px 8px;border-bottom:1px solid #eee">{e['metric']} / 阈值 {e['threshold']}</td>
      <td style="padding:6px 8px;border-bottom:1px solid #eee">{e['sample']}</td>
      <td style="padding:6px 8px;border-bottom:1px solid #eee;color:#666">{e['evidence'] or ''}</td>
    </tr>"""
        for e in events
    )

    html = f"""<div style="font-family:Segoe UI,Microsoft YaHei,sans-serif;font-size:14px;color:#222">
  <h3 style="margin:0 0 12px">{PACKAGE} 评论告警</h3>
  <p style="margin:0 0 12px;color:#555">评估时间 {as_of} · 共 {len(events)} 条（P0 {len(p0)} 条）</p>
  <table style="border-collapse:collapse;width:100%;font-size:13px">
    <tr style="background:#f6f6f6;text-align:left">
      <th style="padding:6px 8px">级别</th><th style="padding:6px 8px">规则</th>
      <th style="padding:6px 8px">对象</th><th style="padding:6px 8px">实际/阈值</th>
      <th style="padding:6px 8px">样本</th><th style="padding:6px 8px">证据</th>
    </tr>{rows}
  </table>
  <p style="margin:16px 0 0;color:#888;font-size:12px">
    口径说明：比例指标基于全量评论（批量报告），已排除 Reviews API 样本；
    滚动窗口取近 7 个完整日，自动剔除报告延迟导致的尾部不完整数据。
    同一问题只会通知一次，可在告警中心确认（ack）/ 指派 / 静默处理。<br>
    本邮件由用户评价观测平台自动发出。
  </p>
</div>"""

    text_body = "\n".join(
        f"[{e['level']}] {e['code']} {e['subject']}  实际 {e['metric']} > 阈值 {e['threshold']}"
        f"（样本 {e['sample']}）\n    {e['evidence'] or ''}"
        for e in events
    )
    return subject, html, text_body


def send_smtp(subject: str, html: str, text_body: str, to: list[str]) -> tuple[bool, str]:
    import os

    host, port = os.environ.get("SMTP_HOST"), int(os.environ.get("SMTP_PORT", "587"))
    user, pwd = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
    sender = os.environ.get("ALERT_FROM") or user
    if not (host and user and pwd and sender):
        return False, "未配置 SMTP_HOST/SMTP_USER/SMTP_PASS"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, pwd)
            s.sendmail(sender, to, msg.as_string())
        return True, f"已发送到 {', '.join(to)}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:200]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    events = fetch_events(all_recent=args.all)
    as_of = datetime.now().strftime("%Y-%m-%d %H:%M")
    if not events:
        print("没有待通知的新告警（open/acked 且未发送过），不发送")
        return 0

    subject, html, text_body = render(events, as_of)
    print(f"主题：{subject}")
    for e in events:
        print(f"  [{e['level']}] {e['code']} {e['subject']} {e['metric']} > {e['threshold']}")

    import os
    to = [x.strip() for x in (os.environ.get("ALERT_TO") or DEFAULT_TO).split(",") if x.strip()]
    if not to:
        print("未配置收件人：请设置环境变量 ALERT_TO，例如 ALERT_TO=you@company.com")
        return 2

    if args.dry_run:
        print("[dry-run] 不实际发送，不标记 notified_at")
        return 0

    ok, detail = send_smtp(subject, html, text_body, to)
    if ok:
        print(detail)
        if not args.all:
            mark_notified([e["id"] for e in events])
        return 0

    OUTBOX.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    f = OUTBOX / f"alert_{ts}.html"
    f.write_text(f"<h2>{subject}</h2>\n{html}", encoding="utf-8")
    (OUTBOX / f"alert_{ts}.txt").write_text(f"{subject}\n\n{text_body}", encoding="utf-8")
    print(f"SMTP 未配置或发送失败（{detail}），已写入待发送：{f}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
