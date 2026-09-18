"""用户评价观测平台 · FastAPI 入口（M0/M1/M2 原型）。

本地原型默认直连 SQLite（analytics 层用 sqlite3），
模型层已用 SQLAlchemy 定义好，切 Postgres 时只需改 REVIEW_DB_URL 并迁移数据。
"""
from __future__ import annotations

import os

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import alerts
import analytics
import config
import translate

config.load_env()  # 必须在 import auth 之前：auth 加载时会读取 SECRET_KEY
import auth  # noqa: E402
from config import PACKAGE, WORKDIR  # noqa: E402
from logging_setup import setup_logging  # noqa: E402

log = setup_logging()

app = FastAPI(title="用户评价观测平台", version="0.1.0")
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

PUBLIC_PATHS = {"/login", "/api/login", "/api/health"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """未登录时：页面跳登录页，API 返回 401。"""
    if not auth.AUTH_ENABLED:
        return await call_next(request)
    path = request.url.path
    if path.startswith("/static") or path in PUBLIC_PATHS:
        return await call_next(request)
    if auth.verify_token(request.cookies.get(auth.COOKIE)):
        return await call_next(request)
    if path.startswith("/api"):
        return JSONResponse({"detail": "未登录或会话已过期"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        "login.html", {"request": request, "package": PACKAGE}
    )


@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    if username != auth.USER or not auth.verify_password(
        password, os.environ.get("PLATFORM_PWHASH")
    ):
        log.warning("登录失败：用户名 %s（来源 %s）", username, request.client.host if request.client else "-")
        return JSONResponse({"detail": "用户名或口令不正确"}, status_code=401)
    log.info("用户登录成功：%s", username)
    resp = JSONResponse({"ok": True, "user": username})
    resp.set_cookie(
        auth.COOKIE, auth.create_token(username),
        httponly=True, samesite="lax", max_age=auth.TOKEN_TTL,
    )
    return resp


@app.post("/api/logout")
def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "package": PACKAGE, "page": "dashboard"},
    )


@app.get("/analysis", response_class=HTMLResponse)
def analysis_page(request: Request):
    return templates.TemplateResponse(
        "analysis.html",
        {"request": request, "package": PACKAGE, "page": "analysis"},
    )


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    return templates.TemplateResponse(
        "admin.html",
        {"request": request, "package": PACKAGE, "page": "admin"},
    )


@app.get("/api/overview")
def api_overview():
    return analytics.overview()


@app.get("/api/summary")
def api_summary(window_days: int = Query(7, ge=3, le=30)):
    return analytics.summary(window_days=window_days)


@app.get("/api/monthly")
def api_monthly():
    return analytics.monthly_trend()


@app.get("/api/daily")
def api_daily(days: int = Query(60, ge=7, le=365)):
    return analytics.daily_trend(days)


@app.get("/api/dim/{dim}")
def api_dim(dim: str, min_n: int = 100, since: str | None = None):
    return analytics.dimension_risk(dim, min_n=min_n, since=since)


@app.get("/api/versions")
def api_versions(limit: int = 15):
    return analytics.version_health(limit)


@app.get("/api/topics")
def api_topics():
    return analytics.topics()


@app.get("/api/freshness")
def api_freshness():
    return analytics.data_freshness()


@app.get("/api/sync")
def api_sync(limit: int = 20):
    return analytics.sync_log(limit)


@app.get("/api/releases")
def api_releases(limit: int = 40):
    return analytics.releases(limit)


