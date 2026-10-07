#!/usr/bin/env bash
# install_sync.sh — 手动触发 ClickUp 增量同步
#
# 正常情况下同步由后端 web 服务在启动时自动运行（app/jobs/clickup_sync.py）。
# 此脚本仅用于在后端服务未启动时手动执行一次同步。
#
# 用法:
#   bash install_sync.sh run
#
# 必须设置的环境变量:
#   CLICKUP_FOLDER_ID    Folder ID 或完整 Folder URL
#
# 可选环境变量:
#   CLICKUP_OUTPUT_DIR   requirements 根目录（默认见下方）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYNC_PY="${SCRIPT_DIR}/sync.py"
DEFAULT_OUTPUT_DIR="${HOME}/gitrepo/ai-prd/workspace/knowledge/requirements"

# Python：优先使用项目 venv，回退到系统 python3
if [ -f "${SCRIPT_DIR}/../../../.venv/bin/python" ]; then
    PYTHON="$(cd "${SCRIPT_DIR}/../../../.venv/bin" && pwd)/python"
elif command -v python3 &>/dev/null; then
    PYTHON="$(command -v python3)"
else
    echo "错误: 未找到 python3" >&2
    exit 1
fi

FOLDER_ID="${CLICKUP_FOLDER_ID:-}"
OUTPUT_DIR="${CLICKUP_OUTPUT_DIR:-${DEFAULT_OUTPUT_DIR}}"

usage() {
    cat <<EOF
用法: $(basename "$0") <command>

命令:
  run   立即执行一次增量同步

说明:
  定时同步由后端 web 服务自动管理（设置 CLICKUP_FOLDER_ID 环境变量后启动服务即可）。
  此脚本仅用于手动触发或后端未运行时的临时同步。
EOF
}

cmd_run() {
    if [ -z "${FOLDER_ID}" ]; then
        echo "错误: 请设置环境变量 CLICKUP_FOLDER_ID" >&2
        exit 1
    fi
    echo "执行增量同步 (folder=${FOLDER_ID}) ..."
    "${PYTHON}" "${SYNC_PY}" \
        --folder "${FOLDER_ID}" \
        --output-dir "${OUTPUT_DIR}"
}

case "${1:-}" in
    run) cmd_run ;;
    *)   usage; exit 1 ;;
esac
