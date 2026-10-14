# 演示视图

**[sentiment_viewer.html](sentiment_viewer.html)** —— 用 2026-10-14 收盘数据、默认权重生成的情绪视图静态快照。

**无需安装任何东西**：克隆仓库后直接双击用浏览器打开（页面内嵌了全部数据与图表库，离线可用）。

可以体验的交互：
- 鼠标在任一面板移动 → 五联面板十字光标联动（上证K线 / 两市成交量 / 情绪分 / 涨停跌停家数 / 炸板率·接力率）
- 右侧面板实时显示悬停日的**全部 15 个构成指标**（原始值 / 分位分条形 / 权重 / 贡献）
- 滚轮缩放、底部滑块选择区间、悬停浮动提示

说明：
- 2025-10-15 之前下方面板（涨停结构）无数据属正常——免费数据源只保留约一年滚动历史
- 要生成最新数据的视图请运行 `core/viewer.py`，或在管理台 `python core/admin.py` 中点"生成视图"

## 更新线上演示（GitHub Pages）

线上地址：<https://zhongbinling.github.io/stock_analyze/>（gh-pages 分支自动部署，`index.html` 即演示页）。
想用最新数据刷新线上演示时：

```bash
cd core && python fetch_daily.py && python viewer.py   # 生成最新视图到 out/sentiment_viewer.html
cd ..
git checkout gh-pages
cp core/out/sentiment_viewer.html index.html
git commit -am "deploy: 更新演示快照" && git push
git checkout main
```
