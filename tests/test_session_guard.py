# -*- coding: utf-8 -*-
"""登录态与防跨站令牌的生命周期(11-25 用户实测: 避雷针页点"数据更新" → ✗ HTTP 400)。

原因: 写死在页面里的防跨站令牌默认只有 1 小时有效期(Flask-WTF WTF_CSRF_TIME_LIMIT=3600),
而管理页都是长期开着的看板 — 页面打开超过一小时后, 上面的每个写操作(数据更新/扫雷/保存
策略/入队…)都在进处理函数之前被 400 拒绝; 拒绝时回的是一整页 HTML, 页面只能显示"HTTP 400"。

这里的应用与生产配置一致: CSRF 开着、经真实登录表单建立会话 — server_app 夹具是关掉
CSRF 的, 其余用例从没走到这条路径。个股库不碰: 路由调用的 update_all / data_freshness
换成桩, 只验证 HTTP 层(更新逻辑本身见 test_stock_b2)。"""
import logging
import re
import time
from pathlib import Path

import pytest

import stock_risk as SR   # noqa: E402

UPDATE = "/admin/api/stock/update_all"
CODE = {"code": "600760.SH"}


class Live:
    """与生产配置一致的应用(CSRF 开启) + 走真实登录表单的客户端"""

    def __init__(self, app):
        self.app = app
        self.calls = []                      # 桩 update_all 收到的股票代码

    def update(self, code, path=None):
        self.calls.append(code)
        return {"日线": 1}

    def login(self, username="admin_t", password="x"):
        c = self.app.test_client()
        r = c.post("/login", data={"username": username, "password": password,
                                   "csrf_token": form_token(c, "/login")})
        assert r.status_code == 302 and r.headers["Location"] == "/mkt", r.status_code
        return c


@pytest.fixture()
def live(tmp_path, monkeypatch):
    import server as SV
    data, cfg = tmp_path / "data", tmp_path / "config"
    data.mkdir()
    cfg.mkdir()
    (cfg / "server.yaml").write_text(
        f"server: {{host: 127.0.0.1, port: 8599}}\n"
        f"app: {{theme_default: light}}\n"
        f"data: {{app_db: '{(data / 'app.db').as_posix()}', "
        f"secret_key_file: '{(data / 'secret.key').as_posix()}'}}\n"
        f"scheduler: {{enabled: false}}\n", encoding="utf-8")
    monkeypatch.setattr(SV, "SERVER_CFG", cfg / "server.yaml")
    monkeypatch.setenv("SA_NO_JOBS", "1")
    app = SV.create_app()
    app.config["TESTING"] = True
    with app.app_context():
        from server.models import User
        for name, level in (("admin_t", 9), ("reader", 2)):
            u = User(username=name, level=level)
            u.set_password("x")
            SV.db.session.add(u)
        SV.db.session.commit()
    box = Live(app)
    monkeypatch.setattr(SR, "update_all", box.update)
    monkeypatch.setattr(SR, "data_freshness",
                        lambda code, path=None: {"日线": {"fresh": True, "last": "2026-11-24"}})
    return box


def form_token(c, path):
    """表单页隐藏域里的令牌(WTForms 渲染)"""
    tags = re.findall(r"<input[^>]*csrf_token[^>]*>", c.get(path).get_data(as_text=True))
    assert tags, path + " 没有渲染 csrf_token 隐藏域"
    return re.search(r'value="([^"]*)"', tags[0]).group(1)


def page_token(c, path):
    """页面 <meta name="csrf-token"> 里写死的令牌(各页 api() 用它)"""
    html = c.get(path).get_data(as_text=True)
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, path + " 没有渲染 csrf-token"
    return m.group(1)


def at_age(monkeypatch, seconds, fn):
    """令牌签发 seconds 秒之后再发请求: 把时钟拨到"那时"(只在这一个请求期间)"""
    real = time.time
    with monkeypatch.context() as mp:
        mp.setattr(time, "time", lambda: real() + seconds)
        return fn()


def post_update(c, token):
    return c.post(UPDATE, json=CODE, headers={"X-CSRFToken": token})


