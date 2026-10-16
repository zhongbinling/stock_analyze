# 更新日志 / Changelog

本项目的所有重要变更记录于此。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布] 第2.2阶段 · 排版/数据/操作优化（开发中）

### S2.2-C 操作优化 - 2026-10-17
- viewer tooltip: 悬停时追加V1对照/环比/分量明细(RSI/量能/乖离/涨停/振幅分位分)
- 数据管理页更新按钮: 点击立即disabled+文字变更+日志占位; poll()根据running/queued恢复

## [未发布] 第二阶段 · 板块温度计

### S2.1-C 内容扩展 - 2026-10-17 · 第2.1阶段完成
- 概念板块默认启用(519个): 次日采集+温度计算自动包含, 排行可搜"AI""机器人"等主题
- 申万一级聚合: core/sw1_map.py(成分股投票+人工映射, 73个BK→28个SW L1) + sector.py calc_sw1
- SW1聚合温度写入sector_scores(code='SW1_名称'), sector_viewer自动展示
- 锚点11/11保持

### S2.1-A 排版优化 - 2026-10-16
- sector_viewer 两行布局: 上行信息聚合(Top5热冷卡片+大盘sparkline) + 下行分析(排行+多板块曲线≤5条)
- viewer 两列面板布局; /mkt iframe自适应+全屏; 修复side div被clear()删除

### S2.1-B 数据补全 - 2026-10-16
- viewer.py: years参数只截断主图(K线/情绪分), 附图(涨停/涨跌/炸板/接力)全量嵌入;
  统一全量日期轴+主图null填充+dataZoom默认近2年窗口可拖全历史
- 涨跌家数历史回补: tushare daily API → mood_raw.up/down_count, 2018-01-02起2130日
  (锚点验证: 2020-07-09 3443↑/421↓ ✓ 2024-10-08 5030↑/291↓ ✓)
- 主链增量: fetch_breadth_today_from_tushare(1请求/日)
- 修复: ISO日期转换(同ts-backfill bug)、daily_basic→daily(pct_chg字段)

### S8 增强 - 2026-10-16 · 第二阶段完成
- fflow 资金流分量入模: 板块主力净流入60日分位(权重5%, 权重微调 pos 7→5/bias60 5→4/vr 8→9/
  zt_share 6→5/ud_ratio 6→4); 近期有值, 早期自动降权
- 概念板块批量开关: POST /admin/api/sectors/concept-toggle(≥7级), 519个概念板块一键启停
- export CLI: python sector.py export --top N → CSV(含14列分量明细), 可与tdx涨停生态公式对照
- 锚点11/11保持; 第二阶段S5~S8全部完成

### S7 板块温度视图与管理集成 - 2026-10-16
- core/sector_viewer.py: 自包含HTML(2.7MB)——大盘情绪对照条+日期滑块+当日温度排行(条形热红冷蓝)+
  板块温度曲线(点击联动)+侧栏分量明细(双向条形); 搜索/最热/最冷筛选; 昼夜主题
- view_defs type 新增 sector_heat(topN/years参数); task_generate 按type分发
- 数据生成页: 类型选择+类型列+板块温度权重提示; /mkt Tab切换(情绪|板块温度) + latest API type参数
- 验收: 产线规则→生成→自动发布→匿名/mkt切Tab可见; 无头浏览器渲染(canvas×2+无JS错误)

### S6 板块温度引擎 - 2026-10-16
- core/sector.py: 纵向温度(13分量五组, 250日分位→加权→EMA→限幅, 与大盘情绪同构)+
  横向热度(当日全板块百分位); calc/report/anchors/remap CLI
- 493板块×2130日×102万行, 整表49s; sector_scores表(components JSON+stage+rank_pct)
- 预处理: @行业名自动前缀匹配27个; 旧东财粗分类@行清理(23668行, BK数据100%覆盖)
- 锚定回放 7/7 全中(2024-10-08半导体+71 / 2024-02-05软件-82); 横向有效性: 前10%次日+0.06%>后10%+0.04%
- 主链集成: task_update 第三步 sector calc; 数据管理页新增板块温度数据集行

