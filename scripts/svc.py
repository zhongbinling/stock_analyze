# -*- coding: utf-8 -*-
"""stock_analyze 服务管理器 (win/mac/linux 统一)

用法:
  python scripts/svc.py start     # 迁移→后台启动→健康等待
  python scripts/svc.py stop      # 停止
  python scripts/svc.py status    # 状态(进程+健康检查)
  python scripts/svc.py restart   # 重启
  python scripts/svc.py install   # 安装开机自启(win:计划任务 / linux:systemd / mac:launchd)
  python scripts/svc.py uninstall # 卸载开机自启

由 service.bat / service.sh 薄包装调用; 也可直接使用。
"""
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PID_FILE = DATA / "service.pid"
LOG_FILE = DATA / "service.log"
HEALTH_TIMEOUT = 30

IS_WIN = os.name == "nt"
TASK_NAME = "stock_analyze_server"
SYSTEMD_UNIT = "/etc/systemd/system/stock_analyze.service"
LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / "com.stock_analyze.server.plist"


def log(msg: str):
    print(msg, flush=True)


def py_exe() -> str:
    """优先 .venv, 否则当前解释器"""
    venv_py = ROOT / ".venv" / ("Scripts/python.exe" if IS_WIN else "bin/python")
    return str(venv_py) if venv_py.exists() else sys.executable


