# 用户评价观测平台（本地原型 M0/M1/M2）

## 页面

| 路径 | 内容 |
| --- | --- |
| `/` | 监控大盘：指标卡、月度趋势（含发版竖线）、日趋势、设备/市场风险、版本健康矩阵、主题 |
| `/analysis` | 分析工作台：多维筛选下钻到原文 |
| `/alerts` | 告警中心：规则清单、有效性回放、告警事件流、立即评估 |
| `/admin` | 数据管理：同步日志、来源分布、数据质量、月度缺口、发版记录 |

## 启动

```bash
cd platform
python -m uvicorn app:app --host 127.0.0.1 --port 8766
```

浏览器打开 http://127.0.0.1:8766

前端资源**全部本地化**（ECharts 位于 `static/vendor/`），不依赖外网 CDN，
内网或离线环境也能正常渲染图表。

首次运行请先按下方「安全配置」生成 `.env`：否则会话密钥每次启动都会重新生成，
导致登录态在重启后失效。

解释器建议用已装依赖的托管 venv：

```text
C:\Users\puhua.kuang\.workbuddy\binaries\python\envs\default\Scripts\python.exe
```

## 目录

```text
platform/
├── app.py          FastAPI 路由（页面 + API）
├── config.py       配置（DB 连接串、包名、告警阈值）
├── models.py       SQLAlchemy 模型（Review / SyncLog / DailyMetric / TopicDaily / AlertRule / AlertEvent）
├── analytics.py    指标计算与查询（SQLAlchemy engine）
├── alerts.py       告警规则引擎 + 生命周期操作（SQLAlchemy engine）
├── notify.py       告警邮件通知（SQLAlchemy engine）
├── templates/      Jinja2 页面（base / dashboard / analysis / admin）
└── static/style.css
```

## 页面

- `/` 监控大盘：核心指标卡、月度差评率趋势、近 60 天日趋势、设备/市场风险 TOP、版本健康矩阵、主题分布
- `/analysis` 分析工作台：按星级/版本/设备/语言/关键词下钻到原文
- `/admin` 数据管理：同步日志、来源分布、数据质量、月度完整性与缺口

## 切换数据库

默认直连本地 SQLite（`work/reviews.db`）。切 Postgres：

```bash
export REVIEW_DB_URL="postgresql+psycopg2://user:pw@host:5432/dbname"
```

`analytics.py`/`alerts.py`/`notify.py` 查询层已全部迁移到 SQLAlchemy `engine`
（见「查询层：SQLAlchemy」一节）。切 Postgres 时主要工作量在 SQL 方言差异，
如 `substr(col,1,n)` 需换成 `substring(col from 1 for n)`，`date('now','-N days')`
换成 Postgres 的 `current_date - interval 'N days'`。模型层已是 SQLAlchemy，结构可直接建表。

## 口径约定（重要）

- 所有比例指标（差评率、均分、版本健康度）只走**全量口径**（CSV / GCS 批量报告）。
- Reviews API 返回的是**有正文评论的近完整采样**，其差评率约为全量的 1.76 倍，**不得用于比例指标**。
- 有正文评论的差评率在大盘上单独展示并标注，避免与全量口径混淆。
- 残缺月（覆盖天数不足或当月）不参与环比，页面上标记为「残缺」。
- 报告有 3–7 天写入延迟，趋势尾部若骤降属正常，平台会自动检测并提示「仍在写入」。

## 数据更新

**推荐用每日同步脚本**（自动选通道：GCS 优先，不可用则降级 API）：

```bash
cd platform
python sync_daily.py            # 执行同步
python sync_daily.py --dry-run  # 只探测通道，不写库
```

脚本会记录同步前后条数到 `work/sync_history.jsonl`。已配置为每日 09:00 自动执行（automation「每日评论增量同步」）。

通道选择逻辑：

