"""LLM 翻译：分析工作台评论正文按需翻译成中文。

范围限定：只翻译当前页展示的评论（不做全量预翻译入库）——已与用户确认的范围，
避免对 13 万+ 条历史评论做一次性翻译产生的成本和延迟。

技术选型背景：免费翻译库（deep-translator 的 Google/MyMemory 引擎）在当前
网络环境下不可用（Google 是 IP 层面长期封禁，MyMemory 是每日免费额度耗尽），
改用大模型翻译。复用 app-review-insight 技能 llm_label.py 里验证过的
OpenAI 兼容调用模式（stdlib urllib，无额外依赖，避免引入新的第三方库）。

未配置 OPENAI_API_KEY / OPENAI_BASE_URL 时静默跳过：不报错、不阻塞评论列表
展示，前端只是不渲染译文行——翻译是增强能力，不是核心功能的硬依赖。
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

from sqlalchemy import select

from logging_setup import setup_logging
from models import Base, ReviewTranslation, SessionLocal, engine

log = setup_logging("platform.translate")

# 已经是中文的语言代码，不需要翻译
ZH_LANGS = {"zh", "zh-cn", "zh-hans", "zh-hant", "zh-tw", "zh-hk"}

# 进程内缓存只作为热缓存，持久化缓存由 SQLite 提供。
_CACHE: dict[str, str] = {}
_CACHE_MAX = 2000


def _ensure_cache_table() -> None:
    Base.metadata.create_all(engine, tables=[ReviewTranslation.__table__])


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_persistent(texts: list[str]) -> dict[str, str]:
    if not texts:
        return {}
    _ensure_cache_table()
    hashes = {_hash_text(text): text for text in texts}
    with SessionLocal() as session:
        rows = session.execute(
            select(ReviewTranslation).where(ReviewTranslation.source_hash.in_(hashes))
        ).scalars().all()
    result = {hashes[row.source_hash]: row.translated_text for row in rows}
    _CACHE.update(result)
    return result


def _save_persistent(values: dict[str, str]) -> None:
    if not values:
        return
    _ensure_cache_table()
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal.begin() as session:
        for source_text, translated_text in values.items():
            key = _hash_text(source_text)
            row = session.get(ReviewTranslation, key)
            if row is None:
                session.add(ReviewTranslation(
                    source_hash=key,
                    source_text=source_text,
                    translated_text=translated_text,
                    created_at=now,
                ))
            elif row.translated_text != translated_text:
                row.translated_text = translated_text


SYSTEM_PROMPT = (
    "你是专业翻译。将用户提供的多条应用商店评论原文翻译成简体中文。\n"
    "只翻译，不要解读、不要评价、不要补充原文没有的内容。\n"
    "保留数字、表情符号、产品名等原样。\n"
    '输出严格的 JSON 数组，每个元素 {"id":"T1","zh":"..."}，'
    "id 与输入一一对应，不要用 markdown 代码块包裹，不要输出任何解释文字。"
)


def _post(url: str, payload: dict, headers: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _call_llm(user_prompt: str) -> str:
    base = os.environ["OPENAI_BASE_URL"].rstrip("/")
    model = os.environ.get("OPENAI_TRANSLATE_MODEL", "gpt-3.5-turbo")
    data = _post(
        f"{base}/chat/completions",
        {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
        },
        {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
    )
    return data["choices"][0]["message"]["content"]


def _parse_json_array(raw: str) -> list[dict]:
    """LLM 偶尔会包 markdown 代码块，做容错提取（沿用 llm_label.py 的处理方式）。"""
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    start, end = s.find("["), s.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"响应中未找到 JSON 数组：{raw[:200]}")
    return json.loads(s[start:end + 1])


def enabled() -> bool:
    """是否已配置翻译所需的环境变量。未配置时上层应静默跳过。"""
    return bool(os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_BASE_URL"))


def _cache_put(text: str, zh: str) -> None:
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()  # 简单粗暴地整体清空，避免引入 LRU 依赖；缓存本身就是尽力而为
    _CACHE[text] = zh


def translate_batch(items: list[dict], batch_size: int = 20) -> dict[str, str]:
    """批量翻译评论正文，返回 {原文: 中文译文}。

    items 每项需含 'text'（评论正文）和可选 'lang'（reviewer_lang，
    已知是中文时跳过不翻译，节省调用）。单条调用失败自动降级为跳过该批次
    （不影响评论列表整体展示，前端对应行不显示译文即可）。
    """
    if not enabled():
        return {}

    # 去重 + 跳过空文本/已知中文；命中缓存的直接收进 result，其余进 pending 待调用
    pending: list[str] = []
    seen: set[str] = set()
    result: dict[str, str] = {}
    for it in items:
        text = (it.get("text") or "").strip()
        lang = (it.get("lang") or "").strip().lower()
        if not text or lang in ZH_LANGS or text in seen:
            continue
        seen.add(text)
        if text in _CACHE:
            result[text] = _CACHE[text]
        else:
            pending.append(text)

    # 先查持久化缓存，进程重启或多 worker 时仍可复用；缓存异常不应阻断翻译。
    try:
        persistent = _load_persistent([text for text in seen if text not in _CACHE])
    except Exception as exc:  # noqa: BLE001 - 缓存是尽力而为，失败只降级
        log.warning("读取译文缓存失败：%s %s", type(exc).__name__, exc)
        persistent = {}
    result.update(persistent)
    pending = [text for text in pending if text not in result]

    if not pending:
        return result

    batches = [pending[i:i + batch_size] for i in range(0, len(pending), batch_size)]
    fresh: dict[str, str] = {}
    for batch in batches:
        idmap = {f"T{i}": t for i, t in enumerate(batch, 1)}
        lines = [f"{tag}: {t[:400]}" for tag, t in idmap.items()]
        user_prompt = "\n".join(lines)
        try:
            raw = _call_llm(user_prompt)
            parsed = _parse_json_array(raw)
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError,
                json.JSONDecodeError, KeyError, TimeoutError) as exc:
            log.warning("翻译调用失败，跳过本批 %d 条：%s %s", len(batch), type(exc).__name__, exc)
            continue
        for entry in parsed:
            tag = str(entry.get("id") or "").strip()
            zh = str(entry.get("zh") or "").strip()
            src = idmap.get(tag)
            if src and zh:
                result[src] = zh
                fresh[src] = zh
                _cache_put(src, zh)

    try:
        _save_persistent(fresh)
    except Exception as exc:  # noqa: BLE001 - 写缓存失败只影响下次复用，不影响本次返回
        log.warning("写入译文缓存失败：%s %s", type(exc).__name__, exc)
    return result