def read_cfg() -> dict:
    import yaml
    with open(ROOT / "config" / "server.yaml", encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("server", {})


def base_url() -> str:
    cfg = read_cfg()
    host = cfg.get("host", "127.0.0.1")
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{int(cfg.get('port', 8501))}"


def healthy(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(base_url() + "/healthz", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def read_pid():
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid) -> bool:
    if not pid:
        return False
    if IS_WIN:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True,
                             text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- start / stop

def cmd_start():
    if healthy():
        log(f"✓ 服务已在运行: {base_url()} (探活通过, 拒绝重复启动)")
        return 0
    DATA.mkdir(exist_ok=True)
    # 1) 幂等迁移 (CLI 进程不拉起任务系统)
    env = {**os.environ, "SA_NO_JOBS": "1", "FLASK_APP": "server:create_app()"}
    log("· 数据库迁移(如有)…")
    r = subprocess.run([py_exe(), "-m", "flask", "db", "upgrade"], cwd=str(ROOT), env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        log("✗ 迁移失败:\n" + (r.stdout or "") + (r.stderr or ""))
        return 1
    # 2) 后台启动
    kw = {}
    if IS_WIN:
        kw["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    with open(LOG_FILE, "ab") as lf:
        p = subprocess.Popen([py_exe(), "-u", "-m", "server"], cwd=str(ROOT),
                             stdout=lf, stderr=subprocess.STDOUT, **kw)
    PID_FILE.write_text(str(p.pid), encoding="utf-8")
    log(f"· 已启动 PID {p.pid}, 日志 {LOG_FILE.relative_to(ROOT)}, 等待健康检查…")
    # 3) 健康等待
    t0 = time.time()
    while time.time() - t0 < HEALTH_TIMEOUT:
        if healthy():
            log(f"✓ 服务就绪: {base_url()}  (管理端 {base_url()}/admin/login)")
            return 0
        time.sleep(1)
    log(f"✗ {HEALTH_TIMEOUT}s 内未通过健康检查, 最近日志:")
    try:
        tail = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
        log("\n".join(tail))
    except OSError:
        pass
    return 1


def cmd_stop():
    if healthy():
        log("· 服务探活通过")
    pid = read_pid()
    if not pid or not pid_alive(pid):
        if healthy():      # 活着但PID文件缺失/失效(如systemd拉起) → 尝试按端口找
            log("⚠ 无有效PID文件但服务在运行; 尝试按端口定位进程…")
            pid = find_pid_by_port()
    if not pid or not pid_alive(pid):
        log("✓ 服务未在运行")
        PID_FILE.unlink(missing_ok=True)
        return 0
    log(f"· 停止 PID {pid}…")
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.kill(pid, 15)
            for _ in range(10):
                if not pid_alive(pid):
                    break
                time.sleep(0.5)
            if pid_alive(pid):
                os.kill(pid, 9)
        except OSError:
            pass
    time.sleep(1)
    PID_FILE.unlink(missing_ok=True)
    log("✓ 已停止" if not healthy() else "⚠ 端口仍通, 请检查 data/service.log")
    return 0


def find_pid_by_port():
    port = base_url().rsplit(":", 1)[-1]
    try:
        if IS_WIN:
            out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True,
                                 text=True).stdout
            for line in out.splitlines():
                if f"127.0.0.1:{port}" in line or f"0.0.0.0:{port}" in line:
                    if "LISTENING" in line.upper():
                        return int(line.split()[-1])
        else:
            out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                                 capture_output=True, text=True).stdout
            return int(out.strip().splitlines()[0])
    except (OSError, ValueError, IndexError):
        pass
    return None


def cmd_status():
    pid = read_pid()
    alive = pid_alive(pid)
    ok = healthy()
    if ok:
        log(f"✓ 运行中: {base_url()} (健康检查通过" + (f", PID {pid})" if alive else ", 非本脚本启动)"))
        return 0
    if alive:
        log(f"⚠ 进程存活(PID {pid})但健康检查未通过, 查看 data/service.log")
        return 1
    log("✗ 未运行")
    return 1


def cmd_restart():
    cmd_stop()
    time.sleep(1)
    return cmd_start()


# ---------------------------------------------------------------- 开机自启

def _tpl(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def cmd_install():
    if IS_WIN:
        # 用户级自启: HKCU Run 键(无需管理员; schtasks ONLOGON 会被拒绝)
        run_key = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
        run_cmd = f'"{ROOT}\\scripts\\service.bat" start'
        r = subprocess.run(["reg", "add", run_key, "/v", TASK_NAME, "/t",
                            "REG_SZ", "/d", run_cmd, "/f"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            log(f"✓ 已注册登录自启(HKCU Run: {TASK_NAME}), 命令: {run_cmd}")
        else:
            log("✗ 注册失败: " + (r.stdout or r.stderr))
        return r.returncode
    if Path("/usr/lib/systemd").exists() or Path("/run/systemd/system").exists():
        unit = _tpl("systemd/stock_analyze.service").format(
            REPO=str(ROOT), PY=py_exe(), USER=os.environ.get("USER", "root"))
        tmp = Path("/tmp/stock_analyze.service")
        tmp.write_text(unit, encoding="utf-8")
        log("· 安装 systemd 单元(需要 sudo)…")
        r = subprocess.run(f"sudo cp {tmp} {SYSTEMD_UNIT} && sudo systemctl daemon-reload "
                           f"&& sudo systemctl enable stock_analyze", shell=True)
        return r.returncode
    if sys.platform == "darwin":
        plist = _tpl("launchd/com.stock_analyze.server.plist").format(
            REPO=str(ROOT), PY=py_exe())
        LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)
        LAUNCHD_PLIST.write_text(plist, encoding="utf-8")
        subprocess.run(["launchctl", "unload", str(LAUNCHD_PLIST)], capture_output=True)
        r = subprocess.run(["launchctl", "load", str(LAUNCHD_PLIST)])
        log("✓ 已安装并加载 launchd(登录自启)" if r.returncode == 0 else "✗ 加载失败")
        return r.returncode
    log("⚠ 未知平台, 请参照 docs/部署手册.md 手动配置")
    return 1


def cmd_uninstall():
    if IS_WIN:
        run_key = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
        r = subprocess.run(["reg", "delete", run_key, "/v", TASK_NAME, "/f"],
                           capture_output=True, text=True)
        log("✓ 已卸载自启" if r.returncode == 0
            else "· 无需卸载(" + (r.stdout or r.stderr).strip() + ")")
        return 0
    if Path(SYSTEMD_UNIT).exists():
        r = subprocess.run("sudo systemctl disable --now stock_analyze && sudo rm "
                           + SYSTEMD_UNIT, shell=True)
        return r.returncode
    if LAUNCHD_PLIST.exists():
        subprocess.run(["launchctl", "unload", str(LAUNCHD_PLIST)], capture_output=True)
        LAUNCHD_PLIST.unlink()
        log("✓ 已卸载 launchd 自启")
    return 0


COMMANDS = {"start": cmd_start, "stop": cmd_stop, "status": cmd_status,
            "restart": cmd_restart, "install": cmd_install, "uninstall": cmd_uninstall}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        log(__doc__)
        return 2
    return COMMANDS[sys.argv[1]]()


if __name__ == "__main__":
    sys.exit(main())
