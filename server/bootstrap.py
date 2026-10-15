# -*- coding: utf-8 -*-
"""引导超级管理员: python -m server.bootstrap [--username admin] [--force]

仅在用户表为空(或 --force)时可执行; 密码用 getpass 隐藏输入, 需输入两次一致。
"""
import argparse
import getpass
import os
import sys

os.environ.setdefault("SA_NO_JOBS", "1")  # CLI无需任务系统

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _read_password(prompt: str) -> str:
    """终端下隐藏输入; 非终端(管道/自动化)回退为普通读取"""
    import sys as _s
    if _s.stdin.isatty():
        return getpass.getpass(prompt)
    print("(检测到非终端输入, 密码将回显)", file=_s.stderr)
    return input(prompt)


def main():
    ap = argparse.ArgumentParser(description="创建超级管理员账号(等级9)")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--nickname", default="超级管理员")
    ap.add_argument("--force", action="store_true", help="用户表非空时也允许追加")
    args = ap.parse_args()

    from server import bcrypt, create_app, db
    from server.models import User

    app = create_app()
    with app.app_context():
        n = User.query.filter_by(deleted_at=None).count()
        if n and not args.force:
            print(f"已存在 {n} 个账号; 如需追加超管请加 --force")
            sys.exit(1)
        if User.query.filter_by(username=args.username, deleted_at=None).first():
            print(f"账号 {args.username} 已存在")
            sys.exit(1)
        pw1 = _read_password("设置密码(≥8位): ")
        pw2 = _read_password("再输入一次: ")
        if len(pw1) < 8:
            print("密码至少 8 位")
            sys.exit(1)
        if pw1 != pw2:
            print("两次输入不一致")
            sys.exit(1)
        u = User(username=args.username, nickname=args.nickname, level=9)
        u.set_password(pw1)
        db.session.add(u)
        db.session.commit()
        print(f"✓ 超级管理员已创建: {args.username} (等级9)")
        print("  请用它登录管理端: http://127.0.0.1:8501/admin/login")


if __name__ == "__main__":
    main()
