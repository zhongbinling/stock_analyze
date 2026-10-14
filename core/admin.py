# -*- coding: utf-8 -*-
"""本地管理台(过渡期工具): 数据状态一览 / 一键更新 / 权重调整 / 情绪视图发布

⚠ 过渡期说明: 第一阶段 S2 交付 server 数据管理/生成/视图管理页面后, 本文件将被移除。
  新的正式服务: python -m server (端口 8501); 本工具默认端口已改为 8599。

用法:
  python admin.py            # http://127.0.0.1:8599
  python admin.py --open

功能:
  1) 数据状态: 指数K线/涨停池/情绪分 各自的最新日期、行数、缺失天数
  2) 一键更新: 增量抓取(指数+涨停池) → 重算评分入库, 带实时日志
  3) 权重设置: 页面调整15个分量权重/窗口参数 → 用当前设置生成交互视图
     (可选择保存为默认, 写入 config_override.yaml)
  4) 视图发布: 生成 out/publish/sentiment_日期.html + index.html(最新副本),
     整个 publish 文件夹可直接拷走发布
"""
import argparse
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import yaml  # noqa: E402
from flask import Flask, jsonify, request  # noqa: E402

from db import connect, DB_PATH  # noqa: E402
from score import load_cfg  # noqa: E402
from metrics import LABELS  # noqa: E402

app = Flask(__name__)
PUBLISH = BASE / "out" / "publish"

# ---------------------------------------------------------------- 任务(单任务队列)

JOB = {"running": False, "kind": "", "log": [], "rc": None, "started": None, "done": None}
JOB_LOCK = threading.Lock()


def _worker(steps, env_extra, after=None):
    """依次执行 steps=[[cmd,...],...], 行式收集日志; rc=最后一步退出码"""
    rc = 0
    for cmd in steps:
        JOB["log"].append(f"$ {' '.join(str(c) for c in cmd)}")
        env = None
        if env_extra:
            import os
            env = {**os.environ, **{k: str(v) for k, v in env_extra.items()}}
        p = subprocess.Popen([str(c) for c in cmd], cwd=BASE, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace")
        for line in p.stdout:
            JOB["log"].append(line.rstrip())
            del JOB["log"][:-300]
        rc = p.wait()
        if rc != 0:
            JOB["log"].append(f"! 步骤失败(退出码 {rc}), 中止")
            break
    if rc == 0 and after:
        try:
            after()
        except Exception as e:  # noqa: BLE001
            JOB["log"].append(f"! 后处理失败: {e}")
            rc = 1
    JOB["rc"] = rc
    JOB["running"] = False
    JOB["done"] = time.time()


def start_job(kind, steps, env_extra=None, after=None):
    with JOB_LOCK:
        if JOB["running"]:
            return False, "已有任务在运行, 请等待完成"
        JOB.update(running=True, kind=kind, log=[], rc=None,
                   started=time.time(), done=None)
    threading.Thread(target=_worker, args=(steps, env_extra, after), daemon=True).start()
    return True, f"已启动: {kind}"


# ---------------------------------------------------------------- 状态

def _fmt_ts(p):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))
    except OSError:
        return "--"


def status_payload():
    cfg = load_cfg()
    conn = connect()
    today = time.strftime("%Y-%m-%d")

    idx = {code: {"rows": n, "first": lo, "last": hi}
           for code, n, lo, hi in conn.execute(
               "SELECT code,COUNT(*),MIN(date),MAX(date) FROM index_daily GROUP BY code")}

    mood_n, mood_last = conn.execute(
        "SELECT COUNT(*),MAX(date) FROM mood_raw WHERE zt_count IS NOT NULL").fetchone() or (0, None)
    ps = cfg["pool_start"]
    pool_start = f"{ps[:4]}-{ps[4:6]}-{ps[6:8]}"
    have = {r[0] for r in conn.execute("SELECT date FROM mood_raw WHERE zt_count IS NOT NULL")}
    missing = [d for (d,) in conn.execute(
        "SELECT date FROM index_daily WHERE code=? AND date>=? ORDER BY date",
        (cfg["index"]["primary"], pool_start)) if d not in have and d <= today]

    sent = conn.execute(
        "SELECT date,score,stage FROM sentiment ORDER BY date DESC LIMIT 1").fetchone()

    artifacts = {}
    for name, p in [("交互视图", BASE / "out" / "sentiment_viewer.html"),
                    ("CSV导出", BASE / "out" / "sentiment.csv"),
                    ("静态图", BASE / "out" / "sentiment_history.png")]:
        artifacts[name] = {"exists": p.exists(), "mtime": _fmt_ts(p), "size_kb": round(p.stat().st_size / 1024) if p.exists() else 0}
    pubs = sorted(PUBLISH.glob("sentiment_*.html")) if PUBLISH.exists() else []
    artifacts["发布目录"] = {"exists": PUBLISH.exists(),
                            "count": len(pubs),
                            "latest": pubs[-1].name if pubs else "--"}

    override = BASE / "config_override.yaml"
    return {
        "db": {"path": str(DB_PATH), "size_kb": round(DB_PATH.stat().st_size / 1024) if DB_PATH.exists() else 0},
        "index": idx,
        "pool": {"rows": mood_n, "last": mood_last,
                 "missing_days": len(missing),
                 "missing_recent": missing[-5:],
                 "start": pool_start},
        "sentiment": {"last": sent[0] if sent else None,
                      "score": sent[1] if sent else None,
                      "stage": sent[2] if sent else None},
        "artifacts": artifacts,
        "override": {"exists": override.exists(), "mtime": _fmt_ts(override)},
        "today": today,
    }


