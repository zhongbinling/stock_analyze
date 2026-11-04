# -*- coding: utf-8 -*-
"""D1-28 N2/N3: AI客户端 — OpenAI兼容(本地Ollama/云端DeepSeek等)"""
import json
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys_path = BASE.parent

import requests  # noqa: E402

ANALYSIS_PROMPT = """你是资深A股宏观策略分析师，正在为市场情绪系统标注宏观新闻事件等级。请分析以下新闻并输出JSON。

新闻标题: {title}
新闻内容: {content}

严格按以下JSON格式输出（不要任何其他文字）:
{{"level": 1, "type": "政策", "impact": "一句话影响(20字内)", "direction": "中性"}}

## 等级判定标准（含示例）

**L5 股灾级** — 系统性风险，市场恐慌
示例: 千股跌停、流动性危机、金融危机、战争爆发、汇市崩盘

**L4 重大级** — 超预期重磅政策，历史性改革
示例: 9·24政策组合拳、四万亿刺激、注册制改革、重大贸易协议签署/破裂、降准降息同时出台

**L3 重要级** — 影响市场方向的重要事件
示例: 央行降息或降准25bp以上、GDP/CPI等关键经济数据发布、美联储议息决议、重大监管新规出台、重要人事任命（央行行长/证监会主席级别）、大规模制裁/关税

**L2 关注级** — 影响市场情绪或板块的政策/事件
示例: 部委喊话稳市场、证监会座谈会、央行公开市场操作、行业政策微调、区域冲突升级、重要企业财报超/低预期、板块热点催化

**L1 普通级** — 日常新闻，对市场影响微弱
示例: 个股公告、行业日常数据、公司人事变动、一般性会议、产品发布

## 判定原则

1. **上倾原则**: 在L1和L2之间犹豫时，选L2（能进入宏观新闻源的已有一定重要性）
2. **机构锚定**: 涉及央行/国务院/证监会/政治局/美联储/GDP/CPI 的新闻，最低L2
3. **实际影响**: 降息降准=L3起，涉及特朗普/美联储对A股的直接影响=L2起
4. **幅度区分**: 央行互换便利中标=L2（工具操作），央行降准=L3（政策转向）
5. **标题vs内容**: 以标题中体现的信息量为主要判断依据

type: 政策|货币|产业|地缘|市场|其他
direction: 利多|利空|中性"""


def chat(base_url: str, model: str, messages: list, api_key: str = "",
         temperature: float = 0.1, max_tokens: int = 2000,  # qwen3等thinking模型需足够额度
         timeout: int = 60) -> str:
    """OpenAI兼容chat调用; 返回assistant文本; 失败抛异常"""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    r = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        json={"model": model, "messages": messages,
              "temperature": temperature, "max_tokens": max_tokens},
        headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def analyze_news(base_url: str, model: str, api_key: str,
                 title: str, content: str = "",
                 found_text: str = "", timeout: int = 60) -> dict:
    """单条新闻AI分析 → {"level", "type", "impact", "direction"}; 失败抛"""
    # found_text追加到content尾部(补充原文信息进分析上下文)
    full_content = content or "无详细内容"
    if found_text:
        full_content += f"\n\n补充原文摘要: {found_text[:500]}"
    resp = chat(base_url, model,
                [{"role": "user",
                  "content": ANALYSIS_PROMPT.format(title=title,
                                                    content=full_content)}],
                api_key=api_key, temperature=0.1, max_tokens=2000,
                timeout=timeout)
    # 提取JSON(模型可能包裹在```json ...```中)
    text = resp.strip()
    if "```" in text:
        parts = text.split("```")
        for p in parts:
            p = p.replace("json", "").strip()
            if p.startswith("{"):
                text = p
                break
    try:
        j = json.loads(text)
        level = int(j.get("level", 1))
        if not 1 <= level <= 5:
            level = 1
        return {"level": level,
                "type": str(j.get("type", "其他"))[:16],
                "impact": str(j.get("impact", ""))[:100],
                "direction": str(j.get("direction", "中性"))[:8]}
    except (json.JSONDecodeError, ValueError, TypeError):
        return {"level": 1, "type": "其他", "impact": "",
                "direction": "中性", "_parse_error": resp[:100]}


CLOUD_PROMPT = """你是资深A股宏观策略分析师。以下是本地AI的初步分析，请进行更深入的二次分析。

新闻标题: {title}
新闻内容: {content}
本地AI初步: L{local_level} [{local_type}] {local_impact} ({local_direction})

请综合评估（可修正等级），严格按JSON输出:
{{"level": 1, "type": "政策", "impact": "更精准的影响分析(30字内)", "direction": "利多", "reasoning": "修正理由(30字内)"}}

等级标准:
L5 股灾级: 流动性危机/千股跌停/系统性风险
L4 重大级: 政策组合拳(如9·24类)/超预期重磅
L3 重要级: 加息降息/重要经济数据/重大监管
L2 关注级: 部委喊话/行业新闻
L1 普通级: 一般性事件

type: 政策|货币|产业|地缘|市场|其他
direction: 利多|利空|中性"""


def analyze_news_cloud(base_url: str, model: str, api_key: str,
                       title: str, content: str,
                       local: dict, timeout: int = 120) -> dict:
    """云端二次分析(含本地结果作为上下文)"""
    resp = chat(base_url, model,
                [{"role": "user",
                  "content": CLOUD_PROMPT.format(
                      title=title, content=content or "无详细内容",
                      local_level=local.get("level", 1),
                      local_type=local.get("type", ""),
                      local_impact=local.get("impact", ""),
                      local_direction=local.get("direction", ""))}],
                api_key=api_key, temperature=0.1, max_tokens=2000,
                timeout=timeout)
    text = resp.strip()
    if "```" in text:
        parts = text.split("```")
        for p in parts:
            p = p.replace("json", "").strip()
            if p.startswith("{"):
                text = p
                break
    try:
        j = json.loads(text)
        level = int(j.get("level", 1))
        if not 1 <= level <= 5:
            level = 1
        return {"level": level,
                "type": str(j.get("type", "其他"))[:16],
                "impact": str(j.get("impact", ""))[:150],
                "direction": str(j.get("direction", "中性"))[:8],
                "reasoning": str(j.get("reasoning", ""))[:100]}
    except (json.JSONDecodeError, ValueError, TypeError):
        return {"level": local.get("level", 1), "type": local.get("type", "其他"),
                "impact": local.get("impact", ""), "direction": local.get("direction", "中性"),
                "_parse_error": resp[:100]}
