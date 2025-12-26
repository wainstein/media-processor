# Media Processor

A video download, transcription, translation, and encoding microservice.

## Features

- **Download** - YouTube, Twitter/X, Instagram, etc. (via yt-dlp)
- **Transcribe** - Speech-to-text with OpenAI Whisper (MPS/CUDA accelerated)
- **Translate** - Subtitle translation with OpenAI GPT
- **Encode** - Video compression with ffmpeg + embedded subtitles (VideoToolbox/NVENC)
- **Logo Watermark** - Optional logo overlay via API

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Client (ChatXia, etc.)                            │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              FastAPI (REST API)                             │
│                              - Accept task requests                         │
│                              - Return task status                           │
│                              - Serve output files                           │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Redis (Task Queue + Result Store)                   │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐            │
│  │ download_q  │ │ transcribe_q│ │ translate_q │ │ encode_q    │            │
│  └─────────────┘ └─────────────┘ └─────────────┘ └─────────────┘            │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Celery Worker (solo pool)                         │
│                           - MPS/CUDA GPU acceleration                       │
│                           - Sequential task execution                       │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Pipeline

```
Submit Task ──▶ Download ──▶ Transcribe ──▶ Translate ──▶ Encode ──▶ Complete
                 yt-dlp      Whisper        OpenAI       ffmpeg
                             MPS/CUDA       GPT-4o       VideoToolbox
```

## Quick Start

### 1. Install Dependencies

```bash
# System dependencies (macOS)
brew install redis ffmpeg

# Create venv and install Python dependencies
./run.sh setup
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env and set OPENAI_API_KEY
```

### 3. Start Services

```bash
# Start Redis
brew services start redis

# Start all services
./run.sh start
```

### 4. Test

Open `test_page.html` in browser, or use the API:

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

### POST /api/transcribe

Quick transcription - upload a file and get text back immediately.

```bash
curl -X POST http://localhost:8000/api/transcribe \
  -F "file=@video.mp4" \
  -F "model=base" \
  -F "language=en"
```

**Parameters:**
- `file`: Audio/video file (required)
- `model`: Whisper model - tiny/base/small/medium/large/turbo (default: base)
- `language`: Language code like en/zh/ja (optional, auto-detect if not specified)

**Response:**
```json
{
  "text": "Full transcript text...",
  "language": "en",
  "segments": [
    {"start": 0.0, "end": 2.5, "text": "Hello world"},
    ...
  ],
  "model": "base",
  "device": "mps"
}
```

### POST /api/tasks/upload

Upload a file for full processing (transcribe + translate + encode).

```bash
curl -X POST http://localhost:8000/api/tasks/upload \
  -F "file=@video.mp4" \
  -F "translate=true" \
  -F "target_language=zh" \
  -F "embed_subtitles=true"
```

**Parameters:**
- `file`: Video file (required)
- `translate`: Whether to translate (default: true)
- `target_language`: Target language (default: zh)
- `embed_subtitles`: Embed subtitles in video (default: true)
- `embed_logo`: Embed logo watermark (default: true)
- `video_bitrate`: Video bitrate (default: 500k)
- `max_width`: Max video width (default: 720)
- `logo_base64`: Logo image in base64 (optional)
- `callback_url`: Callback URL when done (optional)

### POST /api/tasks

Submit a processing task (download from URL).

```json
{
  "url": "https://youtube.com/watch?v=xxx",
  "options": {
    "translate": true,
    "target_language": "zh",
    "embed_subtitles": true,
    "embed_logo": true,
    "video_bitrate": "500k",
    "max_width": 720
  },
  "callback_url": "https://your-server.com/callback",
  "logo_base64": "iVBORw0KGgoAAAANSUhEUgAA..."
}
```

**Parameters:**
- `logo_base64`: Base64-encoded logo image (optional, PNG recommended)

### GET /api/tasks/{task_id}

Get task status.

### GET /api/tasks/{task_id}/result

Get task result details.

### GET /api/tasks/{task_id}/download

Download processed video.

### GET /api/tasks/{task_id}/subtitle

Download subtitle file (ASS format).

### DELETE /api/tasks/{task_id}

Cancel a task.

### GET /health

Health check.

## Commands

```bash
./run.sh setup      # First time: create venv and install dependencies
./run.sh start      # Start all services (background)
./run.sh stop       # Stop all services
./run.sh restart    # Restart services
./run.sh status     # Check status
./run.sh logs       # View logs
./run.sh worker     # Run Worker in foreground
./run.sh api        # Run API in foreground
./run.sh test       # Run tests
```

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| REDIS_URL | redis://localhost:6379/0 | Redis connection URL |
| OPENAI_API_KEY | - | OpenAI API Key (for translation) |
| OUTPUT_DIR | /tmp/media_processor | Output directory |
| WHISPER_MODEL | turbo | Whisper model (tiny/base/small/medium/large/turbo) |
| API_HOST | 0.0.0.0 | API listen address |
| API_PORT | 8000 | API port |

## GPU Acceleration

### Apple Silicon (MPS)

Whisper automatically uses MPS acceleration. Worker uses `--pool=solo` for Metal compatibility.

`PYTORCH_ENABLE_MPS_FALLBACK=1` is automatically set for unsupported operations.

### NVIDIA (CUDA)

Install CUDA-enabled PyTorch for automatic CUDA support.

### Video Encoding

- macOS: VideoToolbox (h264_videotoolbox)
- Linux/NVIDIA: NVENC (h264_nvenc)
- Other: libx264

## Logo Watermark

Supports embedding a logo image (base64-encoded) in the top-right corner of the video.

If `logo_base64` is not provided, the service will try to use `assets/logo.png` if it exists.

## Subtitle Format

Generates bilingual ASS subtitles with:
- Large Chinese translation on top
- Small original text on bottom
- Smart Chinese text wrapping
- Adaptive sizing for portrait/landscape videos

## License

MIT
