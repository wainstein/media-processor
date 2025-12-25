# Media Processor

视频下载、转录、翻译、编码微服务。

## 功能

- **下载** - 支持 YouTube、Twitter/X、Instagram 等平台 (yt-dlp)
- **转录** - 使用 OpenAI Whisper 进行语音识别 (支持 MPS/CUDA 加速)
- **翻译** - 使用 OpenAI GPT 翻译字幕
- **编码** - 使用 ffmpeg 压缩视频并嵌入字幕 (支持 VideoToolbox/NVENC)

## 架构

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              客户端 (ChatXia, 其他服务)                       │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              FastAPI (REST API)                              │
│                              - 接收任务请求                                   │
│                              - 返回任务状态                                   │
│                              - 提供文件下载                                   │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Redis (任务队列 + 结果存储)                      │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐            │
│  │ download_q  │ │ transcribe_q│ │ translate_q │ │ encode_q    │            │
│  └─────────────┘ └─────────────┘ └─────────────┘ └─────────────┘            │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Celery Worker (solo pool)                       │
│                              - 支持 MPS/CUDA GPU 加速                        │
│                              - 队列串行处理                                   │
└─────────────────────────────────────────────────────────────────────────────┘
```

## 任务流程

```
提交任务 ──▶ 下载视频 ──▶ 转录 (Whisper) ──▶ 翻译 (GPT) ──▶ 编码 (ffmpeg) ──▶ 完成
              yt-dlp        MPS/CUDA           OpenAI       VideoToolbox
```

## 快速开始

### 1. 安装依赖

```bash
# 系统依赖 (macOS)
brew install redis ffmpeg

# Python 依赖
pip install -r requirements.txt
```

### 2. 配置

```bash
cp .env.example .env
# 编辑 .env 填入 OPENAI_API_KEY
```

### 3. 启动服务

```bash
# 启动 Redis
brew services start redis

# 启动服务
./run.sh start
```

### 4. 测试

打开浏览器访问测试页面，或使用 API：

```bash
# 提交任务
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"url": "https://youtube.com/watch?v=xxx"}'

# 查询状态
curl http://localhost:8000/api/tasks/{task_id}

# 下载结果
curl -O http://localhost:8000/api/tasks/{task_id}/download
```

## API

### POST /api/tasks

提交处理任务。

```json
{
  "url": "https://youtube.com/watch?v=xxx",
  "options": {
    "translate": true,
    "target_language": "zh",
    "embed_subtitles": true,
    "video_bitrate": "500k",
    "max_width": 720
  },
  "callback_url": "https://your-server.com/callback"
}
```

### GET /api/tasks/{task_id}

查询任务状态。

### GET /api/tasks/{task_id}/result

获取任务结果详情。

### GET /api/tasks/{task_id}/download

下载处理后的视频。

### GET /api/tasks/{task_id}/subtitle

下载字幕文件 (ASS 格式)。

### DELETE /api/tasks/{task_id}

取消任务。

### GET /health

健康检查。

## 命令

```bash
./run.sh start      # 启动所有服务 (后台)
./run.sh stop       # 停止所有服务
./run.sh restart    # 重启
./run.sh status     # 查看状态
./run.sh logs       # 查看日志
./run.sh worker     # 前台运行 Worker
./run.sh api        # 前台运行 API
```

## 配置

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| REDIS_URL | redis://localhost:6379/0 | Redis 连接地址 |
| OPENAI_API_KEY | - | OpenAI API Key (翻译用) |
| OUTPUT_DIR | /tmp/media_processor | 输出目录 |
| WHISPER_MODEL | turbo | Whisper 模型 (tiny/base/small/medium/large/turbo) |
| API_HOST | 0.0.0.0 | API 监听地址 |
| API_PORT | 8000 | API 端口 |

## GPU 加速

### Apple Silicon (MPS)

Whisper 自动使用 MPS 加速。Worker 使用 `--pool=solo` 以兼容 Metal。

### NVIDIA (CUDA)

安装 CUDA 版本的 PyTorch 后自动启用。

### 视频编码

- macOS: 自动使用 VideoToolbox (h264_videotoolbox)
- Linux/NVIDIA: 自动使用 NVENC (h264_nvenc)
- 其他: 使用 libx264

## License

MIT
