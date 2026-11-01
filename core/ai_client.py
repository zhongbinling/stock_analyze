# -*- coding: utf-8 -*-
"""D1-28 N2/N3: AI客户端 — OpenAI兼容(本地Ollama/云端DeepSeek等)"""
import json
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys_path = BASE.parent

import requests  # noqa: E402

ANALYSIS_PROMPT = """你是A股市场情绪分析专家。请分析以下宏观新闻，严格按JSON格式输出（不要任何其他文字）。

新闻标题: {title}
新闻内容: {content}

输出格式:
{{"level": 1, "type": "政策", "impact": "一句话影响(20字内)", "direction": "中性"}}

等级标准:
L5 股灾级: 流动性危机/千股跌停/系统性风险
L4 重大级: 政策组合拳(如9·24类)/超预期重磅/重大改革
L3 重要级: 加息降息/重要经济数据/重大监管变化
L2 关注级: 部委喊话/行业新闻/边际政策变化
L1 普通级: 一般性事件/常规数据发布

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