### tushare 完整数据接入(5100积分) - 2026-10-16
- limit_list_d 涨跌停名单(2020年起): 历史回补 mood_raw 1642日(2020-01-02起, 不覆盖THS段)+
  sector_pool_stat 行业聚合 64826行(自带industry字段, 板块涨停历史从1个月扩展到近7年)
- 当日主链新增 fetch_tushare_mood_today: 每日以tushare口径刷新mood+板块聚合(与THS互为校验,
  逐步统一口径); fetch_margin: 两融汇总2020-05起1555日(最新2.50万亿)
- 板块全量回补完成(东财解封后): 清单1000(行业493+概念507)、日线102万行(2018起)、
  资金流493板块; 行业名映射 BK105/@98(去Ⅰ/Ⅱ/Ⅲ后缀匹配)
- 锚点回放 8/8 全中(扩展窗口后: 2024-02-05 -57.1 / 2024-10-08 +80.2)
- 修复: ts-backfill 起始日YYYYMMDD未转ISO(2020年被字符串比较误排除); industry NaN穿透
  _resolve/_write_pool_agg 类型防御

### tushare 数据源适配 - 2026-10-16
- core/tushare_api.py: pro 接口薄客户端(token读 core/data/tushare_token.txt 或环境变量, 限频节流)
- 指数日线三重回退: 东财→腾讯→tushare(_ts_kline, amount千元→元)
- 交易日历: fetch_calendar 同步近3年+未来日历 → calendar 表;
  交易日守卫升级为"日历前验优先, 无日历回退行情后验"
- token 管理闭环: 数据管理页 tushare 保存 → 同步写 core/data/tushare_token.txt(不入git);
  数据集状态新增"交易日历(tushare)"行
- 本token实测权限: trade_cal/index_daily/daily_basic可用; limit_list_d需2000积分(未接入, 留待升级)

### S5 板块数据底座 - 2026-10-16
- 新表×6: sectors(行业~500细分到三级+概念~500默认停用)/sector_daily/sector_pool_stat/
  sector_fundflow/sector_alias；core/sectors.py 采集编排(清单/增量K线/资金流周频/涨停行业聚合)
- em_api: 板块清单(clist+编号镜像轮换)/板块K线(90.BKxxxx)/资金流历史(fflow)；
  ths_pool_stats 支持 with_industry(东财三池 hybk+lbc 口径)；_get 增加 curl 子进程兜底
  (东财WAF按TLS指纹封锁requests时, curl指纹可用)
- @行业名 优雅降级落库 + remap_pending 自动重映射(清单可用后)；回补/刷新带连续失败熔断
- fetch_daily 主链集成(--no-sectors 可跳过)；数据管理页新增板块数据组(4数据集)
- 采集期遇东财 IP/TLS指纹封锁(push2/push2his): 池聚合719行(近30天,107行业)已回补;
  板块清单/日线/资金流待封锁解除后 `cd core && python sectors.py daily` 一次完成(自动回补+重映射)
- 口径修正: 行业板块实为~500个(细分到三级, 原估86)；涨停结构分量历史=东财池约1个月起积累
  (行业字段hybk仅东财池携带, 10jqka池无), 其余78%权重量价分量多年历史不受影响

## [1.0.0] - 2026-10-15 · 第一阶段完整交付

### S4 服务化与部署
- scripts/svc.py 服务管理器(一份逻辑三平台): start(幂等迁移+后台启动+健康等待)/stop/status/restart/
  install/uninstall(开机自启: win=HKCU Run键 / linux=systemd / mac=launchd)
- service.bat / service.sh 薄包装; deploy_win.bat 与 deploy.sh(deploy_mac/linux.sh入口) 一键部署
  (检查Python→venv→依赖→迁移→无超管时交互创建→询问自启→启动→健康检查)
- 部署手册中文全量重写 + docs/en/DEPLOY.md 英文版
- healthz 增加 version; server.__version__=1.0.0
- Windows 本机全流程验收: service start/status/浏览器匿名/mkt/restart/stop + deploy 真跑(venv)
  + 自启真装真卸; sh 脚本 bash -n 语法校验
- 修复: bat 需 CRLF 行尾(LF+中文注释会解析错乱)+chcp 65001; svc.py 输出 UTF-8;
  schtasks ONLOGON 需管理员 → 改 HKCU Run 键; deploy set /p 中文提示管道兼容


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
