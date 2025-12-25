#!/bin/bash

# Media Processor Service - 启动脚本

set -e

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 加载环境变量 (set -a 自动 export 所有变量)
if [ -f .env ]; then
    set -a; source .env; set +a
fi

# 设置 PATH (Python 3.9 优先，然后 Homebrew)
export PATH="$HOME/Library/Python/3.9/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

# Python 命令
CELERY="$HOME/Library/Python/3.9/bin/celery"
UVICORN="$HOME/Library/Python/3.9/bin/uvicorn"

# 默认值
export REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"
export OUTPUT_DIR="${OUTPUT_DIR:-/tmp/media_processor}"
export WHISPER_MODEL="${WHISPER_MODEL:-turbo}"
export API_HOST="${API_HOST:-0.0.0.0}"
export API_PORT="${API_PORT:-8000}"
export PYTHONPATH="$SCRIPT_DIR:$PYTHONPATH"

# 创建必要目录
mkdir -p "$OUTPUT_DIR"
mkdir -p logs

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

MODE="${1:-help}"

case "$MODE" in
    worker)
        echo -e "${GREEN}启动 Celery Worker (solo 池, 支持 GPU)...${NC}"
        $CELERY -A media_processor.celery_app worker \
            --pool=solo \
            -Q download,transcribe,translate,encode,default \
            -l INFO
        ;;

    api)
        echo -e "${GREEN}启动 API 服务 (端口: $API_PORT)...${NC}"
        $UVICORN media_processor.api.main:app \
            --host "$API_HOST" \
            --port "$API_PORT"
        ;;

    start)
        echo -e "${YELLOW}启动所有服务 (后台模式)...${NC}"

        # 检查 Redis
        if ! redis-cli ping > /dev/null 2>&1; then
            echo -e "${RED}Redis 未运行，请先启动 Redis${NC}"
            exit 1
        fi
        echo -e "${GREEN}Redis OK${NC}"

        # 停止旧进程
        $0 stop 2>/dev/null || true
        sleep 1

        # 启动 Worker
        nohup $CELERY -A media_processor.celery_app worker \
            --pool=solo \
            -Q download,transcribe,translate,encode,default \
            -l INFO > logs/worker.log 2>&1 &
        echo "Worker PID: $!"

        # 启动 API
        nohup $UVICORN media_processor.api.main:app \
            --host "$API_HOST" \
            --port "$API_PORT" > logs/api.log 2>&1 &
        echo "API PID: $!"

        sleep 2
        echo -e "${GREEN}服务已启动${NC}"
        $0 status
        ;;

    stop)
        echo -e "${YELLOW}停止所有服务...${NC}"
        pkill -f "celery.*media_processor" 2>/dev/null || true
        pkill -f "uvicorn.*media_processor" 2>/dev/null || true
        echo -e "${GREEN}已停止${NC}"
        ;;

    restart)
        $0 stop
        sleep 2
        $0 start
        ;;

    status)
        echo -e "${YELLOW}=== 服务状态 ===${NC}"
        echo ""
        echo "Redis:"
        redis-cli ping 2>/dev/null || echo "  未运行"
        echo ""
        echo "Worker:"
        pgrep -fl "celery.*media_processor" || echo "  未运行"
        echo ""
        echo "API:"
        pgrep -fl "uvicorn.*media_processor" || echo "  未运行"
        echo ""
        echo "健康检查:"
        curl -s "http://localhost:$API_PORT/health" 2>/dev/null || echo "  API 未响应"
        echo ""
        ;;

    logs)
        LOG_TYPE="${2:-all}"
        case "$LOG_TYPE" in
            worker)
                tail -f logs/worker.log
                ;;
            api)
                tail -f logs/api.log
                ;;
            *)
                tail -f logs/*.log
                ;;
        esac
        ;;

    test)
        echo -e "${YELLOW}运行测试...${NC}"
        python -m pytest tests/ -v
        ;;

    *)
        echo "Media Processor Service"
        echo ""
        echo "用法: $0 <command>"
        echo ""
        echo "命令:"
        echo "  start     启动所有服务 (后台)"
        echo "  stop      停止所有服务"
        echo "  restart   重启所有服务"
        echo "  status    查看服务状态"
        echo "  worker    前台运行 Worker"
        echo "  api       前台运行 API"
        echo "  logs      查看日志 (worker|api|all)"
        echo "  test      运行测试"
        ;;
esac