# ── 令牌有效期 ──

def test_page_token_outlives_the_hour(live, monkeypatch):
    """页面打开超过一小时乃至隔夜后, 写操作照常通过(用户实测的场景)"""
    assert live.app.config["WTF_CSRF_ENABLED"] is True          # 与生产一致: 防护开着
    assert live.app.config["WTF_CSRF_TIME_LIMIT"] is None
    c = live.login()
    tok = page_token(c, "/admin/stock")
    for aged in (3601, 3600 * 9, 86400 * 3):
        r = at_age(monkeypatch, aged, lambda: post_update(c, tok))
        assert r.status_code == 200, (aged, r.get_data(as_text=True)[:300])
        j = r.get_json()
        assert j["ok"] is True and j["results"] == {"日线": 1} and "日线" in j["datasets"]
    assert live.calls == [CODE["code"]] * 3


def test_login_form_left_open_overnight_still_submits(live, monkeypatch):
    """登录页开着过夜再提交: 同一枚令牌, 不再是 400"""
    c = live.app.test_client()
    tok = form_token(c, "/login")
    r = at_age(monkeypatch, 86400, lambda: c.post(
        "/login", data={"username": "admin_t", "password": "x", "csrf_token": tok}))
    assert r.status_code == 302 and r.headers["Location"] == "/mkt"


def test_aging_the_clock_really_ages_the_token(live, monkeypatch):
    """上面两条不是空转: 同一枚令牌、同样的拨钟, 把时限改回框架默认的 3600 秒就过不了"""
    c = live.login()
    tok = page_token(c, "/admin/stock")
    live.app.config["WTF_CSRF_TIME_LIMIT"] = 3600
    assert at_age(monkeypatch, 3599, lambda: post_update(c, tok)).status_code == 200
    r = at_age(monkeypatch, 3601, lambda: post_update(c, tok))
    assert r.status_code == 400 and r.get_json()["msg"] == "页面安全令牌已过期, 请刷新页面后重试"
    assert live.calls == [CODE["code"]]            # 过期的那次没有被执行


# ── 被拒时页面脚本拿得到原因 ──

def test_rejected_token_returns_readable_json(live, caplog):
    """在别的标签页退出后重新登录: 会话里的令牌换了, 旧页面的令牌被拒 —
    回 JSON + 中文说明(原先是一整页 HTML, 页面只能显示 HTTP 400), 并记下原因"""
    c = live.login()
    tok = page_token(c, "/admin/stock")
    with c.session_transaction() as s:
        s["csrf_token"] = "0" * 40
    with caplog.at_level(logging.WARNING):
        r = post_update(c, tok)
    assert r.status_code == 400 and r.is_json
    assert r.get_json() == {"ok": False, "csrf": True, "msg": "登录状态已变化, 请刷新页面后重试"}
    assert live.calls == []
    assert any("CSRF校验未通过 POST %s(admin_t): The CSRF tokens do not match." % UPDATE
               in rec.getMessage() for rec in caplog.records), \
        [rec.getMessage() for rec in caplog.records]


def test_request_without_token_is_rejected_readably(live):
    r = live.login().post(UPDATE, json=CODE)                # 没带 X-CSRFToken
    assert r.status_code == 400
    assert r.get_json() == {"ok": False, "csrf": True, "msg": "请求未带安全令牌, 请刷新页面后重试"}
    assert live.calls == []


def test_form_post_with_stale_token_gets_site_style_page(live):
    """浏览器表单提交不是页面脚本: 回站内样式的说明页, "返回"指回来源页; 外站 Referer 不回链"""
    c = live.app.test_client()
    form = {"username": "admin_t", "password": "x", "csrf_token": "bogus"}

    def submit(referer):
        r = c.post("/login", data=form, headers={"Referer": referer})
        assert r.status_code == 400 and "text/html" in r.content_type
        return r.get_data(as_text=True)

    html = submit("http://localhost/login?next=/admin/stock")
    assert "请求校验未通过" in html and "请回到原页面" in html
    assert 'href="/login?next=/admin/stock"' in html
    assert "Bad Request" not in html                       # 不是框架默认的英文页
    html = submit("http://evil.example/login")
    assert 'href="/"' in html and "evil.example" not in html
    html = submit("")
    assert 'href="/"' in html
    # 看着像站内、浏览器会跳到外站的写法, 以及残缺的URL: 都不回链
    for bad in ("http://localhost//evil.example/login",      # 协议相对地址
                "http://localhost/\\evil.example/login",     # 反斜杠, 浏览器按 / 处理
                "http://[::1/login"):                        # 非法URL(urlsplit 抛错, 不应 500)
        html = submit(bad)
        assert 'href="/"' in html and "evil" not in html, bad


