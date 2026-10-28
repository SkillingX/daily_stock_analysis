#!/usr/bin/env bash
# ===================================
# daily_stock_analysis 部署脚本
# 用途: 本机直接部署（非 Docker）
# ===================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$APP_DIR/venv"
LOG_DIR="$APP_DIR/logs"
DATA_DIR="$APP_DIR/data"
REPORTS_DIR="$APP_DIR/reports"
FRONTEND_DIR="$APP_DIR/apps/dsa-web"

cd "$APP_DIR"

echo "[1/6] 检查必要目录..."
mkdir -p "$LOG_DIR" "$DATA_DIR" "$REPORTS_DIR"

echo "[2/6] 构建前端静态资源..."
if [ -f "$FRONTEND_DIR/package.json" ]; then
    if command -v npm &>/dev/null; then
        echo "  安装前端依赖..."
        npm --prefix "$FRONTEND_DIR" ci
        echo "  构建前端..."
        npm --prefix "$FRONTEND_DIR" run build
    else
        echo "  WARNING: npm 未安装，跳过前端构建"
    fi
else
    echo "  WARNING: 未找到前端项目，跳过构建"
fi

echo "[3/6] 检查虚拟环境..."
if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo "  未找到虚拟环境，正在创建..."
    python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

echo "[4/7] 安装/更新 Python 依赖..."
pip install --no-cache-dir -r requirements.txt -q

# issue #58：普通工具声明会进入 LiteLLM 的懒加载 MCP 导入路径；pip check、
# 健康检查与普通模块导入识别不了这一缺口。预检必须在停止旧服务之前完成，
# 失败时保留旧服务继续运行并中止切换。
echo "[5/7] 工具依赖预检（安装后、停止旧服务前）..."
if ! "$VENV_DIR/bin/python" scripts/check_tool_deps.py; then
    echo "  预检失败：工具处理依赖不兼容，旧服务未停止，本次部署已中止。"
    exit 1
fi

echo "[6/7] 停止旧服务..."
OLD_PID=$(pgrep -f "venv/bin/python.*main.py.*--serve-only" || true)
if [ -n "$OLD_PID" ]; then
    echo "  停止现有进程 PID=$OLD_PID ..."
    kill "$OLD_PID" 2>/dev/null || true
    sleep 2
    if kill -0 "$OLD_PID" 2>/dev/null; then
        kill -9 "$OLD_PID" 2>/dev/null || true
    fi
    echo "  旧服务已停止"
else
    echo "  未发现运行中的服务"
fi

echo "[7/7] 启动新服务..."
nohup "$VENV_DIR/bin/python" main.py --serve-only --host 0.0.0.0 --port 8000 \
    > "$LOG_DIR/deploy_stdout.log" 2>&1 &
NEW_PID=$!
echo "  服务已启动 (PID=$NEW_PID)"

echo "  等待服务就绪..."
for i in $(seq 1 60); do
    if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
        echo "  服务就绪 (耗时 ${i}s)"
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "  WARNING: 服务未在 60s 内响应健康检查"
        echo "  请检查日志: tail -50 $LOG_DIR/deploy_stdout.log"
    fi
    sleep 1
done

echo ""
echo "====== 部署完成 ======"
echo "  PID:      $NEW_PID"
echo "  端口:     8000"
echo "  日志:     $LOG_DIR/"
echo "  数据:     $DATA_DIR/"
echo "  报告:     $REPORTS_DIR/"
echo "======================"
