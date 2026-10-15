# stock_analyze · A股市场情绪评分系统

[![docs](https://img.shields.io/badge/docs-中文-brightgreen)](docs/整体方案.md) [![docs](https://img.shields.io/badge/docs-English-blue)](docs/en/README.md) [![demo](https://img.shields.io/badge/demo-在线演示-orange)](https://zhongbinling.github.io/stock_analyze/)

通过分析 A 股历史与实时行情数据，输出**市场情绪分值（-100 ~ +100）**：-100 极度冰点/恐慌，+100 极度狂热。用于判断市场处于情绪周期的哪个位置（冰点 → 修复 → 发酵 → 高潮 → 退潮）。

> **English documentation**: [docs/en/README.md](docs/en/README.md)
>
> ⚠️ 本项目仅供学习研究，情绪分是市场温度计，**不构成投资建议**。

## ✨ 已实现功能

| 模块 | 说明 |
|---|---|
| **通达信公式**（[tdx/](tdx/)） | 零依赖副图公式：V1 量价版 + V2.1 广度增强版（涨跌家数跨品种引用、量能非对称方向化、RSI 分位化），附配套涨停生态选股公式 |
| **Python 评分系统**（[core/](core/)） | 15 分量五组加权（涨停结构/量能/动量/位置/波动），250 日滚动分位标准化，EMA 平滑限幅；双数据源直连客户端（同花顺涨停复盘 + 东财/腾讯指数K线自动回退） |
| **交互式情绪视图**（[core/viewer.py](core/viewer.py)） | 自包含 HTML：K线/成交量/情绪分/涨跌停/炸板率接力率五联面板，十字光标联动，右侧实时显示当日**全部构成指标**，滚轮缩放，可离线分发 |
| **Web 管理端**（`/admin`） | 账号与 9 级权限（S1）；数据管理：数据集总览/一键更新/更新策略/数据源（S2）；数据生成：规则 CRUD/权重编辑/定时生成/历史与预览（S2） |
| **校验体系**（[core/backtest.py](core/backtest.py)） | 极端日锚点回放（8/8 命中）、信息系数 IC、五分组收益单调性 |

## 📺 演示

- **在线演示**：<https://zhongbinling.github.io/stock_analyze/> —— 浏览器直接打开，无需克隆（GitHub Pages，gh-pages 分支自动部署）
- **仓库内快照**：[demo/sentiment_viewer.html](demo/sentiment_viewer.html) —— 2026-10-14 收盘数据、默认权重生成，双击即可离线体验

可体验的交互：五联面板十字光标联动、右侧面板悬停显示当日全部构成指标、滚轮缩放、区间滑块。

## 🚀 快速开始

```bash
# 环境要求: Python 3.10+
git clone <你的仓库地址> stock_analyze
cd stock_analyze
pip install -r requirements.txt        # 根级全量依赖(Web服务+评分引擎)

# ★ 一键部署(推荐, 自动建venv+迁移+建超管+启动): Windows: scripts\deploy_win.bat / mac·linux: ./scripts/deploy.sh
# 服务管理(三平台一致): scripts\service.bat start|stop|status|restart  (sh版同理)

# 或直接运行(依赖已装时): python -m server  → http://127.0.0.1:8501
#   数据库迁移(首次或升级后): python -m flask --app "server:create_app()" db upgrade
#   首次使用先建超管: python -m server.bootstrap --username admin (交互输入密码)

# 评分引擎命令行(core/)
cd core
python fetch_daily.py        # 首次回补约240交易日(8~12分钟, 断点续跑)
python report.py             # 当日情绪日报(文本)
python viewer.py --open      # 交互式视图
python backtest.py           # 锚点/IC/分组校验

```

通达信用户：把 [tdx/QXXF_情绪分V1.txt](tdx/QXXF_情绪分V1.txt) 或 [tdx/QXXF_情绪分V2.txt](tdx/QXXF_情绪分V2.txt) 粘贴进公式管理器即可（见文件头注释）。

## 📁 目录结构

```text
stock_analyze/
├── README.md                 # 本文件(中文主文档)
├── CHANGELOG.md              # 版本历史
├── LICENSE                   # MIT
├── requirements.txt          # 全量依赖(部署用)
├── .gitignore
├── demo/                     # 可直接打开的演示产物
│   └── sentiment_viewer.html
├── docs/
│   ├── 整体方案.md           # 设计文档(模型/架构/路线/风险)
│   ├── 开发计划-第一阶段.md  # 多用户行情服务开发计划(S0~S4)
│   ├── 数据说明.md           # 图表口径/分量字典/数据库表/常用SQL
│   ├── 部署手册.md           # 三平台部署(S4 完善)
│   └── en/                   # English documentation
│       ├── README.md
│       └── DATA.md
├── config/
│   └── server.yaml           # 服务配置(端口/绑定/主题/调度)
├── server/                   # Web 服务(第一阶段)
│   ├── __init__.py           #   应用工厂(Flask+SQLAlchemy+Login+Bcrypt+WTF+Migrate)
│   ├── models.py             #   app.db 模型(users...)
│   ├── routes.py             #   路由(首页/healthz/主题)
│   ├── __main__.py           #   python -m server → waitress
│   └── templates/ static/    #   Jinja2 模板 + CSS/JS(昼夜双主题)
├── migrations/               # Flask-Migrate(Alembic) 数据库迁移
├── views_store/             # 生成的视图文件(test/prod, 运行时生成,不入库)
├── tdx/                      # 通达信公式(零依赖版)
│   ├── QXXF_情绪分V1.txt     #   V1 量价版(7分量)
│   ├── QXXF_情绪分V2.txt     #   V2.1 广度增强版(12分量)
│   ├── QXXF_情绪分V2_说明.md #   V2.1 设计说明
│   └── QXXF_涨停生态_配套选股公式.txt
├── core/                     # 评分引擎库(原 v2/)
│   ├── viewer.py             #   交互视图生成器
│   ├── fetch_daily.py        #   数据采集(断点续跑/防限流)
│   ├── em_api.py             #   数据源直连客户端(多源回退)
│   ├── metrics.py / score.py #   指标计算 / 分位标准化加权合成
│   ├── report.py / backtest.py / admin.py(过渡期)
│   ├── db.py / config.yaml / requirements.txt
│   ├── vendor/               #   echarts.min.js(内嵌用, Apache-2.0)
│   ├── data/                 #   行情SQLite(运行时生成,不入库)
│   └── out/                  #   生成产物(运行时生成,不入库)
└── data/                     # 服务运行时数据(app.db/secret.key, 不入库)
```

## 🧠 评分模型速览

每个分量 → **250 日滚动分位** `（X-最低）/（最高-最低）` → 中心化 ±100 → 加权合成 → EMA(3) → 限幅。

| 分量组 | 内容 | 默认权重 |
|---|---|---|
| 涨停结构 | 涨停家数、跌停家数、炸板率、连板高度、接力率（同花顺源） | 20% |
| 量能 | 两市成交量、量比趋势（**非对称方向化**：放量×方向，缩量恒负） | 20% |
| 动量 | RSI(分位)、当日涨幅、跳空缺口、K线实体强度 | 28% |
| 位置 | 20日乖离、60日乖离、250日价格位置 | 20% |
| 波动 | 振幅×当日方向 | 12% |

分档：≤-80 极度冰点 · ≤-60 冰点 · ≤-30 退潮 · ±10 中性 · ≥30 发酵 · ≥60 狂热 · ≥80 极度狂热。
历史锚点校验：2024-02-05 微盘危机 -61、2024-09-30/10-08 +81/+85、2015 股灾 -39、8/8 方向命中。详见 [docs/整体方案.md](docs/整体方案.md)。

## 🗺 路线图

**已完成**
- [x] V1/V2.1 通达信公式（含广度增强、用户版涨停生态选股公式）
- [x] Python 评分系统 + 双数据源 + 历史锚点校验
- [x] 交互式情绪视图（五联面板、构成指标联动）
- [x] 本地管理台（状态/更新/权重/发布）
- [x] 双语文档 + 演示视图 + 仓库化

**第一阶段：多用户行情服务**（详细计划：[docs/开发计划-第一阶段.md](docs/开发计划-第一阶段.md)）
- [x] S0 架构重构与服务骨架（core/server 拆分、主题、健康检查）✅ 2026-10-15
- [x] S1 账号与权限（登录/注册/匿名、9 级权限矩阵、账号管理、设置）✅ 2026-10-15
- [ ] S2 数据管理 + 生成任务（数据集总览、更新策略、定时任务链、生成规则与历史）
- [x] S3 视图管理 + 用户行情页（发布/下线/自动发布、/mkt 行情访问）✅ 2026-10-15
- [x] S4 服务化与部署（win/mac/linux 启停与一键部署脚本、部署手册）✅ 2026-10-15 **第一阶段完成，v1.0.0**

**第二阶段：板块温度计**（详细计划：[docs/开发计划-第二阶段-板块温度计.md](docs/开发计划-第二阶段-板块温度计.md)）
- [ ] S5 板块数据底座（板块字典/日线采集/涨停行业聚合）
- [ ] S6 温度引擎（纵向温度+横向热度排名+锚定回放）
- [ ] S7 视图与管理集成（sector_heat 视图类型/热力图/发布链//mkt Tab）
- [ ] S8 增强（可选：资金流分量/概念板块/历史回放/选股联动）

**第三阶段：服务运维管理**（概要：[docs/开发计划-第三阶段-服务运维.md](docs/开发计划-第三阶段-服务运维.md)）
- [ ] S9 三环境模型与部署单元（dev/灰度/产线 单机多实例隔离）
- [ ] S10 运维系统（独立运维账号/部署更新/启停/日志/发布流与回滚/审计）

**其他计划**
- [ ] 管理页面更好用美观：任务历史与排队、状态图表化、定时自动更新、深色模式
- [ ] 基础数据支持从 tushare 载入更完整数据：涨停池更早历史（2019 起）、涨跌家数历史序列、融资融券余额，token 配置化
- [ ] 完善视图展示：分量开关与自定义面板、多指数对照、情绪分与未来收益叠加分析
- [ ] 极致视图计算优化：payload 压缩（分块/差分编码）、大数据量分段按需加载、Web Worker 渲染
- [ ] V3 盘中实时：分钟级刷新 + 阈值告警推送（企业微信/钉钉 webhook）

## 📚 文档

| 文档 | 内容 |
|---|---|
| [docs/整体方案.md](docs/整体方案.md) | 设计文档：模型、架构、数据源、校准方法、风险 |
| [docs/开发计划-第一阶段.md](docs/开发计划-第一阶段.md) | 第一阶段（多用户行情服务）详细开发计划 ✅ |
| [docs/开发计划-第二阶段-板块温度计.md](docs/开发计划-第二阶段-板块温度计.md) | 第二阶段（板块温度计）详细开发计划 |
| [docs/开发计划-第三阶段-服务运维.md](docs/开发计划-第三阶段-服务运维.md) | 第三阶段（服务运维管理）概要 |
| [docs/数据说明.md](docs/数据说明.md) | 图表口径、15 分量字典、数据库表结构与常用 SQL |
| [CHANGELOG.md](CHANGELOG.md) | 版本历史 |
| [docs/en/README.md](docs/en/README.md) | English overview & usage |
| [docs/en/DATA.md](docs/en/DATA.md) | Data dictionary (English) |

## 📄 许可证

[MIT](LICENSE)（内嵌的 ECharts 为 Apache-2.0 许可，见 [core/vendor/](core/vendor/)）。
