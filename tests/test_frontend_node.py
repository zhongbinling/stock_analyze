# -*- coding: utf-8 -*-
"""前端套件编排: 用夹具 HTML 跑 Node mock 测试(out/test_sector_viewer.js)"""
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
JS = REPO / "out" / "test_sector_viewer.js"


def test_frontend_node_suite(fixture_html):
    if not shutil.which("node"):
        pytest.skip("node 不可用")
    if not JS.exists():
        pytest.fail("out/test_sector_viewer.js 缺失 — 前端套件被删除?")
    r = subprocess.run([shutil.which("node"), str(JS), str(fixture_html)],
                       capture_output=True, text=True, cwd=str(REPO),
                       timeout=120, encoding="utf-8", errors="replace")
    tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-4:])
    assert r.returncode == 0, f"前端套件失败:\n{tail}"
    assert "ALL TESTS PASS" in r.stdout
