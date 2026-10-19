#!/bin/zsh
# Finder 双击入口；服务在当前终端运行，不安装后台常驻任务。
set -u

PROJECT_DIR="${0:A:h}"
cd -- "$PROJECT_DIR" || exit 1

if [[ ! -x .venv/bin/python ]] || ! .venv/bin/python -c 'import uvicorn' >/dev/null 2>&1; then
  print '首次使用请按 README 安装 Python 3.12 和 uv，然后在项目目录运行：uv sync --locked'
  print '依赖安装完成后，再双击本文件。按回车关闭。'
  read -r REPLY
  exit 1
fi

if /usr/sbin/lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
  print '本机 8765 端口已有服务。本脚本不会关闭或替换它。'
  print '若是已启动的策划台，直接打开：http://127.0.0.1:8765'
  print '若是其他程序，请先处理端口占用，或按 README 更换端口。按回车关闭。'
  read -r REPLY
  exit 1
fi

print '正在启动雾潮群岛策划台……'
print '看到 Application startup complete 后，打开：http://127.0.0.1:8765'
print '演示期间请保留此终端窗口。按 Control-C 停止；以后双击本文件即可重新启动。'
print '数据库与历史版本会保留在 data/runtime/，关闭服务不会删除它们。'
.venv/bin/python -m uvicorn game_content_agent.web.app:create_app --factory --app-dir src --host 127.0.0.1 --port 8765 --no-access-log
DEMO_EXIT_CODE=$?
if [[ "$DEMO_EXIT_CODE" -ne 0 ]]; then
  print '服务未正常启动，请查看上方错误及 README 的启动排查。按回车关闭。'
  read -r REPLY
fi
exit "$DEMO_EXIT_CODE"
