#!/bin/bash

# Media Processor Service 启动脚本

set -e

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Media Processor Service${NC}"
echo -e "${GREEN}========================================${NC}"

# 检查 Redis
echo -e "\n${YELLOW}检查 Redis...${NC}"
if ! redis-cli ping > /dev/null 2>&1; then
    echo -e "${RED}Redis 未运行，正在启动...${NC}"
    brew services start redis 2>/dev/null || redis-server --daemonize yes
    sleep 2
fi
echo -e "${GREEN}Redis OK${NC}"

# 设置环境变量
export REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"
export OUTPUT_DIR="${OUTPUT_DIR:-/tmp/media_processor}"
export WHISPER_MODEL="${WHISPER_MODEL:-turbo}"
export WHISPER_DEVICE="${WHISPER_DEVICE:-auto}"

mkdir -p "$OUTPUT_DIR"

# 启动模式
MODE="${1:-all}"

case "$MODE" in
    api)
        echo -e "\n${YELLOW}启动 API 服务...${NC}"
        uvicorn media_processor.api.main:app --host 0.0.0.0 --port 8000 --reload
        ;;

    worker)
        QUEUE="${2:-all}"
        echo -e "\n${YELLOW}启动 Worker (队列: $QUEUE)...${NC}"
        if [ "$QUEUE" == "all" ]; then
            celery -A media_processor.celery_app worker -Q download,transcribe,translate,encode,default -l INFO
        else
            celery -A media_processor.celery_app worker -Q "$QUEUE" -l INFO -c 1
        fi
        ;;

    download-worker)
        echo -e "\n${YELLOW}启动下载 Worker (并发: 3)...${NC}"
        celery -A media_processor.celery_app worker -Q download -l INFO -c 3
        ;;

    transcribe-worker)
        echo -e "\n${YELLOW}启动转录 Worker (GPU, 并发: 1)...${NC}"
        celery -A media_processor.celery_app worker -Q transcribe -l INFO -c 1
        ;;

    translate-worker)
        echo -e "\n${YELLOW}启动翻译 Worker (并发: 5)...${NC}"
        celery -A media_processor.celery_app worker -Q translate -l INFO -c 5
        ;;

    encode-worker)
        echo -e "\n${YELLOW}启动编码 Worker (GPU, 并发: 1)...${NC}"
        celery -A media_processor.celery_app worker -Q encode -l INFO -c 1
        ;;

    all)
        echo -e "\n${YELLOW}启动所有服务 (后台模式)...${NC}"

        # 启动 Workers
        echo "启动 Workers..."
        celery -A media_processor.celery_app worker -Q download -l INFO -c 3 --detach --pidfile=/tmp/celery_download.pid --logfile=/tmp/celery_download.log
        celery -A media_processor.celery_app worker -Q transcribe -l INFO -c 1 --detach --pidfile=/tmp/celery_transcribe.pid --logfile=/tmp/celery_transcribe.log
        celery -A media_processor.celery_app worker -Q translate -l INFO -c 3 --detach --pidfile=/tmp/celery_translate.pid --logfile=/tmp/celery_translate.log
        celery -A media_processor.celery_app worker -Q encode -l INFO -c 1 --detach --pidfile=/tmp/celery_encode.pid --logfile=/tmp/celery_encode.log

        echo -e "${GREEN}Workers 已启动${NC}"

        # 启动 API (前台)
        echo -e "\n${YELLOW}启动 API 服务...${NC}"
        uvicorn media_processor.api.main:app --host 0.0.0.0 --port 8000
        ;;

    stop)
        echo -e "\n${YELLOW}停止所有服务...${NC}"
        pkill -f "celery.*media_processor" || true
        pkill -f "uvicorn.*media_processor" || true
        echo -e "${GREEN}已停止${NC}"
        ;;

    status)
        echo -e "\n${YELLOW}服务状态:${NC}"
        echo -n "API: "
        curl -s http://localhost:8000/health | jq . 2>/dev/null || echo "未运行"
        echo ""
        echo "Workers:"
        celery -A media_processor.celery_app inspect active 2>/dev/null || echo "  未运行"
        ;;

    *)
        echo "用法: $0 {api|worker|download-worker|transcribe-worker|translate-worker|encode-worker|all|stop|status}"
        echo ""
        echo "  api               - 只启动 API 服务"
        echo "  worker [queue]    - 启动 Worker (可指定队列)"
        echo "  download-worker   - 启动下载专用 Worker"
        echo "  transcribe-worker - 启动转录专用 Worker (GPU)"
        echo "  translate-worker  - 启动翻译专用 Worker"
        echo "  encode-worker     - 启动编码专用 Worker (GPU)"
        echo "  all               - 启动所有服务"
        echo "  stop              - 停止所有服务"
        echo "  status            - 查看服务状态"
        exit 1
        ;;
esac
