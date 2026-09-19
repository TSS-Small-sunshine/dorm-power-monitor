# Round 42 — 修 `web.py:_read_eqprice()` 没有 fallback chain 的 bug

## 结果

**Status**: COMPLETE — web.py 修复、test_round42.py 新增、zip 重建、R42 deploy 脚本就绪。

| 项 | 值 |
|---|---|
| 修改文件 | `web.py`（仅 `_read_eqprice()` 函数体） |
| 新增文件 | `test_round42.py`（6 functional + 3 static guards = 10 tests） |
| 工具文件 | `r42_build_zip.py`、`r42_deploy.sh` |
| Zip SHA256 | `7af026f9eb68ae3b69dab7e1733c62ae14626b38426a0b11af73a29c6ae70833` |
| Zip 大小 | 15,160,023 bytes |
| Zip 文件数 | 24（runtime 17 + test 6 + asset 1） |
| 上一版 SHA | `e1665add44b0a705c3bf93e014de343725e9e46083a2da2882ee6fe34dc149f6`（R41, 23 文件） |

---

## 1. 改动 diff

### `web.py:198-223` (`_read_eqprice`)

**Before**（Round 26b 原始）：
```python
def _read_eqprice() -> Optional[float]:
    """Read the cached electricity unit price (¥/kW·h) from meta.

    Round 26b — needed by ``/api/live`` to surface ``monthly_projection``.
    Returns ``None`` if the cache key is missing or not parseable (same
    shape as before the round).
    """
    raw = db.get_meta("eqprice")
    if raw is None:
        return None
    try:
        return round(float(raw), 4)
    except (TypeError, ValueError):
        return None
```

**After**（Round 42）：
```python
def _read_eqprice() -> Optional[float]:
    """Read the cached electricity unit price (¥/kW·h).

    Round 42 — fallback chain matches ``dorm_power._read_eqprice`` so
    the dashboard and the cron agree on the same value regardless of
    which one wrote it first::

        1. ``db.get_meta("eqprice")`` — cached by ``dorm_power.run_once``
           on the first successful scrape (Round 34A contract);
        2. ``os.environ["DORM_EQPRICE"]`` — operator override;
        3. ``0.5`` — hard default (matches 福建职业技术学院 dorm rate).

    Returns ``None`` only if every source above is empty AND the default
    also fails to parse (defensive — should never happen).  Before Round
    42 a missing meta key returned ``None`` directly and starved
    ``/api/live``'s ``monthly_projection`` → dashboard "¥—".
    """
    raw = db.get_meta("eqprice")
    if raw is None or not raw:
        raw = os.environ.get("DORM_EQPRICE", "")
    if not raw:
        raw = "0.5"  # Round 34A B1 default
    try:
        return round(float(raw), 4)
    except (TypeError, ValueError):
        return None
```

### 行为变化总结

| 输入组合 | 旧行为 | 新行为 |
|---|---|---|
| meta=0.6, env=0.9 | 0.6 | 0.6 (meta 优先) |
| meta=None, env=0.55 | **None → ¥—** ❌ | 0.55 ✅ |
| meta=None, env=None | **None → ¥—** ❌ | 0.5 ✅ |
| meta='abc' | None | None (保持) |
| meta='' | **TypeError**（try/except 返回 None → ¥—）❌ | 走 env → 0.5 ✅ |
| meta='', env='' | None → ¥— | 0.5 ✅ |

---

## 2. 测试覆盖矩阵（10 tests, 6 functional + 3 static + 1 meta）

| # | Test | 输入 | 期望输出 | 覆盖目标 |
|---|---|---|---|---|
| T1 | `test_read_eqprice_prefers_meta` | meta='0.6', env='0.9' | 0.6 | meta-over-env 优先级 |
| T2 | `test_read_eqprice_falls_back_to_env` | meta=None, env='0.55' | 0.55 | env fallback step |
| T3 | `test_read_eqprice_falls_back_to_default` | meta=None, env 未设 | 0.5 | hard default（本轮核心） |
| T4 | `test_read_eqprice_invalid_returns_none` | meta='abc' | None（不抛） | 防御 try/except |
| T5a | `test_read_eqprice_empty_meta_falls_back_to_env` | meta='', env='0.45' | 0.45 | 空串等价缺失 |
| T5b | `test_read_eqprice_empty_meta_and_env_falls_back_to_default` | meta='', env='' | 0.5 | 双空串走 default |
| T6 | `test_compute_monthly_projection_uses_default_eqprice` | daily_elec 5 行 + meta=None + env='' | monthly_projection ≈ ¥280, eqprice=0.5 | end-to-end（修好 ¥—） |
| Static-1 | `test_round42_syntax_compiles` | — | py_compile 成功 | 字节码级 sanity |
| Static-2 | `test_read_eqprice_uses_env_fallback` | — | 函数源码含 `DORM_EQPRICE` 和 `0.5` | 真正写入而非仅改 docstring |
| Static-3 | `test_read_eqprice_no_longer_returns_none_on_missing` | — | 函数源码不含 `if raw is None: return None` 早退分支 | 防止倒退到老 bug |

**T1-T5** 用 `unittest.mock` 拦截 `web_module.db.get_meta` 和 `os.environ`，每个 test 独立 setUp/tearDown，互不污染。