1. 凭据优先用人类账号 OAuth token（`~/.workbuddy/secrets/play_report_oauth.json`），其次服务账号 JSON
2. 探测 GCS 桶 `pubsite_prod_rev_16693200349579533994` 是否可读，可读则拉取当月与上月报告
3. 不可读则降级到 Reviews API 增量

手动导入（补历史用）：

```bash
python <skill>/scripts/run_all.py --csv-dir <目录> --package com.tcl.browser --db ../work/reviews.db
```

## 安全配置（部署前必读）

### 登录口令

平台默认开启鉴权。在 `platform/.env` 配置（该文件已在 `.gitignore` 中）：

```bash
SECRET_KEY=<随机长字符串>        # 会话签名密钥，务必更换
PLATFORM_USER=admin
PLATFORM_PASSWORD=<登录口令>
# 可选：用口令哈希代替明文，优先级更高
# PLATFORM_PWHASH=sha256$<salt>$<hash>
```

生成口令哈希：

```bash
python -c "import auth; print(auth.hash_password('你的口令'))"
```

未登录时：页面跳转 `/login`，API 返回 401；`/api/health` 与静态资源放行。
本地开发可设 `AUTH_ENABLED=0` 关闭鉴权。

### 已修复：SQL 注入

`analytics.py` 与 `alerts.py` 的查询已全部改为绑定参数。此前 `device`/`version`/`lang`/`since`
等入参直接拼接进 SQL，可被构造 `' OR '1'='1` 绕过条件（已实测可利用）。

## 查询层：SQLAlchemy

`analytics.py`/`alerts.py`/`notify.py` 已从裸 `sqlite3` + `?` 占位符迁移到
SQLAlchemy `engine`（`models.py` 中定义）+ `text()` 命名参数（`:name`）。

**为什么用命名参数而不是位置参数**：不同数据库驱动的位置参数占位符风格不同
（SQLite 用 `?`，Postgres 用 `%s`），命名参数经 SQLAlchemy 方言层统一转换后
才具备跨库可移植性，为将来切 Postgres 留好口子。

**SQLAlchemy `Row` 对象的两个坑**（迁移时踩过，记录下来避免重复踩）：

1. `Row` 支持属性访问（`row.a`）和元组解包（`a, b = row`），但**不支持下标访问**
   （`row["a"]` 会报 `TypeError`）。需要 dict 风格访问时用 `.execute(...).mappings()`
   而不是普通 `.execute(...)`。
2. `fetchall()` 返回的 `Row` 列表如果直接塞进 FastAPI 返回的 dict/list 里，
   `jsonable_encoder` 不知道怎么序列化会报 500（`ValueError: dictionary update
   sequence element ... TypeError: vars() argument must have __dict__ attribute`）。
   凡是要直接作为 API 响应体返回的行数据，必须先转成 `tuple(row)` 或
   `dict(zip(keys, row))`，不能把 `Row` 对象原样放进返回值。

## 预聚合表：daily_metric / topic_daily

`analytics.py` 新增 `refresh_daily_metric(days=400)` / `refresh_topic_daily(days=400)`，
全量重算最近 N 天写入（`delete + insert`，幂等，不做增量 diff——当前数据量级
全表聚合耗时百毫秒到几秒级，没必要引入增量判断复杂度）：

- `daily_metric`：`stat_date × dim_type(all/version/device/lang) × dim_value`，
  字段 `total`/`avg_rating`/`negative`/`negative_rate`。
- `topic_daily`：`stat_date × topic`，字段 `mentions`/`negative_mentions`/`negative_ratio`，
  主题词典**动态 import** `app-review-insight` 技能脚本 `analyze.py` 里的 `TOPIC_RE`
  （而不是把词典复制一份到平台目录），避免主题口径出现两份定义漂移。

现有 API/查询函数**不读**这两张表，写入是为将来数据量上百万级或做跨天/跨维度
历史趋势对比时提前铺好预聚合层，目前不影响任何现有行为。

