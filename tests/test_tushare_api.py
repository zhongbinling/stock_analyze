# -*- coding: utf-8 -*-
"""tushare_api: token加载优先级/接口调用成功与错误路径/常用接口参数组装"""
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import tushare_api as T  # noqa: E402


@pytest.fixture()
def tok(tmp_path, monkeypatch):
    f = tmp_path / "tushare_token.txt"
    monkeypatch.setattr(T, "TOKEN_FILE", f)
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    return f


# ── token 加载 ──

def test_load_token_priority(tok, monkeypatch):
    assert T.load_token() == "" and T.has_token() is False   # 无token
    monkeypatch.setenv("TUSHARE_TOKEN", "env-token")
    assert T.load_token() == "env-token"                      # 环境变量兜底
    tok.write_text("file-token\n", encoding="utf-8")
    assert T.load_token() == "file-token"                     # 文件优先
    tok.write_text("   ")                                     # 空白文件 → 回退env
    assert T.load_token() == "env-token"


def test_call_no_token_raises(tok):
    with pytest.raises(RuntimeError, match="token"):
        T.call("daily", "ts_code")


# ── call 成功/错误路径(网络mock) ──

def _mock_post(monkeypatch, payload, slept):
    import requests

    class FakeResp:
        def raise_for_status(self):
            if isinstance(payload, Exception):
                raise payload

        def json(self):
            return payload
    monkeypatch.setattr(requests, "post",
                        lambda url, **kw: FakeResp())
    monkeypatch.setattr(T.time, "sleep", lambda s: slept.append(s))


def test_call_ok_explicit_fields(tok, monkeypatch):
    tok.write_text("tk", encoding="utf-8")
    sent = []

    def fake_post(url, json=None, **kw):
        sent.append(json)
        return type("R", (), {
            "raise_for_status": lambda self: None,
            "json": lambda self: {"code": 0, "data": {
                "fields": ["ts_code", "close"],
                "items": [["600519.SH", 1500.0]]}}})()
    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    slept = []
    monkeypatch.setattr(T.time, "sleep", lambda s: slept.append(s))
    df = T.call("daily", "ts_code,close", ts_code="600519.SH")
    assert df["close"].tolist() == [1500.0]
    assert sent[0]["api_name"] == "daily" and sent[0]["token"] == "tk"
    assert sent[0]["params"] == {"ts_code": "600519.SH"}
    assert slept == [T.SLEEP]                      # 限频间隔


def test_call_ok_fields_from_response(tok, monkeypatch):
    """fields空 → 用响应内fields建列"""
    tok.write_text("tk", encoding="utf-8")
    import requests
    monkeypatch.setattr(requests, "post", lambda url, **kw: type("R", (), {
        "raise_for_status": lambda self: None,
        "json": lambda self: {"code": 0, "data": {
            "fields": ["a", "b"], "items": [[1, 2]]}}})())
    monkeypatch.setattr(T.time, "sleep", lambda s: None)
    df = T.call("x", "")
    assert list(df.columns) == ["a", "b"]


def test_call_api_error_and_http_error(tok, monkeypatch):
    tok.write_text("tk", encoding="utf-8")
    import requests
    monkeypatch.setattr(requests, "post", lambda url, **kw: type("R", (), {
        "raise_for_status": lambda self: None,
        "json": lambda self: {"code": 401, "msg": "没有权限"}})())
    with pytest.raises(RuntimeError, match="没有权限"):
        T.call("x", "")
    monkeypatch.setattr(requests, "post", lambda url, **kw: type("R", (), {
        "raise_for_status": lambda self: (_ for _ in ()).throw(
            requests.HTTPError("502")), "json": lambda self: {}})())
    with pytest.raises(requests.HTTPError):
        T.call("x", "")


# ── 常用接口参数组装 ──

def test_wrappers_params(tok, monkeypatch):
    tok.write_text("tk", encoding="utf-8")
    calls = []

    def fake_call(api, fields="", **params):
        calls.append((api, fields, params))
        return pd.DataFrame()
    monkeypatch.setattr(T, "call", fake_call)
    T.trade_cal("20260101", "20260131")
    T.index_daily("000001.SH", "20260101")
    T.index_daily("000001.SH", "20260101", "20260201")
    T.limit_list_d("20261015")
    T.margin("20261010")
    T.margin("20261010", "20261015")
    apis = [c[0] for c in calls]
    assert apis == ["trade_cal", "index_daily", "index_daily",
                    "limit_list_d", "margin", "margin"]
    assert calls[0][2]["exchange"] == "SSE" and calls[0][2]["is_open"] == ""
    assert "end_date" not in calls[4][2]           # margin无end不传
    assert calls[5][2]["end_date"] == "20261015"   # 有end传入
