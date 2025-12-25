# Media Processor

A microservice for video downloading, transcription, translation, and encoding.

## Features

- **Download** - YouTube, Twitter/X, Instagram and more (via yt-dlp)
- **Transcribe** - Speech recognition with OpenAI Whisper (MPS/CUDA accelerated)
- **Translate** - Subtitle translation with OpenAI GPT
- **Encode** - Video compression with ffmpeg (VideoToolbox/NVENC support)

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Client (ChatBot, Web App, etc.)                 │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              FastAPI (REST API)                              │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Redis (Task Queue + Result Store)               │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Celery Worker (solo pool)                       │
│                              - MPS/CUDA GPU acceleration                     │
│                              - Sequential task processing                    │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Pipeline

```
Submit ──▶ Download ──▶ Transcribe ──▶ Translate ──▶ Encode ──▶ Complete
             yt-dlp      Whisper        GPT         ffmpeg
```

## Quick Start

### 1. Install Dependencies

```bash
# System dependencies (macOS)
brew install redis ffmpeg

# Python dependencies
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env and add your OPENAI_API_KEY
```

### 3. Start Services

```bash
# Start Redis
brew services start redis

# Start services
./run.sh start
```

### 4. Test

Open browser to access test page, or use API:

```bash
# Submit task
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"url": "https://youtube.com/watch?v=xxx"}'

# Check status
curl http://localhost:8000/api/tasks/{task_id}

# Download result
curl -O http://localhost:8000/api/tasks/{task_id}/download
```

## API

### POST /api/tasks

Submit a processing task.

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

Get task status.

### GET /api/tasks/{task_id}/result

Get task result details.

### GET /api/tasks/{task_id}/download

Download processed video.

### GET /api/tasks/{task_id}/subtitle

Download subtitle file (ASS format).

### DELETE /api/tasks/{task_id}

Cancel task.

### GET /health

Health check.

## Commands

```bash
./run.sh start      # Start all services (background)
./run.sh stop       # Stop all services
./run.sh restart    # Restart
./run.sh status     # Check status
./run.sh logs       # View logs
./run.sh worker     # Run Worker in foreground
./run.sh api        # Run API in foreground
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| REDIS_URL | redis://localhost:6379/0 | Redis connection URL |
| OPENAI_API_KEY | - | OpenAI API Key (for translation) |
| OUTPUT_DIR | /tmp/media_processor | Output directory |
| WHISPER_MODEL | turbo | Whisper model (tiny/base/small/medium/large/turbo) |
| API_HOST | 0.0.0.0 | API listen address |
| API_PORT | 8000 | API port |

## GPU Acceleration

### Apple Silicon (MPS)

Whisper automatically uses MPS. Worker uses `--pool=solo` for Metal compatibility.

### NVIDIA (CUDA)

Automatically enabled after installing CUDA version of PyTorch.

### Video Encoding

- macOS: VideoToolbox (h264_videotoolbox)
- Linux/NVIDIA: NVENC (h264_nvenc)
- Other: libx264

## License

MIT
