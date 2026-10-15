# 更新日志 / Changelog

本项目的所有重要变更记录于此。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布] 第一阶段 · 多用户行情服务（开发中）

### S3 视图管理 + 用户行情页 - 2026-10-15
- 视图管理页 /admin/views(≥5运营)：环境/发布状态/数据日期/规则名筛选，预览/发布/下线/删除，
  产线规则自动发布开关(与数据生成页≥7分权)
- 用户行情页 /mkt(≥1)：默认最新已发布产线情绪视图(iframe+头栏信息)，?rid=指定，
  历史视图列表(≥2)，空态引导
- /mkt/view/<rid> 仅服务 published 产物(路径安全校验)；发布语义: 仅prod可发布，test/失败/无文件拒绝
- view_runs 增加 env/vtype 快照列(存量回填)；生成时写入
- 修复 require_level 的API判定(含/admin/api/*前缀, 越权返回JSON而非HTML)
- 审计: view_publish/view_unpublish/view_delete/def_autopublish

### S2 数据管理 + 生成任务 - 2026-10-15
- 任务系统 server/jobs.py：单工作线程队列(上限20) + 子进程调用 core CLI + APScheduler 定时(策略热更新)
- 数据管理页 /admin/data：数据集总览(5类, 含缺失天数健康度)、手动增量更新(实时日志)、
  更新策略(自动更新/生成与时间)、数据源管理(启停/tushare token 掩码)、任务历史
- 数据生成页 /admin/generate：生成规则 CRUD、15分量权重编辑器、立即生成、生成历史与文件预览
- 视图产物落 views_store/{env}/{type}/{日期}/；产线+自动发布 → published=1(S3 用户可见)；测试环境永不发布
- 定时生成带交易日守卫(行情最新日期==今天)；手动生成不受限
- 新表: jobs/view_defs/view_runs/data_sources/app_settings
- 移除过渡期旧管理台 core/admin.py(功能已并入 server)
- 修复: viewer.py 输出目录多级创建(mkdir parents)；种子数据在迁移前跳过

### S1 账号与权限 - 2026-10-15
- 认证：用户登录/注册(等级2)/匿名会话(等级1)、管理登录(≥5)、登出；登录限速(5次失败锁10分钟)
- 权限：require_level 装饰器 + 9级矩阵(1/2/5/7/9)，越权403页，导航按登录态/等级动态渲染
- 账号管理(9)：列表/新增(2/3/5/7/9)/停用启用/重置密码/软删除(禁删自己与最后一个可用9级)
- 账号信息(2)：昵称修改、改密、等级展示、上次登录；设置(1)：主题账号级持久化
- 审计：admin_log 表记录登录/注册/账号操作/改密；工作台与账号管理页展示
- bootstrap CLI 建超管(支持管道输入)；表单字段错误渲染修复；迁移: admin_log + users.pref_theme
- 自测35项全通过(权限矩阵/限速/CSRF/审计/主题持久化)
- Review修复: 开放重定向(next白名单)/软删除用户名释放可复用/停用9级删除误拦/重置密码去JS依赖/管理登录被拒落审计/昵称错误渲染

### S0 架构重构与服务骨架 - 2026-10-15
- `v2/` 重命名为 `core/`（git 历史保留），文档路径全量更新
- 新增 `server/` Web 服务骨架：Flask 应用工厂 + Flask-SQLAlchemy / Flask-Login /
  Flask-Bcrypt / Flask-WTF(CSRF) / Flask-Migrate 五扩展初始化
- `python -m server`（waitress）启动；首页 + `/healthz` + 主题切换 API
- 昼/夜双主题（dataviz 参考色板），localStorage + 会话双持久化，CSRF 令牌全站
- `config/server.yaml` 服务配置拆分；根级全量 `requirements.txt`
- app.db 首版迁移（users 表雏形，Flask-Bcrypt 密码哈希）
- 旧 `core/admin.py` 标记过渡期（默认端口改 8599，S2 移除）；`docs/部署手册.md` 草稿

## [0.5.0] - 2026-10-15

仓库化与文档体系。

- 项目整理为规范 git 仓库（stock_analyze），按私有项目管理
- 双语文档：中文主 README + English README/DATA.md（docs/en/）
- 新增 LICENSE(MIT)、CHANGELOG、.gitignore；整体方案/数据说明归档至 docs/
- 新增 demo/：可直接双击打开的情绪视图演示快照（数据截至 2026-10-14）

## [0.4.0] - 2026-10-14

本地管理台。

- 新增 `v2/admin.py`：数据状态一览（各数据集最新日期/行数/缺失天数告警）、
  一键增量更新（抓取→重算，实时日志，单任务队列）、
  可视化权重设置（仅本次生成 / 保存为默认 config_override.yaml / 一键恢复）、
  视图发布（按时间戳生成 out/publish/ 并刷新 index.html）
- `score.py` 支持配置覆盖（config_override.yaml 与 SENTIMENT_OVERRIDE 环境变量）
- `viewer.py` 重构出 `generate_html()` 供管理台调用
- 新增 docs/数据说明.md（图表口径/分量字典/数据库表/常用 SQL）

## [0.3.0] - 2026-10-14

交互式情绪视图 + 评分口径对齐通达信 V2.1。

- 新增 `v2/viewer.py`：自包含 HTML 交互视图（内嵌 ECharts 与全量数据，离线可用），
  五联面板十字光标联动，右侧面板悬停显示当日全部构成指标，滚轮缩放 + 区间滑块
- 评分口径与用户通达信公式 QXXF_情绪分V2.1 对齐：
  - RSI 线性改分位化（修复恐慌日隐性下限）
  - 量能分量非对称方向化（放量×方向、缩量恒负/涨减半；修复"缩量下跌被错误翻正"）
  - 新增跳空缺口、K 线实体强度、60 日乖离分量，权重结构改为 20/20/28/20/12
- 锚点校验：2024-02-05 微盘危机 -28→**-61**、2015 股灾 -39、8/8 方向命中；IC -0.030→-0.047

## [0.2.0] - 2026-10-12

Python 评分系统。

- 数据层：自研直连客户端 `em_api.py`（东财K线→腾讯回退；同花顺涨停复盘主源+东财兜底），
  绕开 akshare 在本机网络下被东财 WAF 断连的问题；发现东财涨停池历史仅约 1 个月，改用同花顺约一年滚动历史
- 指标与评分：metrics/score 分层，12 分量分位标准化加权合成，缺失分量自动归一化
- 涨停"昨日涨停表现"无历史源 → 以接力率（今日2板+家数/昨日涨停家数）作赚钱效应代理
- SQLite 存储（index_daily/mood_raw/sentiment），fetch_daily 断点续跑 + 防限流
- 文本日报 report.py、静态图、锚点/IC/五分组校验 backtest.py

## [0.1.0] - 2026-10-11

初始版本。

- 整体方案（模型设计/版本路线图/校准方法/风险）
- 通达信公式 V1（QXXF 情绪分·指数基础版，7 分量纯量价，零版本依赖）

## 用户贡献（TDX 侧）

- QXXF_情绪分V2（V2.1 广度增强版）：两市涨跌家数跨品种引用、量能非对称方向化、
  RSI 分位化、跳空缺口/K线实体/60日乖离等新分量，及配套说明文档
- QXXF_涨停生态_配套选股公式
