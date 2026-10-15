# -*- coding: utf-8 -*-
"""部署辅助: 检查是否已有可用账号
退出码: 0=已有账号(跳过bootstrap) 3=无账号(需创建)"""
import os
import sys

os.environ.setdefault("SA_NO_JOBS", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import create_app          # noqa: E402
from server.models import User         # noqa: E402

a = create_app()
with a.app_context():
    sys.exit(0 if User.query.filter_by(deleted_at=None).count() else 3)
