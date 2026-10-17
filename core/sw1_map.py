# -*- coding: utf-8 -*-
"""申万一级映射构建 (S2.1-C): 成分股行业名 → SW L1 → 东财BK

用法: python sw1_map.py build   # 一次性构建 sw1_map 表
      python sw1_map.py show    # 查看映射结果
"""
import argparse
import sys
from pathlib import Path
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import tushare_api as T  # noqa: E402


def build():
    """成分股投票: 每只股票的东财行业名 → 出现最多的SW一级即为该行业的映射"""
    # 1. SW L1 行业及成分
    sw = T.call("index_classify", "", level="L1", src="SW2021")
    print(f"SW L1: {len(sw)} 个行业")
    stock2sw = {}   # ts_code -> sw1_name
    for _, row in sw.iterrows():
        members = T.call("index_member", "", index_code=row["index_code"])
        for _, m in members.iterrows():
            stock2sw[m["con_code"]] = row["industry_name"]
        print(f"  {row['industry_name']}: {len(members)} 只")
    print(f"成分股总数: {len(stock2sw)}")

    # 2. stock_basic: ts_code -> 东财行业名
    sb = T.call("stock_basic", "ts_code,name,industry", list_status="L")
    industry2ts = {}   # 东财行业名 -> [sw1_name votes]
    for _, r in sb.iterrows():
        ind = r["industry"]
        ts = r["ts_code"]
        if not isinstance(ind, str) or not ind or ts not in stock2sw:
            continue
        industry2ts.setdefault(ind, []).append(stock2sw[ts])

    # 3. 投票: 每个东财行业名 → 得票最多的SW一级
    mapping = {}   # 东财行业名 -> sw1_name
    for ind, votes in industry2ts.items():
        winner = Counter(votes).most_common(1)[0]
        mapping[ind] = winner[0]

    # 4. 东财行业名 → BK码(从 sectors 表, 精确→去后缀→子串)
    import sqlite3
    conn = sqlite3.connect(str(BASE / "data" / "sentiment.db"))
    all_bk = conn.execute(
        "SELECT code, name FROM sectors WHERE kind='industry'").fetchall()
    conn.execute("DROP TABLE IF EXISTS sw1_map")
    conn.execute("CREATE TABLE sw1_map(bk_code TEXT, bk_name TEXT, sw1_name TEXT, "
                 "PRIMARY KEY(bk_code))")

    def find_bk(ind_name):
        # 人工补充映射(stock_basic行业名 → BK名关键词)
        MANUAL = {
            "IT设备": "计算机", "专用机械": "机械", "中成药": "中药", "乳制品": "食品",
            "供气供热": "燃气", "元器件": "电子", "全国地产": "房地产", "公共交通": "公交",
            "其他商业": "商业", "化工原料": "化学", "塑料": "塑料", "橡胶": "橡胶",
            "船舶": "船舶", "装修装饰": "装修", "证券": "证券", "超市连锁": "超市",
            "路桥": "公路", "软件服务": "软件", "轻工机械": "机械", "运输设备": "运输",
            "钢加工": "钢", "铁路": "铁路", "陶瓷": "陶瓷", "食品": "食品",
            "白酒": "白酒", "啤酒": "啤酒", "葡萄酒": "酒", "软饮料": "饮料",
            "家用电器": "家电", "汽车服务": "汽车服务", "汽车零部件": "汽车零",
            "通信设备": "通信设备", "互联网": "互联网", "计算机": "计算机",
            "半导体": "半导体", "生物制药": "生物", "化学制药": "化学制药",
            "医疗保健": "医疗", "环境保护": "环保", "仓储物流": "物流",
            "石油开采": "石油", "煤炭": "煤炭", "有色金属": "有色", "钢铁": "钢铁",
            "银行": "银行", "保险": "保险", "房地产": "房地产", "建筑": "建筑",
            "电力": "电力", "水务": "水务", "燃气": "燃气", "机场": "机场",
            "港口": "港口", "高速公路": "高速", "园林": "园林", "纺织": "纺织",
            "服装": "服装", "造纸": "造纸", "印刷": "印刷", "家具": "家具",
            "玩具": "玩具", "珠宝首饰": "珠宝", "酒店餐饮": "酒店", "旅游": "旅游",
            "教育": "教育", "传媒": "传媒", "出版": "出版", "广告": "广告",
            "贸易": "贸易", "农业": "农业", "饲料": "饲料", "养殖": "养殖",
            "种植": "种植", "农药": "农药", "化肥": "化肥", "兽药": "兽药",
        }
        # 先查人工映射
        if ind_name in MANUAL:
            kw = MANUAL[ind_name]
            for c, n in all_bk:
                if kw in n:
                    return c, n
        # 精确
        for c, n in all_bk:
            if n == ind_name:
                return c, n
        # 去后缀
        base = ind_name.rstrip("ⅠⅡⅢ").strip()
        for c, n in all_bk:
            if n == base:
                return c, n
        # 子串
        cands = [(c, n) for c, n in all_bk
                 if ind_name in n or (len(n) >= 2 and n in ind_name)]
        if cands:
            return min(cands, key=lambda x: len(x[1]))
        return None, None

    inserted = 0
    unmapped = []
    for ind, sw1 in sorted(mapping.items()):
        code, bkname = find_bk(ind)
        if code:
            conn.execute("INSERT OR REPLACE INTO sw1_map VALUES(?,?,?)",
                         (code, bkname, sw1))
            inserted += 1
        else:
            unmapped.append((ind, sw1))
    conn.commit()

    # 统计
    total_bk = conn.execute("SELECT COUNT(*) FROM sectors WHERE kind='industry'").fetchone()[0]
    mapped = conn.execute("SELECT COUNT(*) FROM sw1_map").fetchone()[0]
    sws = conn.execute("SELECT DISTINCT sw1_name FROM sw1_map ORDER BY sw1_name").fetchall()
    conn.close()
    print(f"\n映射结果: {mapped}/{total_bk} 行业板块 → {len(sws)} 个SW一级")
    print(f"SW一级: {[s[0] for s in sws]}")
    if unmapped:
        print(f"未匹配({len(unmapped)}):", unmapped[:10])


