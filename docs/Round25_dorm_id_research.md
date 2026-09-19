# Round 25 — 宿舍 ID 调研

> Round 43 标记：已 deprecated。  
> Room ID 调研结论：roomId 是 UUID（不是 4 字符 hex）。  
> 当前代码 (`dorm_power._discover_room` + `_fetch_data`) 已正确处理。  
> 本报告作为 R25 决策记录保留，**新工作请直接读 `dorm_power.py:run_once` 的相关代码段**。

<!-- 旧内容 -->

# Round 25 — dorm ID 解析 & 动态测算可行性报告

## 结论先行

**前提修正**：`b3b8` **不是硬编码**。系统当前就在跑"自动发现"模式 —— `.env` 没有设 `DORM_ROOM_ID`，每个 scrape 由 `_discover_room()` 从 `finduser` GET 响应里动态解出 `<input type="hidden" id="roomId" value="b3b8">` 再 POST 给 5 个数据端点。

**可行性**：**已经实现**，无需新增任何代码。仅当 `finduser` 改版 / 限流时才用 `.env` 里的 `DORM_ROOM_ID` 兜底。
多 dorm 改造：需要按 `openid → roomId` 路由 + 隔离存储，是另一条路线图。

---

## 1. `b3b8` 当前来源

**不是硬编码**。流向如下（全部在 `dorm_power.py`）：

| 位置 | 行为 |
|---|---|
| `config.py:66` | `DORM_ROOM_ID: str = os.getenv("DORM_ROOM_ID", "")` — 留空 |
| `.env` | **只**有 `DORM_OPENID` + `DORM_BASE_URL`，**没有** `DORM_ROOM_ID` |
| `dorm_power.py:1267-1277` | `run_once()`：`if config.DORM_ROOM_ID` 空 → `_fetch_html` → `_discover_room(html)` → `room_id`（每 scrape 重抓一次） |
| `dorm_power.py:1283` | `db.set_meta("last_room_id", room_id)` — 缓存供 dashboard 读 |
| `dorm_power.py:1283`/各 `_fetch_*` | `roomId=<uuid>` 作为 **POST form body**（非 query string）发到 6 个端点 |
| `dorm_power.py:193-197` | `_safe_room_tail(room_id)` 把后 4 字符 `b3b8` 打进日志（脱敏） |

`b3b8` 是日志里的 `_safe_room_tail()` 输出，不是 URL 参数。任务描述里的 `?room_tail=b3b8` 是日志措辞，不是真实 wire format。

## 2. 解析路径（自动发现已上线）

```python
# dorm_power.py:208-225
def _discover_room(html: str) -> tuple[Optional[str], Optional[str]]:
    room_id = None
    for tag in _INPUT_TAG_RE.finditer(html):              # <input ...>
        attrs = _parse_input_attrs(tag.group(0))          # {type,id,value}
        if attrs.get("type") != "hidden":
            continue
        if attrs.get("id", "").lower() == "roomid":      # ★ 关键 id
            room_id = attrs.get("value")
    # room_label 走 _ROOM_NO_RE: <... id="roomNo">...</>
    return room_id, room_label
```

regex 锚点（`dorm_power.py:91-99`）：

- `_INPUT_TAG_RE = re.compile(r"<input\b[^>]*>")`
- `_INPUT_ATTR_RE = re.compile(r"""(?P<key>type|id|value)\s*=\s*["'](?P<val>[^"']*)["']""")`
- `_ROOM_NO_RE = re.compile(r"""<[^>]*\sid=["']roomNo["'][^>]*>(?P<val>[^<]+)</""")`

## 3. fetcher 函数清单

全部用 **POST form-urlencoded**，openid 仅走 query string（仅 `_fetch_html` 是 GET）：

