# -*- coding: utf-8 -*-
"""routes.py 公共路由: 健康检查/主题(会话+账号级)/登录(限频/开放重定向守卫)/
注册/登出/me表单/设置/mkt视图发布守卫"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


# ── 公开端点 ──

def test_healthz_and_index(server_app):
    anon = server_app["app"].test_client()
    j = anon.get("/healthz").get_json()
    assert j["status"] == "ok" and j["service"] == "stock_analyze"
    assert anon.get("/").status_code == 200


def test_theme_session_and_account(server_app):
    c = server_app["client"]
    r = c.post("/api/theme", json={"theme": "dark"})
    assert r.get_json() == {"ok": True, "theme": "dark"}     # 会话级
    from server.models import User
    with server_app["app"].app_context():
        u = User.query.filter_by(username="admin_t").first()
        assert u.pref_theme == "dark"                         # 已登录→账号级
    assert c.post("/api/theme", json={"theme": "neon"}).get_json()["theme"] == "dark"
    assert c.post("/api/theme", json={}).get_json()["ok"] is True   # 无效值不炸


# ── 登录/注册/登出 ──

def test_login_wrong_password_and_logout(server_app):
    anon = server_app["app"].test_client()
    r = anon.post("/login", data={"username": "admin_t", "password": "bad"},
                  follow_redirects=False)
    assert r.status_code == 200                               # 回登录页
    assert b"\xe8\xb4\xa6\xe5\x8f\xb7\xe6\x88\x96\xe5\xaf\x86\xe7\xa0\x81" in r.data or \
        "账号或密码错误".encode() in r.data
    # 正确密码登录成功 → 跳转
    r2 = anon.post("/login", data={"username": "admin_t", "password": "x",
                                   "remember": "y"}, follow_redirects=False)
    assert r2.status_code == 302
    assert anon.get("/logout", follow_redirects=False).status_code == 302


def test_login_open_redirect_guard(server_app):
    """next仅允许站内相对路径: //evil与http://被拒回默认页"""
    anon = server_app["app"].test_client()
    anon.post("/login", data={"username": "admin_t", "password": "x"})
    r = anon.post("/login", data={"username": "admin_t", "password": "x",
                                  "next": "//evil.com"}, follow_redirects=False)
    assert r.status_code == 302 and "//evil.com" not in r.headers["Location"]
    r2 = anon.post("/login", data={"username": "admin_t", "password": "x",
                                   "next": "https://evil.com"},
                   follow_redirects=False)
    assert "evil.com" not in r2.headers["Location"]


def test_login_rate_limited(server_app, monkeypatch):
    # routes在导入时绑定名字 — 需patch路由模块上的引用而非auth源
    from server import routes as R
    monkeypatch.setattr(R, "rate_limit_left", lambda u: 30)
    anon = server_app["app"].test_client()
    r = anon.post("/login", data={"username": "admin_t", "password": "x"})
    assert "尝试次数过多" in r.get_data(as_text=True)


def test_register_flow(server_app):
    anon = server_app["app"].test_client()
    r = anon.post("/register", data={
        "username": "newuser1", "nickname": "新用户",
        "password": "password8", "confirm": "password8"},
        follow_redirects=False)
    assert r.status_code == 302                               # 注册成功跳转
    from server.models import User
    with server_app["app"].app_context():
        u = User.query.filter_by(username="newuser1").first()
        assert u is not None and u.level == 2                 # 自助注册默认等级
    # 重复用户名 → 校验错误不注册
    r2 = anon.post("/register", data={
        "username": "newuser1", "password": "password8",
        "confirm": "password8"})
    assert "该账号已被使用" in r2.get_data(as_text=True)
    # 两次密码不一致
    r3 = anon.post("/register", data={
        "username": "other2", "password": "password8",
        "confirm": "different8"})
    assert "两次密码不一致" in r3.get_data(as_text=True)


# ── me / settings ──

def test_me_nickname_and_password_change(server_app):
    c = server_app["client"]
    r = c.post("/me", data={"nick-nickname": "新昵称", "nick-submit": "保存昵称"},
               follow_redirects=False)
    assert r.status_code == 302
    from server.models import User
    with server_app["app"].app_context():
        u = User.query.filter_by(username="admin_t").first()
        assert u.nickname == "新昵称"
    # 错误当前密码 → 拒改
    r2 = c.post("/me", data={"pw-old": "wrong", "pw-new": "newpasswd8",
                             "pw-confirm": "newpasswd8",
                             "pw-submit": "修改密码"},
                follow_redirects=True)          # flash在重定向后的页面渲染
    assert "当前密码不正确" in r2.get_data(as_text=True)
    # 正确流程 → 可用新密码登录
    c.post("/me", data={"pw-old": "x", "pw-new": "newpasswd8",
                        "pw-confirm": "newpasswd8", "pw-submit": "修改密码"})
    anon = server_app["app"].test_client()
    assert anon.post("/login", data={"username": "admin_t",
                                     "password": "newpasswd8"},
                     follow_redirects=False).status_code == 302


def test_settings_theme_form(server_app):
    c = server_app["client"]
    r = c.post("/settings", data={"theme": "dark", "submit": "保存"},
               follow_redirects=False)
    assert r.status_code == 302
    with c.session_transaction() as s:
        assert s["theme"] == "dark"


# ── mkt 视图(发布守卫) ──

def _mk_run(app, tmp, published=1, env="prod", monkeypatch=None):
    from server.models import ViewRun
    # _safe_run_file要求: ROOT相对路径 + 位于views_store/下 + 文件存在
    store = tmp / "views_store"
    store.mkdir(exist_ok=True)
    f = store / f"run_{published}_{env}.html"
    f.write_text("<html>ok</html>", encoding="utf-8")
    if monkeypatch is not None:
        from server import routes_admin as RA
        monkeypatch.setattr(RA, "ROOT", tmp)
    with app.app_context():
        r = ViewRun(vtype="sentiment", def_name="d", env=env, status="done",
                    published=published, file_path=str(f.relative_to(tmp)),
                    data_date="2026-11-16",
                    published_at=datetime(2026, 11, 16, 9, 0,
                                          tzinfo=timezone.utc))
        from server import db as sdb
        sdb.session.add(r)
        sdb.session.commit()
        return r.id


def test_mkt_latest_and_view_guard(server_app, monkeypatch):
    app, c, tmp = server_app["app"], server_app["client"], server_app["tmp"]
    rid_pub = _mk_run(app, tmp, published=1, monkeypatch=monkeypatch)
    rid_unpub = _mk_run(app, tmp, published=0, monkeypatch=monkeypatch)
    j = c.get("/mkt/api/latest").get_json()
    assert j["view"]["rid"] == rid_pub and j["view"]["data_date"] == "2026-11-16"
    assert c.get("/mkt").status_code == 200
    assert c.get(f"/mkt/view/{rid_pub}").status_code == 200
    assert c.get(f"/mkt/view/{rid_unpub}").status_code == 404    # 未发布不可见
    assert c.get("/mkt/view/99999").status_code == 404
    # 指定rid
    j2 = c.get(f"/mkt/api/latest?rid={rid_pub}").get_json()
    assert j2["view"]["rid"] == rid_pub


def test_mkt_views_level_gate(server_app, monkeypatch):
    app, c = server_app["app"], server_app["client"]
    _mk_run(app, server_app["tmp"], monkeypatch=monkeypatch)
    j = c.get("/mkt/api/views").get_json()                       # admin(9)≥2 ✓
    assert len(j["views"]) >= 1
    anon = app.test_client()
    assert anon.get("/mkt/api/views").status_code == 302         # 未登录重定向
