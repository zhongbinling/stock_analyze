# stock_analyze · A-Share Market Sentiment Scoring System

[![中文文档](https://img.shields.io/badge/docs-中文-brightgreen)](../整体方案.md) [![demo](https://img.shields.io/badge/demo-sentiment--viewer-orange)](../../demo/sentiment_viewer.html)

Analyzes A-share (China stock market) historical and live market data and produces a **market sentiment score from -100 to +100**, where -100 means extreme freeze/panic and +100 means extreme euphoria. Use it to locate where the market sits in the sentiment cycle (freeze → recovery → fermentation → climax → ebb).

> Primary documentation is in Chinese: [../整体方案.md](../整体方案.md) (design) · [../数据说明.md](../数据说明.md) (data dictionary).
>
> ⚠️ For research/education only. The sentiment score is a market thermometer, **not investment advice**.

## ✨ Features

| Module | Description |
|---|---|
| **TDX formulas** ([../../tdx/](../../tdx/)) | Zero-dependency TongDaXin sub-chart formulas: V1 (price/volume) and V2.1 (breadth-enhanced: cross-symbol advance/decline reference, asymmetric volume direction, percentile RSI), plus a companion limit-up ecosystem stock screener |
| **Python scoring engine** ([../../v2/](../../v2/)) | 15 components in 5 groups (limit-up structure / volume / momentum / position / volatility), 250-day rolling percentile normalization, weighted sum + EMA smoothing; direct multi-source data client (THS limit-up review + EastMoney/Tencent index kline with automatic fallback) |
| **Interactive viewer** | Self-contained HTML: 5 synchronized panels (candlestick, volume, sentiment score, limit-up/down counts, open-board/relay rates), crosshair with a side panel showing the **full component breakdown** for the hovered day, wheel zoom, works offline |
| **Local admin console** | Data status dashboard, one-click incremental update with live logs, visual weight editing (per-run or save-as-default), publish generated views to `out/publish/` |
| **Validation** | Extreme-day anchor replay (8/8 hits), information coefficient (IC), quintile forward-return monotonicity |

## 📺 Demo

- **Live demo**: <https://zhongbinling.github.io/stock_analyze/> — open directly in the browser, no clone needed (GitHub Pages, auto-deployed from the `gh-pages` branch).
- **In-repo snapshot**: [../../demo/sentiment_viewer.html](../../demo/sentiment_viewer.html) — generated from data through 2026-10-14; double-click to open offline after cloning.

## 🚀 Quick Start

```bash
# Python 3.10+
git clone <your-repo-url> stock_analyze
cd stock_analyze/v2
pip install -r requirements.txt

# Option A: admin console (recommended) — http://127.0.0.1:8501
python admin.py --open

# Option B: CLI
python fetch_daily.py        # first backfill ≈240 trading days (8-12 min, resumable)
python report.py             # daily text report
python viewer.py --open      # interactive viewer
python backtest.py           # anchor / IC / quintile checks
```

TDX users: paste [../../tdx/QXXF_情绪分V1.txt](../../tdx/QXXF_情绪分V1.txt) or the V2.1 formula into the TDX formula manager (usage notes in the file header).

## 🧠 Model at a Glance

Each component → **250-day rolling percentile** → centered to ±100 → weighted sum → EMA(3) → clamped.

| Group | Components | Weight |
|---|---|---|
| Limit-up structure | limit-up count, limit-down count, open-board rate, max consecutive boards, relay rate (THS source) | 20% |
| Volume | market volume, 5/20 volume ratio (**asymmetric direction**: expanding volume × day direction, shrinking volume always negative) | 20% |
| Momentum | percentile RSI(14), daily change, opening gap, candle-body strength | 28% |
| Position | 20d/60d bias, 250-day price position | 20% |
| Volatility | amplitude × day direction | 12% |

Bands: ≤-80 extreme freeze · ≤-60 freeze · ≤-30 ebb · ±10 neutral · ≥30 fermentation · ≥60 euphoria · ≥80 extreme euphoria.

Anchor validation: 2024-02-05 micro-cap crash -61, 2024-09-30/10-08 +81/+85, 2015 crash -39 — 8/8 directional hits.

## 🗺 Roadmap

**Done** — TDX formulas, Python scoring engine with dual sources, interactive viewer, admin console, anchor validation, bilingual docs, demo artifact.

**Planned**
- [ ] Prettier & more usable admin console: job history/queue, chart-based status, scheduled auto-update, dark mode
- [ ] Load richer base data from tushare: longer limit-up history (from 2019), historical advance/decline counts, margin balance; configurable token
- [ ] Richer views: component toggles, custom panels, multi-index comparison, score-vs-forward-return overlay
- [ ] Aggressive viewer performance: payload compression (chunking/delta encoding), lazy segment loading, Web Worker rendering
- [ ] Intraday real-time (V3): minute-level refresh + threshold alerts via WeCom/DingTalk webhook

## 📄 License

[MIT](../../LICENSE). Bundled ECharts is Apache-2.0 ([../../v2/vendor/](../../v2/vendor/)).
