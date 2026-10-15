#!/bin/sh
# stock_analyze 一键部署 (macOS 入口)
exec sh "$(dirname "$0")/deploy.sh" "$@"
