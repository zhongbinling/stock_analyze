# Data Dictionary (English)

> Condensed from the Chinese [../数据说明.md](../数据说明.md). Field names in code/DB are the authoritative reference.

## Interactive viewer — 5 panels

| # | Panel | Content | Unit / note | Source |
|---|---|---|---|---|
| 1 | Candlestick | SSE Composite daily OHLC, red = up / green = down (CN convention) | points | EastMoney → Tencent fallback |
| 2 | Volume | SH + SZ combined daily volume | 100M shares (亿手) | same |
| 3 | **Sentiment score** | core output, -100…+100, reference lines ±60/±80, red zone = euphoria / blue = freeze | score | 15-component weighted composite |
| 4 | Limit-up/down counts | red bars up = limit-up count, green bars down = limit-down count | stocks | THS limit-up review |
| 5 | Open-board & relay rate | yellow = open-board rate, green = relay rate | % | THS limit-up review |

Panels 4–5 start at **2025-10-15**: the THS endpoint only keeps a rolling ~1 year of history. This is a free-data-source boundary, not a bug; the window fills up as you run daily updates.

Side panel columns: **raw value** (actual number of the day) · **percentile score** (position within trailing 250 days, ±100, shown as a two-way bar) · **weight** (effective weight of the day — missing components are renormalized away) · **contribution** (score × weight).

## The 15 components

| Group | Component | Raw meaning | Direction | Weight |
|---|---|---|---|---|
| Limit-up structure (20%) | zt_count | closing limit-up count (SH main + ChiNext + STAR, ex-ST) | more = hotter | 4% |
| | dt_count | limit-down count | more = colder | 4% |
| | zb_rate | open-board rate = broken / (sealed + broken) | higher = colder | 4% |
| | lb_max | highest consecutive-board count (space board) | higher = hotter | 4% |
| | jl_rate | relay rate = today's ≥2-board count ÷ yesterday's limit-up count | higher = hotter | 4% |
| Volume (20%) | vol_total | combined SH+SZ volume, percentile × day direction (asymmetric: shrinking volume always negative; shrinking on an up day is halved) | | 12% |
| | vr | 5d/20d mean-volume ratio, same direction rule | | 8% |
| Momentum (28%) | rsi | RSI(14), percentile | higher = hotter | 10% |
| | chg | daily % change, percentile | | 8% |
| | gap | open/prev-close - 1 (auction/overnight sentiment) | | 5% |
| | kr | 3-day mean of (close-open)/(high-low) | | 5% |
| Position (20%) | bias20 | close/MA20 - 1 | | 8% |
| | bias60 | close/MA60 - 1 | | 5% |
| | close | position within 250-day high-low range | | 7% |
| Volatility (12%) | amp | amplitude percentile × day direction (big up = hot, big down = panic) | | 12% |

Composite: percentile → center to ±100 → weighted sum → EMA(3) → clamp ±100.

## Database (v2/data/sentiment.db, SQLite)

Open with **DB Browser for SQLite**, a VS Code SQLite extension, or `python -m sqlite3`.

- **index_daily** — index dailies: `code` (`1.000001` SSE, `0.399106` SZ Composite), `date`, OHLC, `volume` (lots), `amount`, `pct_chg`.
- **mood_raw** — one row per trading day: `zt_count/dt_count`, `zb_count/zb_rate`, `lb_max/lb2_count`, `jl_rate`, `yzt_count`, `prev_zt_chg` (EastMoney recent-month only), `up_count/down_count` (latest day only).
- **sentiment** — rebuilt on each recompute: `date`, `score`, `stage`, `components` (JSON of the day's percentile scores).

```sql
-- last 10 days
SELECT date, score, stage FROM sentiment ORDER BY date DESC LIMIT 10;
-- most euphoric days by limit-up count
SELECT date, zt_count, lb_max, jl_rate FROM mood_raw ORDER BY zt_count DESC LIMIT 10;
```

## Generated artifacts (git-ignored)

`v2/out/sentiment_viewer.html` (interactive viewer) · `v2/out/publish/` (timestamped publishable views + `index.html`) · `v2/out/sentiment.csv` · `v2/out/sentiment_history.png`.
`v2/config.yaml` = defaults; `v2/config_override.yaml` = admin-console "save as default" overrides (delete to reset).