@app.get("/api/reviews")
def api_reviews(
    star_max: int | None = None,
    version: str | None = None,
    device: str | None = None,
    lang: str | None = None,
    keyword: str | None = None,
    since: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    items, total = analytics.search_reviews(
        star_max=star_max,
        version=version,
        device=device,
        lang=lang,
        keyword=keyword,
        since=since,
        limit=limit,
        offset=offset,
    )
    return {"total": total, "items": items}


@app.post("/api/reviews/translations")
async def api_review_translations(request: Request):
    """异步翻译当前页评论，最多接收 10 条，避免阻塞评论首屏。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求格式不正确"}, status_code=400)
    raw_items = body.get("items", []) if isinstance(body, dict) else []
    if not isinstance(raw_items, list):
        return JSONResponse({"detail": "items 必须是数组"}, status_code=400)
    items = [item for item in raw_items[:10] if isinstance(item, dict)]
    zh_map = translate.translate_batch(items, batch_size=10)
    translations = [
        {"index": index, "text_zh": zh_map.get((item.get("text") or "").strip(), "")}
        for index, item in enumerate(items)
    ]
    return {"translations": translations}



def _csv_escape(v) -> str:
    """转义为 CSV 字段：防公式注入（Excel/Sheets 打开时 =/+/-/@ 开头会被当公式执行），
    并处理引号、逗号、换行。"""
    s = "" if v is None else str(v)
    if s and s[0] in ("=", "+", "-", "@"):
        s = "'" + s
    if any(ch in s for ch in (",", '"', "\n", "\r")):
        s = '"' + s.replace('"', '""') + '"'
    return s


@app.get("/api/reviews/export")
def api_reviews_export(
    star_max: int | None = None,
    version: str | None = None,
    device: str | None = None,
    lang: str | None = None,
    keyword: str | None = None,
    since: str | None = None,
):
    items, total, truncated = analytics.export_reviews(
        star_max=star_max, version=version, device=device,
        lang=lang, keyword=keyword, since=since,
    )
    header = ["date", "star", "lang", "device", "android_version", "version", "source", "text"]
    lines = [",".join(header)]
    for r in items:
        lines.append(",".join(_csv_escape(r.get(k)) for k in header))
    csv_body = "\r\n".join(lines) + "\r\n"
    if truncated:
        log.info(
            "导出被截断：命中 %d 条，仅导出前 %d 条",
            total, analytics.EXPORT_MAX_ROWS,
        )
    resp = Response(content="\ufeff" + csv_body, media_type="text/csv; charset=utf-8")
    resp.headers["Content-Disposition"] = 'attachment; filename="reviews_export.csv"'
    resp.headers["X-Export-Total"] = str(total)
    resp.headers["X-Export-Truncated"] = "1" if truncated else "0"
    return resp


@app.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request):
    return templates.TemplateResponse(
        "alerts.html",
        {"request": request, "package": PACKAGE, "page": "alerts"},
    )


@app.get("/api/health")
def api_health():
    return {"status": "ok", "package": PACKAGE, "workdir": str(WORKDIR)}


@app.get("/api/alerts")
def api_alerts(limit: int = 60, status: str | None = None):
    return alerts.recent(limit, status=status)


def _current_user(request: Request) -> str:
    return auth.verify_token(request.cookies.get(auth.COOKIE)) or "unknown"


@app.post("/api/alerts/{event_id}/ack")
def api_alert_ack(event_id: int, request: Request):
    ok = alerts.ack(event_id, _current_user(request))
    if not ok:
        return JSONResponse({"detail": "事件不存在或状态不允许确认"}, status_code=409)
    return {"ok": True}


@app.post("/api/alerts/{event_id}/assign")
async def api_alert_assign(event_id: int, request: Request):
    body = await request.json()
    assignee = str(body.get("assignee", "")).strip()
    if not assignee:
        return JSONResponse({"detail": "指派人不能为空"}, status_code=400)
    ok = alerts.assign(event_id, assignee)
    if not ok:
        return JSONResponse({"detail": "事件不存在"}, status_code=404)
    return {"ok": True}


@app.post("/api/alerts/{event_id}/mute")
async def api_alert_mute(event_id: int, request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    days = int(body.get("days", 7)) if body else 7
    days = max(1, min(days, 90))
    ok = alerts.mute(event_id, days=days)
    if not ok:
        return JSONResponse({"detail": "事件不存在或状态不允许静默"}, status_code=409)
    return {"ok": True, "days": days}


@app.post("/api/alerts/{event_id}/resolve")
def api_alert_resolve(event_id: int):
    ok = alerts.resolve(event_id)
    if not ok:
        return JSONResponse({"detail": "事件不存在或已解决"}, status_code=409)
    return {"ok": True}


@app.get("/api/alerts/rules")
def api_alert_rules():
    return alerts.rule_catalog()


@app.post("/api/alerts/run")
def api_alerts_run():
    ev = alerts.evaluate()
    return {"triggered": len(ev), "events": ev}


@app.get("/api/alerts/backtest")
def api_alerts_backtest():
    """用历史日期回放，验证规则有效性（不落库）。"""
    points = [
        ("2025-03-20", "2025 事故期"),
        ("2025-04-15", "事故高峰"),
        ("2025-06-15", "修复后"),
        ("2026-05-20", "2026-05 跳升"),
        ("2026-08-15", "健康期"),
    ]
    out = []
    for d, label in points:
        ev = alerts.evaluate(d, persist=False)
        out.append({"date": d, "label": label, "triggered": len(ev), "events": ev})
    return out