def show():
    import sqlite3
    conn = sqlite3.connect(str(BASE / "data" / "sentiment.db"))
    for r in conn.execute(
            "SELECT sw1_name, COUNT(*) as cnt, GROUP_CONCAT(bk_name, '|') FROM sw1_map "
            "GROUP BY sw1_name ORDER BY cnt DESC"):
        boards = r[2][:60] + "..." if len(r[2]) > 60 else r[2]
        print(f"  {r[0]:<6} ({r[1]:>2} 板块): {boards}")
    conn.close()


def build_sw2():
    """S3-E: 构建申万二级映射 sw2_map(bk_code, bk_name, sw1_name, sw2_name)"""
    # 1. SW L1 (用于确定 L2 → L1 父子关系)
    sw1 = T.call("index_classify", "", level="L1", src="SW2021")
    stock2l1 = {}
    l1_names = set()
    for _, row in sw1.iterrows():
        l1_names.add(row["industry_name"])
        members = T.call("index_member", "", index_code=row["index_code"])
        for _, m in members.iterrows():
            stock2l1[m["con_code"]] = row["industry_name"]
    print(f"SW L1: {len(sw1)} 个, 成分 {len(stock2l1)} 只")

    # 2. SW L2
    sw2 = T.call("index_classify", "", level="L2", src="SW2021")
    print(f"SW L2: {len(sw2)} 个")
    stock2l2 = {}
    for _, row in sw2.iterrows():
        members = T.call("index_member", "", index_code=row["index_code"])
        for _, m in members.iterrows():
            stock2l2[m["con_code"]] = row["industry_name"]
    print(f"  成分 {len(stock2l2)} 只")

    # 3. 确定 L2 → L1 父子(成分股重叠)
    l2_to_l1 = {}
    for _, row in sw2.iterrows():
        members = T.call("index_member", "", index_code=row["index_code"])
        l1_votes = Counter()
        for _, m in members.iterrows():
            l1 = stock2l1.get(m["con_code"])
            if l1:
                l1_votes[l1] += 1
        if l1_votes:
            l2_to_l1[row["industry_name"]] = l1_votes.most_common(1)[0][0]
    print(f"  L2→L1 关系: {len(l2_to_l1)} 个")

    # 4. stock_basic: ts_code → 东财行业名 → 投票得出 SW L2
    sb = T.call("stock_basic", "ts_code,name,industry", list_status="L")
    industry2sw2 = {}
    for _, r in sb.iterrows():
        ind = r["industry"]
        ts = r["ts_code"]
        if not isinstance(ind, str) or not ind or ts not in stock2l2:
            continue
        industry2sw2.setdefault(ind, []).append(stock2l2[ts])

    mapping2 = {}   # 东财行业名 → sw2_name
    for ind, votes in industry2sw2.items():
        mapping2[ind] = Counter(votes).most_common(1)[0][0]

    # 5. 东财行业名 → BK码(复用 find_bk 逻辑)
    import sqlite3
    conn = sqlite3.connect(str(BASE / "data" / "sentiment.db"))
    all_bk = conn.execute(
        "SELECT code, name FROM sectors WHERE kind='industry'").fetchall()

    def find_bk(ind_name):
        if ind_name in MANUAL_MAP:
            kw = MANUAL_MAP[ind_name]
            for c, n in all_bk:
                if kw in n:
                    return c, n
        for c, n in all_bk:
            if n == ind_name:
                return c, n
        base = ind_name.rstrip("ⅠⅡⅢ").strip()
        for c, n in all_bk:
            if n == base:
                return c, n
        cands = [(c, n) for c, n in all_bk
                 if ind_name in n or (len(n) >= 2 and n in ind_name)]
        if cands:
            return min(cands, key=lambda x: len(x[1]))
        return None, None

    conn.execute("DROP TABLE IF EXISTS sw2_map")
    conn.execute("CREATE TABLE sw2_map(bk_code TEXT, bk_name TEXT, "
                 "sw1_name TEXT, sw2_name TEXT, PRIMARY KEY(bk_code))")
    inserted, unmapped = 0, []
    for ind, sw2_name in sorted(mapping2.items()):
        code, bkname = find_bk(ind)
        if code:
            sw1_name = l2_to_l1.get(sw2_name, "")
            conn.execute("INSERT OR REPLACE INTO sw2_map VALUES(?,?,?,?)",
                         (code, bkname, sw1_name, sw2_name))
            inserted += 1
        else:
            unmapped.append((ind, sw2_name))
    conn.commit()
    total_bk = conn.execute("SELECT COUNT(*) FROM sectors WHERE kind='industry'").fetchone()[0]
    sw2s = conn.execute("SELECT COUNT(DISTINCT sw2_name) FROM sw2_map").fetchone()[0]
    conn.close()
    print(f"\nSW2 映射: {inserted}/{total_bk} BK → {sw2s} 个SW二级")
    if unmapped:
        print(f"未匹配({len(unmapped)}):", unmapped[:10])