# ── 登录没了 ──

def test_logged_out_script_request_gets_401_json(live):
    """在别的标签页登出后, 旧页面的写操作: 401 + 说明。
    原先回"跳转登录页": fetch 跟着跳过去拿到登录页 HTML(状态码 200), 页面把它当成空对象,
    "数据更新"会显示"● 数据已更新"而实际什么都没做"""
    c = live.login()
    c.get("/logout")
    tok = page_token(c, "/login")                          # 旧页面里写死的令牌(登出不清它)
    r = post_update(c, tok)
    assert r.status_code == 401 and r.is_json
    assert r.get_json() == {"ok": False, "msg": "未登录或登录已失效, 请刷新页面重新登录"}
    assert live.calls == []


def test_browser_requests_still_redirect_to_login(live):
    """只改了页面脚本的请求(JSON 请求头): 浏览器导航与不带头的请求照旧跳登录页"""
    c = live.app.test_client()
    r = c.get("/admin/stock")
    assert r.status_code == 302 and r.headers["Location"] == "/login?next=/admin/stock"
    for path in ("/admin/api/rt/hist_gap", "/mkt/api/views"):
        r = c.get(path)
        assert r.status_code == 302 and r.headers["Location"].startswith("/login?next="), path


def test_insufficient_level_carries_a_message(live):
    """权限不足的 403 同样带说明(页面原先只能显示 HTTP 403); 原有字段不变"""
    c = live.login("reader")
    r = post_update(c, page_token(c, "/"))
    assert r.status_code == 403
    assert r.get_json() == {"error": "forbidden", "need_level": 7, "ok": False,
                            "msg": "权限不足, 该操作需要管理员(等级7)"}
    assert live.calls == []


# ── 服务端改回 JSON 说明后不用改页面: 靠的是各管理页的两处约定 ──

def test_every_admin_page_sends_token_and_shows_msg():
    """① 写请求都带 X-CSRFToken 与 JSON 头(服务端据此回 JSON 说明/401);
    ② 出错时显示的是服务端的 msg(取不到才是 HTTP 状态码)。
    覆盖所有自带 api() 的管理页 — 新加的页面自动纳入"""
    tpl = Path(__file__).resolve().parent.parent / "server" / "templates" / "admin"
    pages = [p for p in sorted(tpl.glob("*.html"))
             if "async function api(" in p.read_text(encoding="utf-8")]
    assert len(pages) >= 9, [p.name for p in pages]
    for p in pages:
        html = p.read_text(encoding="utf-8")
        assert "j.msg || ('HTTP ' + r.status)" in html, f"{p.name} 出错时不显示服务端说明"
        assert "'X-CSRFToken': CSRF" in html, f"{p.name} 没带页面令牌"
        bare = _write_call_without_token(html)
        assert bare is None, f"{p.name} 有写请求不带安全令牌: {bare}"


def _write_call_without_token(html):
    """页面里每一处写请求(POST/PUT/PATCH/DELETE): 要么经由带令牌的 api(), 要么自己带了 X-CSRFToken 头。
    返回第一处不带令牌的调用原文; 都带了 → None"""
    for m in re.finditer(r"\{method: *'(?:POST|PUT|PATCH|DELETE)'", html):
        a, f = html.rfind("api(", 0, m.start()), html.rfind("fetch(", 0, m.start())
        via_api = a > f and m.start() - a < 200
        if not via_api and "X-CSRFToken" not in html[m.start():m.start() + 300]:
            return html[m.start():m.start() + 80]
    return None
