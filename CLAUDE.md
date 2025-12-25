# CLAUDE.md

## 项目概述

Media Processor 是一个独立的视频处理微服务，从 ChatXia 项目中拆分出来。设计目标是作为通用的媒体处理后端，可以服务于多个前端应用（Telegram Bot、Web App 等）。

## 核心功能

1. **下载** - 使用 yt-dlp 支持 YouTube、Twitter/X、Instagram 等平台
2. **转录** - 使用 OpenAI Whisper 进行语音识别
3. **翻译** - 使用 OpenAI GPT 翻译字幕
4. **编码** - 使用 ffmpeg 压缩视频并嵌入硬字幕

## 架构设计

### 为什么用 Celery + Redis？

- **解耦** - API 和 Worker 分离，可独立扩展
- **可靠** - 任务持久化，崩溃后可恢复
- **监控** - 内置任务状态追踪

### 为什么用 `--pool=solo`？

**重要**: Celery 默认使用 `prefork` 池（基于 fork），但 **Metal/MPS 不支持 fork**。

原因：
- `fork()` 复制进程内存，但不复制 GPU 上下文
- Metal 的 GPU 状态在 fork 后变成无效指针
- 子进程尝试使用 MPS 会触发 SIGABRT 崩溃

解决方案：
- 使用 `--pool=solo` 运行单进程，不 fork
- 牺牲并发换取 GPU 兼容性
- 任务串行执行，但每个任务可以用 GPU 加速

### GPU 加速

| 组件 | macOS (Apple Silicon) | Linux (NVIDIA) | 其他 |
|------|----------------------|----------------|------|
| Whisper | MPS | CUDA | CPU |
| ffmpeg | h264_videotoolbox | h264_nvenc | libx264 |

ffmpeg 通过 subprocess 启动，是独立进程，不受 fork 限制，可以安全使用 VideoToolbox。

## 目录结构

```
media-processor/
├── media_processor/
│   ├── __init__.py
│   ├── celery_app.py      # Celery 配置和队列定义
│   ├── api/
│   │   ├── __init__.py
│   │   └── main.py        # FastAPI 应用
│   └── tasks/
│       ├── __init__.py
│       ├── download.py    # yt-dlp 下载
│       ├── transcribe.py  # Whisper 转录
│       ├── translate.py   # GPT 翻译
│       ├── encode.py      # ffmpeg 编码
│       └── pipeline.py    # 完整处理管道
├── .env                   # 环境变量 (不提交)
├── .env.example           # 环境变量模板
├── requirements.txt
├── run.sh                 # 启动脚本
├── test_page.html         # 测试页面
└── README.md
```

## 关键文件说明

### `tasks/pipeline.py`

核心处理管道。**注意**：所有子任务都是直接函数调用（`_do_download`, `_do_transcribe` 等），而不是 Celery 子任务。

原因：在 Celery 任务内部调用 `.apply().get()` 会导致死锁（"Never call result.get() within a task"）。

### `run.sh`

启动脚本，使用 Python 3.9 的 celery 和 uvicorn（路径 `$HOME/Library/Python/3.9/bin/`），避免使用系统 Python。

## 开发命令

```bash
# 启动服务
./run.sh start

# 停止服务
./run.sh stop

# 查看状态
./run.sh status

# 查看日志
./run.sh logs          # 所有日志
./run.sh logs worker   # Worker 日志
./run.sh logs api      # API 日志

# 前台运行（调试用）
./run.sh worker
./run.sh api
```

## 部署

### 服务器信息

- 地址: 192.168.2.123
- 目录: `/Users/james/media-processor`
- API: http://192.168.2.123:8000

### 部署步骤

```bash
# 服务器上
cd /Users/james/media-processor
git pull
./run.sh restart
```

### 依赖

- Redis (Homebrew)
- ffmpeg (Homebrew)
- Python 3.9 + pip 包

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /api/tasks | 提交任务 |
| GET | /api/tasks/{id} | 查询状态 |
| GET | /api/tasks/{id}/result | 获取结果 |
| GET | /api/tasks/{id}/download | 下载视频 |
| GET | /api/tasks/{id}/subtitle | 下载字幕 |
| DELETE | /api/tasks/{id} | 取消任务 |
| GET | /health | 健康检查 |

## 已知问题和注意事项

1. **yt-dlp 格式问题** - YouTube 经常更新签名算法，如果下载失败，先尝试 `pip install -U yt-dlp`

2. **MPS 模型兼容性** - Whisper 的 small/medium/large/large-v3 在 MPS 上可能有 NaN 问题，turbo 和 large-v2 稳定

3. **内存占用** - turbo 模型约需 2GB 显存，large-v2 约需 4GB

## 环境变量

```env
REDIS_URL=redis://localhost:6379/0
OPENAI_API_KEY=sk-xxx
OUTPUT_DIR=/tmp/media_processor
WHISPER_MODEL=turbo
API_HOST=0.0.0.0
API_PORT=8000
```

## 未来计划

- [ ] 添加 WebSocket 实时进度推送
- [ ] 支持更多输出格式
- [ ] 添加任务优先级
- [ ] 文件清理定时任务
- [ ] Docker 部署支持
