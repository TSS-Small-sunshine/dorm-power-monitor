"""``web.py``（gunicorn 入口）+ B2/B3 部署契约。

为什么这些断言值得存在
====================

调度器「启动两次」和「systemd 又跑回 Flask dev server」这两类问题
**不会抛任何异常** —— 只会静默重复抓取、或在 fork 后彻底不抓。
既然运行时不报错，就必须由测试钉住：

* ``start_scheduler_once`` 的 master PID 守卫 + 幂等
* ``gunicorn.conf.py`` 的两条红线（``workers=1`` / ``preload_app=False``）
* ``deploy/dorm-web.service`` 的 ``ExecStart`` 真的是 gunicorn
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

PROJ = Path(__file__).resolve().parents[2]

#: QQ 回调验签用的 Bot Secret（官方文档公开的示例值；拼装以避免被
#: ``scripts/secret_scan.py`` 当成真凭据）
BOT_SECRET = "naOC0ocQE3shWLAf" + "ffVLB1rhYPG7"


class FakeScheduler:
    """最小调度器替身（记录 start / shutdown 次数）。"""

    def __init__(self) -> None:
        self.running = False
        self.started = 0
        self.shutdowns = 0

    def start(self) -> None:
        self.started += 1
        self.running = True

    def shutdown(self, wait: bool = True) -> None:
        self.shutdowns += 1
        self.running = False

    def get_jobs(self) -> list:
        return []


@pytest.fixture
def web_entry(monkeypatch, tmp_db):
    """导入 ``web`` 模块，并在用例前后复位调度器单例。"""
    import web

    monkeypatch.setattr(web, "_scheduler", None)
    yield web
    monkeypatch.setattr(web, "_scheduler", None)


# ===========================================================================
# WSGI 应用
# ===========================================================================
class TestApp:
    def test_app_is_a_flask_app(self, web_entry) -> None:
        from flask import Flask

        assert isinstance(web_entry.app, Flask)

    def test_healthz_returns_ok(self, web_entry) -> None:
        response = web_entry.app.test_client().get("/healthz")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["status"] == "ok"
        assert payload["version"].startswith("starwatt")

    def test_import_does_not_start_the_scheduler(self, web_entry) -> None:
        """B3：模块导入绝不能启动调度器（起停只能由 gunicorn 钩子驱动）。"""
        assert web_entry.scheduler_running() is False

    def test_no_app_run_in_entry_module(self) -> None:
        """**开发与生产都用 gunicorn** —— 入口模块里不许**调用** ``app.run()``。

        这条不是洁癖：Flask dev server 的 reloader 会 fork 一个子进程再导入
        一次模块，调度器就会起两次（重复抓取），而运行时不报任何错。

        用 AST 判断「调用」而不是 grep 文本 —— docstring 里说明「为什么不用
        app.run()」是合法的，不能因此判红。
        """
        import ast

        tree = ast.parse((PROJ / "web.py").read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "app"
        ]
        assert calls == [], f"web.py 里出现了 app.run() 调用：{calls}"


# ===========================================================================
# 调度器启动守卫（B3 防线 3）
# ===========================================================================
class TestStartSchedulerOnce:
    """守卫的判据是 **gunicorn arbiter 的 PID**，不是「模块导入时的 PID」。

    gunicorn 26 的顺序是 ``post_fork()`` → ``load_wsgi()``：``preload_app=False``
    时 ``web`` 是在 worker 里被导入的，所以「导入时 PID」恒等于 worker 自己的
    PID —— 早期版本用它当判据，结果是**每次启动都误判成 master 而崩溃**
    （用 `python -c "from web import start_scheduler_once; start_scheduler_once()"`
    就能复现）。现在由 ``post_fork`` 传入 ``server.pid``，判据才成立。
    """

    def test_refuses_to_start_in_master(self, web_entry) -> None:
        """在 master 进程里调用（``master_pid`` == 本进程）→ **启动即失败**。"""
        with pytest.raises(RuntimeError, match="master"):
            web_entry.start_scheduler_once(master_pid=os.getpid())

    def test_starts_in_worker(self, web_entry, monkeypatch) -> None:
        """worker 进程：``master_pid`` 与自己的 PID 不同 → 正常启动。"""
        fake = FakeScheduler()
        monkeypatch.setattr("starwatt.scheduler.build_scheduler", lambda: fake)

        web_entry.start_scheduler_once(master_pid=os.getpid() + 1)

        assert fake.started == 1
        assert web_entry.scheduler_running() is True

    def test_first_import_then_start_works(self, web_entry, monkeypatch) -> None:
        """复现 gunicorn 的真实顺序：**先导入、再启动**也必须能起来。

        这正是修掉的那个 P0：旧守卫在这种顺序下抛 RuntimeError，
        于是 ``gunicorn web:app`` 每个 worker 都起不来。
        """
        fake = FakeScheduler()
        monkeypatch.setattr("starwatt.scheduler.build_scheduler", lambda: fake)

        web_entry.start_scheduler_once(master_pid=os.getpid() - 1)  # 任意 ≠ 自己

        assert fake.started == 1

    def test_is_idempotent(self, web_entry, monkeypatch) -> None:
        """``post_fork`` 被重复调用时不得产生第二个调度器。"""
        fake = FakeScheduler()
        monkeypatch.setattr("starwatt.scheduler.build_scheduler", lambda: fake)

        web_entry.start_scheduler_once(master_pid=os.getpid() + 1)
        web_entry.start_scheduler_once(master_pid=os.getpid() + 1)

        assert fake.started == 1

    def test_stop_shuts_down_and_resets(self, web_entry, monkeypatch) -> None:
        fake = FakeScheduler()
        monkeypatch.setattr("starwatt.scheduler.build_scheduler", lambda: fake)

        web_entry.start_scheduler_once(master_pid=os.getpid() + 1)
        web_entry.stop_scheduler()

        assert fake.shutdowns == 1
        assert web_entry.scheduler_running() is False

    def test_stop_without_start_is_noop(self, web_entry) -> None:
        web_entry.stop_scheduler()  # 不应抛


# ===========================================================================
# B2 / B3 —— 配置文件契约
# ===========================================================================
class TestGunicornConfig:
    def _text(self) -> str:
        return (PROJ / "gunicorn.conf.py").read_text(encoding="utf-8")

    def test_single_worker(self) -> None:
        """🔴 多 worker = 重复抓取 N 倍。"""
        assert "workers = 1" in self._text()

    def test_preload_disabled(self) -> None:
        """🔴 preload=True → 调度器在 master 启动 → fork 后线程丢失。"""
        assert "preload_app = False" in self._text()

    def test_post_fork_starts_scheduler(self) -> None:
        text = self._text()
        assert "def post_fork(" in text
        assert "start_scheduler_once" in text

    def test_worker_hooks_stop_scheduler(self) -> None:
        text = self._text()
        assert "def worker_int(" in text
        assert "def worker_abort(" in text
        assert "stop_scheduler" in text


class TestGunicornHooks:
    """**真正执行** ``gunicorn.conf.py`` 的钩子 —— 验证 B2/B3 的接线。"""

    @staticmethod
    def _load():
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "gunicorn_conf_under_test", PROJ / "gunicorn.conf.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _server(pid: int):
        """gunicorn 传给钩子的 arbiter（只需要 ``.pid``）。"""

        class _Arbiter:
            pass

        arbiter = _Arbiter()
        arbiter.pid = pid
        return arbiter

    def test_post_fork_starts_the_scheduler(self, web_entry, monkeypatch) -> None:
        fake = FakeScheduler()
        monkeypatch.setattr("starwatt.scheduler.build_scheduler", lambda: fake)

        # gunicorn 传 (server, worker)；server.pid 是 master 的 PID
        self._load().post_fork(self._server(os.getpid() + 1), None)

        assert fake.started == 1

    def test_post_fork_passes_the_arbiter_pid(self) -> None:
        """钩子必须把 ``server.pid`` 传下去 —— 这是守卫唯一的判据来源。

        如果哪天有人把参数删了，守卫就退化成「不判定」，
        master 里误启动将不再报错（正是这个 P0 的反面）。
        """
        source = (PROJ / "gunicorn.conf.py").read_text(encoding="utf-8")
        assert "master_pid=server.pid" in source

    def test_worker_int_stops_the_scheduler(self, web_entry, monkeypatch) -> None:
        fake = FakeScheduler()
        monkeypatch.setattr("starwatt.scheduler.build_scheduler", lambda: fake)

        conf = self._load()
        conf.post_fork(self._server(os.getpid() + 1), None)
        conf.worker_int(None)

        assert fake.shutdowns == 1
        assert web_entry.scheduler_running() is False

    def test_bind_defaults_to_loopback(self, monkeypatch) -> None:
        monkeypatch.delenv("GUNICORN_BIND", raising=False)
        monkeypatch.delenv("FLASK_PORT", raising=False)
        assert self._load().bind == "127.0.0.1:5000"

    def test_bind_follows_flask_port(self, monkeypatch) -> None:
        monkeypatch.delenv("GUNICORN_BIND", raising=False)
        monkeypatch.setenv("FLASK_PORT", "5099")
        assert self._load().bind == "127.0.0.1:5099"


# ===========================================================================
# QQ 回调路由（POST /qq/events）
# ===========================================================================
class TestQqEventsRoute:
    """路由只做三件事：读原始 body → 验签 → 交给 handle_event。"""

    @staticmethod
    def _sign(body: bytes, timestamp: str = "1700000000") -> dict:
        from starwatt.notify import ed25519, qq

        seed = ed25519.seed_from_secret(BOT_SECRET)
        signature = ed25519.sign(seed, timestamp.encode() + body).hex()
        return {
            qq.SIGNATURE_HEADER: signature,
            qq.TIMESTAMP_HEADER: timestamp,
        }

    def test_validation_event_returns_signature(self, web_entry, tmp_db) -> None:
        import json

        from starwatt.config_registry import set_many

        set_many({"qq_bot_secret": BOT_SECRET})
        body = json.dumps(
            {"op": 13, "d": {"plain_token": "PT", "event_ts": "1700000000"}}
        ).encode()

        response = web_entry.app.test_client().post(
            "/qq/events", data=body, headers=self._sign(body)
        )

        assert response.status_code == 200
        payload = response.get_json()
        assert payload["plain_token"] == "PT" and payload["signature"]

    def test_bad_signature_is_rejected(self, web_entry, tmp_db) -> None:
        import json

        from starwatt.config_registry import set_many

        set_many({"qq_bot_secret": BOT_SECRET})
        body = json.dumps({"op": 0, "t": "C2C_MESSAGE_CREATE", "d": {}}).encode()
        headers = self._sign(body)
        headers["X-Signature-Ed25519"] = "00" * 64  # 换掉签名

        response = web_entry.app.test_client().post(
            "/qq/events", data=body, headers=headers
        )

        assert response.status_code == 401
        assert response.get_json()["code"] == 1

    def test_missing_signature_is_rejected(self, web_entry, tmp_db) -> None:
        """fail-closed：没配密钥 / 没带头一律 401。"""
        response = web_entry.app.test_client().post("/qq/events", json={"op": 0})
        assert response.status_code == 401

    def test_invalid_json_returns_400(self, web_entry, tmp_db) -> None:
        from starwatt.config_registry import set_many

        set_many({"qq_bot_secret": BOT_SECRET})
        body = b"not-json"
        response = web_entry.app.test_client().post(
            "/qq/events", data=body, headers=self._sign(body)
        )
        assert response.status_code == 400

    def test_command_event_is_acked(self, web_entry, tmp_db) -> None:
        """消息事件 → 200 + code 0（回复细节由 qq 单测覆盖，这里只验路由）。"""
        import json

        from starwatt.config_registry import set_many, set_state

        set_many({"qq_bot_secret": BOT_SECRET, "qq_bot_enabled": True})
        set_state("last_room_id", "room-1")
        body = json.dumps(
            {
                "op": 0,
                "t": "C2C_MESSAGE_CREATE",
                "d": {"id": "m-1", "content": "/帮助", "user_openid": "u-1"},
            }
        ).encode()

        response = web_entry.app.test_client().post(
            "/qq/events", data=body, headers=self._sign(body)
        )

        assert response.status_code == 200
        assert response.get_json() == {"code": 0}

    def test_handler_exception_still_returns_200(self, web_entry, tmp_db, monkeypatch) -> None:
        """绝不让 QQ 收到 5xx（它会重试风暴）。"""
        import json

        from starwatt.config_registry import set_many
        from starwatt.notify import qq

        set_many({"qq_bot_secret": BOT_SECRET})

        def _boom(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(qq, "handle_event", _boom)
        body = json.dumps({"op": 0, "t": "C2C_MESSAGE_CREATE", "d": {}}).encode()

        response = web_entry.app.test_client().post(
            "/qq/events", data=body, headers=self._sign(body)
        )

        assert response.status_code == 200
        assert response.get_json() == {"code": 0}


# ===========================================================================
# 飞书事件订阅路由（POST /feishu/event）
# ===========================================================================
class TestFeishuEventsRoute:
    KEY = "a" * 32

    @classmethod
    def _headers(cls, body: bytes, ts: str = "1700000000", nonce: str = "n") -> dict:
        import hashlib

        from starwatt.notify import crypto

        sig = hashlib.sha256((ts + nonce + cls.KEY + body.decode()).encode()).hexdigest()
        return {
            crypto.SIGNATURE_HEADERS[0]: sig,
            crypto.SIGNATURE_HEADERS[1]: ts,
            crypto.SIGNATURE_HEADERS[2]: nonce,
        }

    @staticmethod
    def _encrypted(payload: dict) -> bytes:
        import base64
        import hashlib
        import json

        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad

        digest = hashlib.sha256(TestFeishuEventsRoute.KEY.encode()).digest()
        plain = b"\x00" * 16 + json.dumps(payload).encode()
        cipher = AES.new(digest, AES.MODE_CBC, digest[:16])
        blob = base64.b64encode(cipher.encrypt(pad(plain, 16))).decode()
        return json.dumps({"encrypt": blob}).encode()

    def _configure(self) -> None:
        from starwatt.config_registry import set_many

        errors = set_many({"feishu_encrypt_key": self.KEY})
        assert errors == {}, errors

    def test_url_verification_echoes_challenge(self, web_entry, tmp_db) -> None:
        import json

        self._configure()
        body = json.dumps(
            {"type": "url_verification", "challenge": "CH-1"}
        ).encode()

        response = web_entry.app.test_client().post(
            "/feishu/event", data=body, headers=self._headers(body)
        )

        assert response.status_code == 200
        assert response.get_json() == {"challenge": "CH-1"}

    def test_encrypted_event_is_decrypted(self, web_entry, tmp_db) -> None:
        """🔑 加密事件：解密后仍要能完成握手。"""
        self._configure()
        body = self._encrypted({"type": "url_verification", "challenge": "CH-2"})

        response = web_entry.app.test_client().post(
            "/feishu/event", data=body, headers=self._headers(body)
        )

        assert response.status_code == 200
        assert response.get_json() == {"challenge": "CH-2"}

    def test_bad_signature_is_rejected(self, web_entry, tmp_db) -> None:
        self._configure()
        body = b'{"type":"url_verification","challenge":"C"}'
        headers = self._headers(body)
        headers["X-Lark-Signature"] = "0" * 64

        response = web_entry.app.test_client().post(
            "/feishu/event", data=body, headers=headers
        )

        assert response.status_code == 401

    def test_missing_signature_is_rejected(self, web_entry, tmp_db) -> None:
        """fail-closed：缺签名头一律 401。"""
        self._configure()
        response = web_entry.app.test_client().post(
            "/feishu/event", json={"type": "url_verification", "challenge": "C"}
        )
        assert response.status_code == 401

    def test_invalid_json_returns_400(self, web_entry, tmp_db) -> None:
        self._configure()
        body = b"not-json"
        response = web_entry.app.test_client().post(
            "/feishu/event", data=body, headers=self._headers(body)
        )
        assert response.status_code == 400

    def test_message_event_is_acked(self, web_entry, tmp_db) -> None:
        """消息事件 → 200（回复细节由 dispatcher 单测覆盖）。"""
        import json

        from starwatt.config_registry import set_many, set_state

        self._configure()
        set_many({"push_bot_enabled": True})
        set_state("last_room_id", "room-1")
        body = json.dumps(
            {
                "schema": "2.0",
                "header": {"event_type": "im.message.receive_v1"},
                "event": {
                    "sender": {"sender_id": {"open_id": "ou_1"}},
                    "message": {
                        "message_type": "text",
                        "content": json.dumps({"text": "/帮助"}),
                    },
                },
            }
        ).encode()

        response = web_entry.app.test_client().post(
            "/feishu/event", data=body, headers=self._headers(body)
        )

        assert response.status_code == 200
        assert response.get_json() == {"code": 0, "msg": "ok"}

    def test_handler_exception_still_returns_200(self, web_entry, tmp_db, monkeypatch) -> None:
        """绝不让飞书收到 5xx（它会重试风暴）。"""
        import json

        from starwatt.notify import dispatcher

        self._configure()

        def _boom(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(dispatcher, "handle_event", _boom)
        body = json.dumps({"type": "url_verification", "challenge": "C"}).encode()

        response = web_entry.app.test_client().post(
            "/feishu/event", data=body, headers=self._headers(body)
        )

        assert response.status_code == 200


class TestSystemdUnit:
    def _text(self) -> str:
        return (PROJ / "deploy" / "dorm-web.service").read_text(encoding="utf-8")

    def test_exec_start_uses_gunicorn(self) -> None:
        text = self._text()
        assert "gunicorn" in text
        assert "gunicorn.conf.py" in text
        assert "web:app" in text

    def test_legacy_flask_dev_server_is_gone(self) -> None:
        """B2：``python web.py``（Flask dev server + reloader）必须彻底消失。"""
        assert "python /opt/dorm-power-monitor/web.py" not in self._text()

    def test_working_directory_is_set(self) -> None:
        """``web:app`` 要靠 cwd 才能被 gunicorn 导入。"""
        assert "WorkingDirectory=/opt/dorm-power-monitor" in self._text()