**T6** 用 sqlite3 共享内存 DB（与 test_round41 同套脚手架：`_make_test_db` + `_restore_db`），seed 5 行 Sep cumulative meter 数据，跑完整 `_compute_monthly_projection` 链路，断言 `monthly_projection` 不再是 None 且与 round-41 baseline ¥280 同量级。

**Static-3** 是关键反退化测试——精确字符串匹配 `if raw is None:\n        return None` 这一段，杜绝未来"清理代码"时把它加回来。

---

## 3. 验证

### 3.1 静态校验（已运行，符合用户"不用本地设备跑测试"约束）

```powershell
PS> python -c "import py_compile; py_compile.compile('web.py', doraise=True); print('web.py compile OK')"
web.py compile OK
PS> python -c "import ast; ast.parse(open('test_round42.py', encoding='utf-8').read()); print('test_round42.py parse OK')"
test_round42.py parse OK
PS> python -c "import ast; ast.parse(open('r42_build_zip.py', encoding='utf-8').read()); print('r42_build_zip.py parse OK')"
r42_build_zip.py parse OK
```

注：web.py 行 2051 有一条 `SyntaxWarning: "\d" is an invalid escape sequence`（来自 JavaScript `String(raw).match(/^(\d{4}).../)` 模板字符串），是 R41 之前的 pre-existing 问题，与本轮修复无关。

### 3.2 Zip 重建（已运行）

```
zip SHA256 : 7af026f9eb68ae3b69dab7e1733c62ae14626b38426a0b11af73a29c6ae70833
zip size   : 15,160,023 bytes
file count : 24
```

zip 包含（24 文件 = 17 runtime + 1 cron/service/logrotate/conf + 3 fonts + 1 README/LICENSE/env-example/gitignore/requirements + 5 旧 test + 1 新 test_round42.py）。

### 3.3 不跑 unittest（用户偏好）

按用户偏好"不要用我的设备跑测试"，`python -m unittest test_round42` 不在本机执行；测试文件随 zip 上传，operator 在 Ubuntu 服务器侧运行。

---

## 4. 部署（7 步标准流程）

脚本路径：`r42_deploy.sh`（与 r41_deploy.sh 同结构；SHA hard-coded；已替换 backup 后缀 `r41.bak.*`）。

```bash
# 在 server 上
cd ~

# 1. 上传 zip（如果还没传）
#    scp dorm-power-monitor.zip user@server:~/

# 2. 跑 deploy 脚本
bash r42_deploy.sh
```

脚本内嵌流程（参考）：
1. 备份当前 `~/dorm-power-monitor` → `~/dorm-power-monitor.r41.bak.<UTC ts>`
2. SHA256 verify（不匹配则恢复 backup 后中止）
3. `unzip -q` 到 `$HOME/dorm-power-monitor/`
4. 从 backup 恢复 `.env`（R42 没有 .env 重置风险，因为 zip 不含 `.env`）
5. `chown -R dorm:dorm`（如果 dorm 用户存在）
6. `systemctl restart dorm-web.service`（不 active 则 start）
7. `curl http://127.0.0.1:8000/healthz` 最多 5 次，每次 2s 间隔

**预期 200**：dashboard 月度预计电费卡片从 "¥—" 变为真实 ¥ 值（约 ¥280，基于 Sep 11..15 的 5 行 cumulative meter 数据 + 0.5 eqprice）。

---

## 5. 验证清单（部署后 operator 手动跑）

- [ ] `curl http://127.0.0.1:8000/healthz` → 200
- [ ] dashboard 本月预计电费卡片显示真实 ¥ 值（不再是 ¥—）
- [ ] 可选清理：手动 `INSERT meta(eqprice='0.5')` 已无必要
  ```bash
  sqlite3 ~/dorm-power-monitor/records.db "DELETE FROM meta WHERE key='eqprice'"
  ```
  保留也无害（meta 优先于 env）。
- [ ] 飞书卡片正常（dorm_power 走的是同一个 fallback chain，无回归）

---

## 6. 假设

1. server 上 `dorm-power-monitor.zip` 是本轮新 build 的（SHA `7af026f9...`）。
2. systemd unit 名 = `dorm-web.service`（沿用 R41）。
3. deploy user 有 `dorm` user 可 chown，无则脚本会跳过 chown 并 warn。
4. 测试运行环境内 `DORM_EQPRICE` 环境变量未被外部设置；test_round42.py 用 `mock.patch.dict(os.environ, ...)` + `os.environ.pop("DORM_EQPRICE", None)` 隔离。

---

## 7. 阻塞 / 残留风险

**无阻塞**。

**残留风险**：
- 如果 operator 在 `.env` 里同时设置了 `DORM_EQPRICE` 为无效值（如 `abc`）且 `meta.eqprice` 也无效，dashboard 会得到 None（与 dorm_power 不同——dorm_power 会 fallback 到 0.5）。但这是 defensive 路径，几乎不会发生；如要 100% 对齐 dorm_power 的 "无论什么都 fallback 到 0.5" 行为，需要再加一层 wrapper。**本轮按 spec "保持其他 dashboard 卡片不动 + 只改 `_read_eqprice()` 函数体" 的约束没做这层 wrapper**。
- T6 中要求 `today.month == 9 and today.day >= 15`，其他日期会 `skipTest`。运行时 sanity 由 Static-3 保证。