# ---------------------------------------------------------------- API

@app.get("/api/status")
def api_status():
    return jsonify(status_payload())


@app.get("/api/weights")
def api_weights():
    cfg = load_cfg()
    import yaml as _y
    with open(BASE / "config.yaml", encoding="utf-8") as f:
        defaults = _y.safe_load(f)
    return jsonify({
        "effective": cfg["weights"],
        "defaults": defaults["weights"],
        "window": cfg["window"],
        "pool_min_periods": cfg.get("pool_min_periods", 60),
        "labels": {k: LABELS.get(k, k) for k in cfg["weights"]},
    })


@app.post("/api/update")
def api_update():
    ok, msg = start_job("增量更新数据", [
        [sys.executable, "-u", "fetch_daily.py"],
        [sys.executable, "-u", "score.py"],
    ])
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 409)


@app.post("/api/generate")
def api_generate():
    body = request.get_json(force=True) or {}
    weights = {k: float(v) for k, v in (body.get("weights") or {}).items() if v is not None}
    over = {}
    if weights:
        over["weights"] = weights
    for k in ("window", "pool_min_periods"):
        if body.get(k):
            over[k] = int(body[k])
    if body.get("save_default"):
        (BASE / "config_override.yaml").write_text(
            yaml.safe_dump(over if over else {}, allow_unicode=True, sort_keys=False), encoding="utf-8")

    years = body.get("years")
    ts = time.strftime("%Y%m%d_%H%M")
    out_file = PUBLISH / f"sentiment_{ts}.html"
    env_extra = {}
    tmp_override = BASE / "data" / "_tmp_override.yaml"
    if over and not body.get("save_default"):
        tmp_override.parent.mkdir(exist_ok=True)
        tmp_override.write_text(yaml.safe_dump(over, allow_unicode=True, sort_keys=False), encoding="utf-8")
        env_extra["SENTIMENT_OVERRIDE"] = tmp_override
    steps = [[sys.executable, "-u", "score.py"],
             [sys.executable, "-u", "viewer.py", "--out", out_file]
             + (["--years", str(int(years))] if years else [])]

    def after():
        if tmp_override.exists() and not body.get("save_default"):
            tmp_override.unlink()
        shutil.copyfile(out_file, PUBLISH / "index.html")

    ok, msg = start_job("生成情绪视图", steps, env_extra=env_extra, after=after)
    return jsonify({"ok": ok, "msg": msg, "out": str(out_file)}), (200 if ok else 409)


@app.post("/api/weights/reset")
def api_weights_reset():
    p = BASE / "config_override.yaml"
    if p.exists():
        p.unlink()
        return jsonify({"ok": True, "msg": "已删除 config_override.yaml, 恢复默认权重"})
    return jsonify({"ok": True, "msg": "本就没有覆盖文件"})


@app.get("/api/job")
def api_job():
    return jsonify({"running": JOB["running"], "kind": JOB["kind"], "rc": JOB["rc"],
                    "log": "\n".join(JOB["log"][-40:]),
                    "elapsed": round((JOB["done"] or time.time()) - JOB["started"], 1) if JOB["started"] else 0})