| 函数 | 端点 | Method | Body 字段 | 备注 |
|---|---|---|---|---|
| `_fetch_html` L251 | `/dormEmRealRead/finduser` | GET | — | 拿 shell HTML，**只用于 discover roomId + EqPrice** |
| `_fetch_data` L277 | `/dormEmRealRead/getEmRealRead` | POST | `roomId` | 主数据 API，每 scrape 跑 |
| `_fetch_dormEmQuery` L532 | `/dormEmQuery/selectRecord` | POST | `roomId, stime, etime, page` | F1 回填 30 天 |
| `_fetch_dormEmDayElectQuery` L547 | `/dormEmDayElectQuery/getEmDayElectQuery` | POST | `roomId, stime, etime, page` | F2 |
| `_fetch_dormEmWgQuery` L562 | `/dormEmWgQuery/selectWgElect` | POST | `roomId, stime, etime, page` | F3 |
| `_fetch_dormEmRunStatus` L577 | `/dormEmRunStatus/getEmRunStatus` | POST | `roomId` | F4 |
| `_fetch_dormEmPayQuery` L595 | `/dormEmPayQuery/getEmPayQuery` | POST | `roomId, stime, etime, feeType` | F5 |
| `_fetch_eqprice` L616 | `/dormEmRealRead/finduser` | GET | — | 复用 `_fetch_html`，解 `EqPrice` hidden input |

**关键**：**所有数据端点都不返回 dorm 身份信息** —— 它们的返回都是 `{"status": "0", ...}` + 数据行。学校把 dorm 身份 **只**塞在 finduser shell 页里。

**端点清单勘误**：任务简报里列的 `/dormEmPay/pay`（缴费）在代码里 **不存在**。只有 `dormEmPayQuery/getEmPayQuery`（**读历史** F5）。支付端未实现。

## 4. finduser 响应字段（学校 schema 推断）

`_discover_room` 和 `_discover_eqprice` 用到的字段：

| 字段 | HTML 形态 | 用途 |
|---|---|---|
| `roomId` | `<input type="hidden" id="roomId" value="b3b8">` | 数据 API 凭证 |
| `roomNo` | `<... id="roomNo">6号楼-1-119</...>` | 人类可读标签（best-effort） |
| `EqPrice` | `<input type="hidden" id="EqPrice" value="0.533">` | 单价 |

真实响应没在仓库里 dump 过（`_dorm_zip_scratch` 里也没有 fixture HTML）。建议用户在浏览器抓一次 `finduser` shell 页源码确认。

## 5. 动态测算可行性 — **已实现**

代码位置：`dorm_power.py:1267-1277`。

```python
if config.DORM_ROOM_ID:
    room_id = config.DORM_ROOM_ID.strip()
else:
    html = _fetch_html(session, openid)
    room_id, room_label = _discover_room(html)
    if not room_id:
        raise RuntimeError(
            "could not find <input id=\"roomId\"> in the finduser page; "
            "set DORM_ROOM_ID in .env to override the discovery step."
        )
```

**所以当前生产路径就是"动态测算"**。每个 scrape 跑一次 GET → regex 解 hidden input → 5 个端点共用。无需任何新代码。

**若要进一步"完全去掉 finduser 额外请求"**：把解出的 `roomId` 缓存到 SQLite `meta`（已有 `last_room_id` L1283），下一次 scrape 直接复用直到失败。**未实现**，可作为 Round 26 候选。

## 6. 多 dorm 改造路线图（**不建议在当前 round 做**）

需要的不止是 "动态 dorm ID"：

- **按用户路由**：`openid → roomId` 映射表（现在 SQLite 单 dorm 单隐式 openid）
- **存储隔离**：`daily_elec` / `violations` / `pay_history` 已按 `roomId` 主键 → 天然支持多 dorm，**无需改 schema**
- **Feishu bot dispatch**：`feishu_bot.handle_event(body, room_id)` 当前 room_id 来自 `web.py:2130` 的 `db.get_meta("last_room_id")`，**全局单值**。多 dorm 必须改成按 sender_open_id 查映射
- **Dashboard**：当前所有路由读 `last_room_id`（`web.py:140`），多 dorm 需要按用户或 URL 参数分流

工程量 ~1–2 个 round。建议先做完 "缓存 last_room_id 跳过 GET" 再谈多 dorm。

## 7. 已验证 / 未验证

- **已验证**：`b3b8` 字面量在整个 `dorm-power-monitor/` 主目录（不含 `_dorm_zip_scratch/`） **不存在**，`.env` 不含 `DORM_ROOM_ID`。
- **未验证**：finduser 真实 HTML 形态（仓库无 fixture）— 用户可在浏览器 DevTools 抓一次确认 `<input id="roomId">` 仍存在；若学校改版需补 fallback regex。

---

# 调查详情（200 行）

## A. `b3b8` 字面量全局搜索