# 提取 MANUAL_MAP 供 build_sw2 复用
MANUAL_MAP = {
    "IT设备": "计算机", "专用机械": "机械", "中成药": "中药", "乳制品": "食品",
    "供气供热": "燃气", "元器件": "电子", "全国地产": "房地产", "公共交通": "公交",
    "其他商业": "商业", "化工原料": "化学", "塑料": "塑料", "橡胶": "橡胶",
    "船舶": "船舶", "装修装饰": "装修", "证券": "证券", "超市连锁": "超市",
    "路桥": "公路", "软件服务": "软件", "轻工机械": "机械", "运输设备": "运输",
    "钢加工": "钢", "铁路": "铁路", "陶瓷": "陶瓷", "食品": "食品",
    "白酒": "白酒", "啤酒": "啤酒", "葡萄酒": "酒", "软饮料": "饮料",
    "家用电器": "家电", "汽车服务": "汽车服务", "汽车零部件": "汽车零",
    "通信设备": "通信设备", "互联网": "互联网", "计算机": "计算机",
    "半导体": "半导体", "生物制药": "生物", "化学制药": "化学制药",
    "医疗保健": "医疗", "环境保护": "环保", "仓储物流": "物流",
    "石油开采": "石油", "煤炭": "煤炭", "有色金属": "有色", "钢铁": "钢铁",
    "银行": "银行", "保险": "保险", "房地产": "房地产", "建筑": "建筑",
    "电力": "电力", "水务": "水务", "燃气": "燃气", "机场": "机场",
    "港口": "港口", "高速公路": "高速", "园林": "园林", "纺织": "纺织",
    "服装": "服装", "造纸": "造纸", "印刷": "印刷", "家具": "家具",
    "玩具": "玩具", "珠宝首饰": "珠宝", "酒店餐饮": "酒店", "旅游": "旅游",
    "教育": "教育", "传媒": "传媒", "出版": "出版", "广告": "广告",
    "贸易": "贸易", "农业": "农业", "饲料": "饲料", "养殖": "养殖",
    "种植": "种植", "农药": "农药", "化肥": "化肥", "兽药": "兽药",
}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "build-sw2", "show"])
    args = ap.parse_args()
    if args.cmd == "build":
        build()
    elif args.cmd == "build-sw2":
        build_sw2()
    else:
        show()
