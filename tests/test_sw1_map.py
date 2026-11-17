# -*- coding: utf-8 -*-
"""sw1_map: 成分股投票(东财行业名→SW一级) + BK四级匹配(人工/精确/去后缀/子串)
+ build_sw2的L2→L1父子投票"""
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import sw1_map as SM  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "data" / "sentiment.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE sectors(code TEXT, name TEXT, kind TEXT)")
    con.executemany("INSERT INTO sectors VALUES(?,?,'industry')", [
        ("BK1", "银行"), ("BK2", "计算机"), ("BK3", "房地产"),
        ("BK4", "计算机设备"), ("BK5", "白酒")])
    con.commit(); con.close()
    monkeypatch.setattr(SM, "BASE", tmp_path)

    def fake_call(api, fields="", **kw):
        if api == "index_classify":
            lv = kw["level"]
            if lv == "L1":
                return pd.DataFrame({"index_code": ["801740", "801120"],
                                     "industry_name": ["银行", "食品饮料"]})
            return pd.DataFrame({"index_code": ["801795"],
                                 "industry_name": ["白酒Ⅱ"]})
        if api == "index_member":
            ic = kw["index_code"]
            if ic == "801740":
                return pd.DataFrame({"con_code": ["601398.SH", "600036.SH"]})
            if ic == "801120":
                return pd.DataFrame({"con_code": ["600519.SH", "000858.SZ"]})
            return pd.DataFrame({"con_code": ["600519.SH", "000858.SZ"]})  # L2白酒
        if api == "stock_basic":
            return pd.DataFrame({
                "ts_code": ["601398.SH", "600036.SH", "600519.SH", "000858.SZ",
                            "600100.SH"],
                "name": ["工商银行", "招商银行", "茅台", "五粮液", "无行业"],
                # 工商银行EM行业=白酒(跨行业错位) → 白酒行业票 2:1 食品饮料胜出
                "industry": ["白酒", "银行", "白酒", "白酒", ""]})
        raise ValueError(api)
    monkeypatch.setattr(SM.T, "call", fake_call)
    return db


def _rows(db, sql):
    con = sqlite3.connect(str(db))
    r = con.execute(sql).fetchall()
    con.close()
    return r


def test_build_voting_and_bk_match(env, capsys):
    """白酒行业2股投食品饮料(601398混入银行票数1:2不敌) → 白酒BK5→食品饮料;
    银行精确匹配BK1; 无行业股剔除"""
    SM.build()
    m = dict(_rows(env, "SELECT bk_code, sw1_name FROM sw1_map"))
    assert m.get("BK5") == "食品饮料"      # 白酒板块 → 投票胜者食品饮料
    assert m.get("BK1") == "银行"          # 银行精确匹配
    out = capsys.readouterr().out
    assert "映射结果" in out


def test_manual_and_suffix_and_substring(env, monkeypatch):
    """人工映射(IT设备→计算机) / 去罗马后缀 / 子串最短名优先"""
    con = sqlite3.connect(str(env))
    con.executemany("INSERT INTO sectors VALUES(?,?,'industry')", [
        ("BK6", "房地产开发"), ("BK7", "机械")])
    con.commit(); con.close()
    # 改stock_basic: IT设备股 + 房地产Ⅰ行业名场景(经投票产生行业名)
    def fake_call(api, fields="", **kw):
        if api == "index_classify":
            return pd.DataFrame({"index_code": ["801120"],
                                 "industry_name": ["食品饮料"]})
        if api == "index_member":
            return pd.DataFrame({"con_code": ["600519.SH", "000858.SZ",
                                              "600100.SH", "600200.SH"]})
        return pd.DataFrame({
            "ts_code": ["600519.SH", "000858.SZ", "600100.SH", "600200.SH"],
            "name": ["茅台", "五粮液", "IT股", "地产股"],
            "industry": ["IT设备", "IT设备", "房地产Ⅰ", "房地产开发"]})
    monkeypatch.setattr(SM.T, "call", fake_call)
    SM.build()
    m = dict(_rows(env, "SELECT bk_code, sw1_name FROM sw1_map"))
    assert "BK2" in m                    # IT设备→人工映射关键词"计算机"命中BK2
    assert "BK3" in m                    # 房地产Ⅰ去后缀→BK3(或子串)


def test_build_sw2_parent_votes(env, capsys):
    """L2白酒的成分全部投票给L1食品饮料 → sw2_map带父级; 东财行业名→SW二级"""
    SM.build_sw2()
    rows = _rows(env, "SELECT bk_code, sw1_name, sw2_name FROM sw2_map")
    by_sw2 = {r[2]: r for r in rows}
    assert "白酒Ⅱ" in by_sw2
    assert by_sw2["白酒Ⅱ"][1] == "食品饮料"    # 父级由成分重叠投票得出
    out = capsys.readouterr().out
    assert "SW2 映射" in out


def test_manual_map_nonempty():
    """人工映射表非空且值全为非空串(构建期数据)"""
    assert len(SM.MANUAL_MAP) > 50
    assert all(isinstance(v, str) and v for v in SM.MANUAL_MAP.values())