`grep` `b3b8` over `dorm-power-monitor/` (排除 `_dorm_zip_scratch/` 与 `__pycache__`)：**0 命中**。
`grep` `room_tail` over 全部项目：仅出现在 `_safe_room_tail()` 日志代码里（`dorm_power.py:193, 692, 1395, 1414`），非 URL 参数。
`grep` `DORM_ROOM_ID`：仅 `config.py:66` 读取 + `.env.example` 文档 + `dorm_power.py:1267` fallback 判断。

## B. `.env` 当前内容（仅键名）

```
DORM_OPENID=<值已 redact>
DORM_BASE_URL=http://ybhqcz.fjny.edu.cn
```

没有 `DORM_ROOM_ID`、没有 `DORM_PAY_*`。

## C. fetcher 函数完整签名（从源码）

```
_fetch_html(session, openid) -> str
_fetch_data(session, openid, room_id) -> dict  # status==0 校验
_post_form(session, openid, path, **fields) -> Any  # 共用 helper
_fetch_dormEmQuery(session, openid, room_id, days=30) -> list[dict]
_fetch_dormEmDayElectQuery(session, openid, room_id, days=30) -> list[dict]
_fetch_dormEmWgQuery(session, openid, room_id, days=30) -> list[dict]
_fetch_dormEmRunStatus(session, openid, room_id) -> dict
_fetch_dormEmPayQuery(session, openid, room_id, days=30, fee_type=-1) -> list[dict]
_fetch_eqprice(session, openid) -> Optional[float]
_parse_meta_ts(raw: Optional[str]) -> Optional[datetime]
_discover_room(html) -> (Optional[str], Optional[str])     # ★ roomId + roomNo
_discover_eqprice(html) -> Optional[float]                # ★ EqPrice
_parse_input_attrs(tag) -> dict[str, str]                  # 通用 input attr parser
_safe_room_tail(room_id) -> str                            # 日志脱敏后 4 字符
_safe_path(url) -> str                                     # 路径脱敏（不含 openid）
_build_url(path, openid) -> str                            # ★ openid 仅在此入 URL
_validate_openid() -> str                                  # 必填校验
```

`_fetch_*` 全部 POST form / Referer=finduser / `X-Requested-With: XMLHttpRequest`（`dorm_power.py:472-482`）。

## D. roomId 在系统里的传播链

```
finduser GET (openid)
    ↓ HTML
_discover_room(html)
    ↓ "b3b8"
db.set_meta("last_room_id", "b3b8")           # L1283
    ↓
_fetch_data POST roomId="b3b8"                # L293  (F1 主)
_fetch_dormEmQuery POST roomId="b3b8"         # L542  (F1-backfill)
_fetch_dormEmDayElectQuery POST roomId="b3b8" # L557  (F2)
_fetch_dormEmWgQuery POST roomId="b3b8"      # L572  (F3)
_fetch_dormEmRunStatus POST roomId="b3b8"     # L590  (F4)
_fetch_dormEmPayQuery POST roomId="b3b8"      # L611  (F5)
    ↓
db.record_daily_elec / record_violation / record_pay / upsert_run_status
    ↓
web.py:140 db.get_meta("last_room_id") → dashboard render
feishu_bot.handle_event(body, room_id)        # web.py:2130-2131
```

每张 SQLite 表的主键都包含 `roomId`（`db.py:48, 56, 65, 69`），所以**表 schema 已经是多 dorm ready**。缺的是"按 openid 分流"那一层。

## E. 风险与建议

| 风险 | 缓解 |
|---|---|
| finduser 改版 / 限流 | `DORM_ROOM_ID` 兜底（已存在） |
| discover 失败整个 scrape 挂 | L1273-1277 已 raise RuntimeError，让 cron 重试 |
| 多 dorm 用户 | 当前架构不支持，需要新一轮工程 |
| RoomId 在不同学校可能是楼栋+寝室 hash 还是 UUID | 看实测值；schema 无依赖 |

## F. 建议用户做的事（不写代码）

1. 浏览器打开 `finduser` 页 → DevTools → View Source → 确认 `<input id="roomId">` 还在
2. 若想"完全跳过 GET"：手工 `sqlite3 records.db "INSERT OR REPLACE INTO meta(key,value) VALUES('last_room_id','<值>');"`，然后改 `dorm_power.py` 的 discover 分支（**这是新代码，未做**）
3. 多 dorm 路线图 → 单独 round 立项

## G. 校验

- 0 文件改动（仅本报告新增）
- 0 python / sqlite 执行（仅读源码 + grep）
- openid / secret 未 echo
- scratch 目录未删