# ---------------------------------------------------------------- 页面(内嵌)

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>市场情绪系统 · 管理台</title>
<style>
  :root { --surface:#fcfcfb; --page:#f9f9f7; --ink:#0b0b0b; --ink2:#52514e;
          --muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7; --blue:#2a78d6;
          --red:#e34948; --aqua:#1baf7a; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--page); color:var(--ink); font:13px/1.6 system-ui,
         -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }
  header { display:flex; gap:16px; align-items:baseline; padding:14px 22px 10px;
           border-bottom:1px solid var(--grid); flex-wrap:wrap; }
  header h1 { font-size:16px; margin:0; }
  header .sub { color:var(--ink2); }
  main { padding:16px 22px 60px; max-width:1180px; }
  section { background:var(--surface); border:1px solid var(--grid); border-radius:10px;
            padding:14px 18px; margin-bottom:16px; }
  section h2 { font-size:13px; color:var(--ink2); margin:0 0 10px; font-weight:600; }
  table { border-collapse:collapse; width:100%; }
  th, td { padding:5px 8px; border-bottom:1px solid var(--grid); text-align:left;
           font-variant-numeric:tabular-nums; white-space:nowrap; }
  th { color:var(--muted); font-weight:500; font-size:12px; }
  .ok { color:var(--aqua); } .warn { color:#c98500; } .bad { color:var(--red); }
  button { border:1px solid var(--axis); background:var(--surface); color:var(--ink);
           border-radius:8px; padding:7px 16px; font-size:13px; cursor:pointer; }
  button:hover { border-color:var(--blue); color:var(--blue); }
  button.primary { background:var(--blue); border-color:var(--blue); color:#fff; }
  button.primary:hover { opacity:.9; color:#fff; }
  button:disabled { opacity:.45; cursor:not-allowed; }
  .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin:6px 0; }
  pre#log { background:#0d0d0d; color:#c3c2b7; border-radius:8px; padding:10px 12px;
            font:11.5px/1.5 Consolas,monospace; max-height:220px; overflow:auto;
            white-space:pre-wrap; display:none; margin-top:10px; }
  input[type=number] { width:76px; padding:4px 6px; border:1px solid var(--axis);
                       border-radius:6px; font:inherit; }
  .chip { display:inline-block; padding:1px 10px; border-radius:11px; font-size:12px;
          background:var(--grid); color:var(--ink2); }
  .hero { font-size:30px; font-weight:700; }
  .hint { color:var(--muted); font-size:12px; }
</style>
</head>
<body>
<header>
  <h1>市场情绪系统 · 管理台</h1>
  <span class="sub" id="dbinfo">--</span>
  <span class="sub">最新情绪分 <b class="hero" id="latestScore">--</b>
  <span class="chip" id="latestStage">--</span> <span class="sub" id="latestDate"></span></span>
</header>
<main>

<section>
  <h2>① 数据状态</h2>
  <table id="statTable"><tbody><tr><td>加载中…</td></tr></tbody></table>
  <p class="hint">涨停池数据源(同花顺)仅保留约一年滚动历史, 2025-10 之前无数据属正常;
  "缺失交易日"指数据源范围内应抓未抓的日子, 大于0时可点"增量更新"补齐。</p>
</section>

<section>
  <h2>② 数据更新</h2>
  <div class="row">
    <button class="primary" id="btnUpdate">增量更新数据(指数+涨停池+重算评分)</button>
    <span class="hint" id="jobState"></span>
  </div>
  <pre id="log"></pre>
</section>

<section>
  <h2>③ 权重设置与视图生成</h2>
  <div class="row">
    <span>标准化窗口N <input type="number" id="window" step="10"></span>
    <span>池分量起步窗口 <input type="number" id="pmp" step="10"></span>
    <span>视图年限(空=全部) <input type="number" id="years" placeholder="全部"></span>
  </div>
  <table id="wTable"><thead><tr><th>分量</th><th>权重</th><th>有效占比</th></tr></thead>
    <tbody></tbody></table>
  <div class="row">
    <button class="primary" id="btnGenerate">用当前设置生成视图 → out/publish/</button>
    <button id="btnSave">保存为默认(写覆盖文件)</button>
    <button id="btnReset">恢复默认权重</button>
    <span class="hint">权重和:<b id="wSum">--</b>(≠1会自动归一化; 缺数据的日子的权重自动重分配)</span>
  </div>
</section>

</main>
<script>
const $ = id => document.getElementById(id);
let wData = null, timer = null;

async function refreshStatus() {
  const s = await (await fetch("/api/status")).json();
  $("dbinfo").textContent = `数据库 ${s.db.path} (${s.db.size_kb}KB)` +
    (s.override.exists ? ` · 权重覆盖文件存在(${s.override.mtime})` : "");
  if (s.sentiment.last) {
    const sc = s.sentiment.score;
    $("latestScore").textContent = (sc >= 0 ? "+" : "") + sc;
    $("latestScore").style.color = sc >= 0 ? "var(--red)" : "var(--blue)";
    $("latestStage").textContent = s.sentiment.stage;
    $("latestDate").textContent = s.sentiment.last;
  }
  const idxRows = Object.entries(s.index).map(([c, v]) =>
    `${c}: ${v.rows}行 (${v.first}~${v.last})`).join("<br>");
  const missCls = s.pool.missing_days > 3 ? "warn" : "ok";
  $("statTable").innerHTML = `
    <tr><th>数据集</th><th>规模</th><th>范围/最新</th><th>状态</th></tr>
    <tr><td>指数日线</td><td>${idxRows}</td>
        <td class="ok">正常</td></tr>
    <tr><td>涨停池(同花顺口径)</td><td>${s.pool.rows} 天</td><td>最新 ${s.pool.last || "--"} (起点${s.pool.start})</td>
        <td class="${missCls}">缺失 ${s.pool.missing_days} 天${s.pool.missing_recent.length ? " " + s.pool.missing_recent.join(",") : ""}</td></tr>
    <tr><td>情绪分(已入库)</td><td colspan="2">最新 ${s.sentiment.last || "--"} ${s.sentiment.score ?? "--"} [${s.sentiment.stage || "--"}]</td>
        <td class="ok">--</td></tr>
    <tr><td>产物文件</td><td colspan="2">${Object.entries(s.artifacts).map(([k, v]) =>
        `${k}: ${v.exists ? (v.mtime + (v.size_kb ? " " + v.size_kb + "KB" : (v.count !== undefined ? " " + v.count + "个" : ""))) : "无"}`).join("<br>")}</td>
        <td class="hint">publish/index.html 为最新发布视图</td></tr>`;
}

async function refreshWeights() {
  wData = await (await fetch("/api/weights")).json();
  $("window").value = wData.window; $("pmp").value = wData.pool_min_periods;
  const tb = $("wTable").querySelector("tbody");
  tb.innerHTML = "";
  for (const [k, w] of Object.entries(wData.effective)) {
    const tr = document.createElement("tr");
    const isDefault = wData.defaults[k] === w;
    tr.innerHTML = `<td>${wData.labels[k] || k}${isDefault ? "" : ' <span class="chip">已改</span>'}</td>
      <td><input type="number" step="0.01" data-k="${k}" value="${w}"></td>
      <td class="eff"></td>`;
    tb.appendChild(tr);
  }
  tb.querySelectorAll("input").forEach(i => i.addEventListener("input", updateSum));
  updateSum();
}
function updateSum() {
  let sum = 0; const inputs = [...$("wTable").querySelectorAll("input")];
  inputs.forEach(i => sum += parseFloat(i.value) || 0);
  $("wSum").textContent = sum.toFixed(2);
  inputs.forEach(i => {
    const v = parseFloat(i.value) || 0;
    i.closest("tr").querySelector(".eff").textContent =
      sum > 0 ? (v / sum * 100).toFixed(1) + "%" : "--";
  });
}

async function pollJob() {
  const j = await (await fetch("/api/job")).json();
  $("jobState").textContent = j.running
    ? `${j.kind} 运行中… ${j.elapsed}s`
    : (j.started ? `${j.kind} ${j.rc === 0 ? "完成" : "失败(退出码" + j.rc + ")"} · ${j.elapsed}s` : "");
  if (j.started) { $("log").style.display = "block"; $("log").textContent = j.log || "(无输出)"; }
  $("btnUpdate").disabled = $("btnGenerate").disabled = j.running;
  if (j.running) { timer = setTimeout(pollJob, 1500); }
  else if (timer) { clearTimeout(timer); timer = null; refreshStatus(); }
}

$("btnUpdate").onclick = async () => {
  const r = await (await fetch("/api/update", {method: "POST"})).json();
  if (!r.ok) alert(r.msg); else pollJob();
};
$("btnGenerate").onclick = async () => {
  const weights = {};
  $("wTable").querySelectorAll("input").forEach(i => weights[i.dataset.k] = parseFloat(i.value));
  const body = {weights, window: +$("window").value, pool_min_periods: +$("pmp").value};
  if ($("years").value) body.years = +$("years").value;
  const r = await (await fetch("/api/generate", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})).json();
  if (!r.ok) alert(r.msg); else pollJob();
};
$("btnSave").onclick = async () => {
  const weights = {};
  $("wTable").querySelectorAll("input").forEach(i => weights[i.dataset.k] = parseFloat(i.value));
  const r = await (await fetch("/api/generate", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({weights, window: +$("window").value, pool_min_periods: +$("pmp").value, save_default: true})})).json();
  if (!r.ok) alert(r.msg); else pollJob();
};
$("btnReset").onclick = async () => {
  await fetch("/api/weights/reset", {method: "POST"});
  await refreshWeights(); await refreshStatus();
};

refreshStatus(); refreshWeights(); pollJob();
setInterval(refreshStatus, 30000);
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return PAGE


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8599)  # 8501 已让给 server 服务
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args()
    url = f"http://127.0.0.1:{args.port}"
    print(f"管理台: {url}  (Ctrl+C 退出)")
    if args.open:
        import webbrowser
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