已挂载到 `sync_daily.py`：每次同步成功后自动调用 `refresh_aggregates()`
重算这两张表；刷新失败只打印警告，不影响本次同步的返回值。

**已发现并修复的历史问题**：`alembic_version` 曾显示已到 head（`0002`），
但这两张表实际不存在于数据库文件里——说明 `0001` 迁移当年执行时这两张表
创建失败但版本号被错误标记为已应用。用
`Base.metadata.create_all(engine, tables=[DailyMetric.__table__, TopicDaily.__table__])`
补建后写入验证正常，`0001` 迁移脚本本身的 `create_table` 定义是正确的，
在全新环境（含未来的 Postgres）上正常跑迁移不会重现这个问题。

## 评论中文翻译（异步 + 持久化缓存）

- **首屏不等待翻译**：`GET /api/reviews` 只返回评论原文；译文由前端在列表渲染完成后
  异步请求 `POST /api/reviews/translations`（每次最多 10 条）回填，翻译耗时不再阻塞页面加载。
- **默认只翻译前 10 条**，页面提供「翻译本页全部」按钮按需翻译整页（每批 10 条顺序请求）。
- **译文持久化缓存**：新表 `review_translation`（迁移 `0003`），以原文 SHA-256 为主键，
  进程重启或多 worker 复用同一份缓存，不重复调用翻译服务。
- **降级**：未配置翻译后端或调用失败时只显示原文并给出提示，不影响检索与导出。
- 配置（写在 `.env`，未提供时自动跳过翻译）：

```
OPENAI_API_KEY=...
OPENAI_BASE_URL=...
OPENAI_TRANSLATE_MODEL=gpt-3.5-turbo
```

## 数据库迁移

用 Alembic 管理平台自建表：`daily_metric` / `topic_daily` / `release` / `alert_rule` / `alert_event`。

`reviews` 与 `sync_log` 由 app-review-insight 技能脚本创建维护，**不纳入迁移**（env.py 中过滤）。

```bash
cd platform
python -m alembic current                             # 查看版本
python -m alembic revision --autogenerate -m "说明"    # 生成新迁移
python -m alembic upgrade head                        # 应用
```

现有库（表已由脚本建好）执行 `python -m alembic stamp 0001` 标记为已应用。

注意：`alembic.ini` 必须保持 ASCII——中文 Windows 下 Alembic 以 GBK 读取，含中文会因解码失败报错。

## 日志

`logging_setup.py` 统一配置：控制台 + `../work/logs/platform.log`（5MB 轮转，保留 3 份）。
级别由 `LOG_LEVEL` 环境变量控制。登录失败等关键事件已埋点。

## 发版记录

`release` 表存发版记录。初始化与增量更新：

```bash
cd platform
python init_releases.py          # 建表 + 从数据推导写入
python init_releases.py --show   # 查看
```

发布日期取自版本名中的构建日期（如 `8.20.030_38303fc_260904_gp` → `260904` → 2026-09-04），
比"该版本首条评论日期"可靠——后者会因少量脏数据而远早于真实发布日。

用途：趋势图上的发版竖线、版本健康矩阵的发布日列、后续发版后 72h 专项巡检。

## 告警

规则引擎在 `alerts.py`，7 条规则（R1–R7），全部带样本量下限，不使用单日指标。

```bash
cd platform
python alerts.py                      # 评估当前并落库
python alerts.py --as-of 2025-03-20   # 历史回放（验证规则有效性，不落库）
python alerts.py --no-persist         # 只评估不写库
```

滚动窗口取**近 7 个完整日**，自动剔除"报告仍在写入"的尾部（中位数 50% 以下的最近日期），
避免把数据延迟误判为评论量暴跌。

**有效性已用历史数据验证**：

| 回放日期 | 场景 | 结果 |
| --- | --- | --- |
| 2025-03-20 | 事故期 | R1 触发，16.59% > 阈值 11.79% |
| 2025-04-15 | 事故高峰 | R1 触发，19.59% > 阈值 16.76% |
| 2025-06-15 | 修复后 | 未触发 |
| 2026-05-20 | 2026-05 跳升 | R1 触发，7.60% > 阈值 6.29% |
| 2026-08-15 | 健康期 | 未触发 |

基线取过去 90 天动态计算，阈值 = 基线 + 2σ，会随数据自动漂移。

告警评估已纳入每日 09:00 自动化流程。

### 告警闭环：ack / 指派 / 静默 / 解决 + 去重

`alert_event` 表新增生命周期字段（迁移 `0002_alert_event_lifecycle`）：
`status`（open/acked/muted/resolved）、`occurrences`、`last_seen_at`、
`acked_by`/`acked_at`、`assignee`、`mute_until`、`notified_at`、`resolved_at`。

`evaluate()` 不再每次评估都插新行，而是按 **code+subject** 去重合并：

- 同一问题持续触发：更新同一行（`occurrences+1`、刷新 `last_seen_at`/`evidence`），不产生新记录
- 问题不再触发：既有的 open/acked/muted 行自动置为 `resolved`
- 静默到期（`mute_until` 已过）后若问题仍在触发：状态回到 `open`，且清空 `notified_at`（重新提醒）

告警中心页面（`/alerts`）按状态分 Tab（进行中/已确认/已静默/已解决），每行可操作：

| 操作 | API | 说明 |
| --- | --- | --- |
| 确认 | `POST /api/alerts/{id}/ack` | 记录确认人（当前登录用户），状态转 acked |
| 指派 | `POST /api/alerts/{id}/assign` | body `{"assignee": "姓名"}` |
| 静默 N 天 | `POST /api/alerts/{id}/mute` | body `{"days": 7}`，默认 7 天，上限 90 天 |
| 标记解决 | `POST /api/alerts/{id}/resolve` | 人工关闭，不等规则自动判定 |

`GET /api/alerts?status=xxx` 不传 `status` 时默认排除 `resolved`（避免历史噪音淹没当前问题）。

**邮件去重**：`notify.py` 改为按 `notified_at` 是否为空判断是否发送过，
不再用时间游标。同一问题只通知一次；静默到期重新浮现才会再通知一次。

```bash
cd platform
python notify.py             # 发送尚未通知过的 open/acked 告警
python notify.py --dry-run   # 只渲染不发送，不标记 notified_at
python notify.py --all       # 忽略 notified_at，重发最近 20 条（人工排查用）
```

收件人配置 `ALERT_TO`（`.env` 或环境变量），SMTP 未配置时落 `work/outbox/`。

## 首页一句话结论

`GET /api/summary` 返回近 7 日（可调 `window_days`）差评率的一句话健康结论，
含较上周 / 较上月环比、90 日基线阈值、近半年分位数。口径与告警 R1 完全一致
（近 7 个完整日滚动，自动剔除报告延迟尾部），避免大盘与告警"各说各话"。

大盘顶部结论卡三种状态：

- `ok`（健康）：绿色左边框，正常区间
- `watch`（留意）：环比上升超 2pp 但未破基线阈值
- `warn`（需关注）：超出 90 日基线 + 2σ 阈值

## 分析工作台：导出与筛选分享

- **筛选条件同步进 URL**：星级/版本/设备/语言/起始日期/关键词/页码全部写入 query string
  （`history.replaceState`，不产生跳转），刷新页面或复制链接分享给同事都不会丢失筛选状态。
  「复制筛选链接」按钮直接拷贝当前筛选对应的 URL。
- **导出 CSV**：`GET /api/reviews/export`，筛选口径与页面检索完全一致（复用 `_search_where`），
  单次导出上限 20000 条（按最新时间排序取前部，超限会在前端提示并需二次确认）。
  CSV 已做**公式注入防护**（`=`/`+`/`-`/`@` 开头的字段加前导 `'`）和标准转义（引号/逗号/换行），
  文件带 UTF-8 BOM，Excel 直接打开不乱码。
