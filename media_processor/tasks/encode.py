"""
编码任务 - 使用 ffmpeg 进行视频编码和字幕嵌入
"""
import os
import platform
import subprocess
import tempfile
from typing import Optional, List, Dict
from celery import shared_task

from media_processor.logging import get_task_logger

logger = get_task_logger(__name__)

OUTPUT_DIR = os.getenv("OUTPUT_DIR", tempfile.gettempdir())


def _get_video_encoder() -> str:
    """选择最佳视频编码器"""
    system = platform.system()
    machine = platform.machine()

    # Apple Silicon
    if system == "Darwin" and machine in ("arm64", "aarch64"):
        return "h264_videotoolbox"

    # NVIDIA GPU
    try:
        result = subprocess.run(
            ["ffmpeg", "-encoders"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if "h264_nvenc" in result.stdout:
            # 进一步检查 NVIDIA GPU 是否可用
            nvidia_check = subprocess.run(
                ["nvidia-smi"],
                capture_output=True,
                timeout=5
            )
            if nvidia_check.returncode == 0:
                return "h264_nvenc"
    except:
        pass

    # 回退到 CPU
    return "libx264"


@shared_task(bind=True, name="media_processor.tasks.encode.encode_video")
def encode_video(
    self,
    video_path: str,
    task_id: str,
    segments: Optional[List[Dict]] = None,
    output_format: str = "mp4",
    video_bitrate: str = "500k",
    audio_bitrate: str = "64k",
    max_width: int = 720,
    embed_logo: bool = True
) -> dict:
    """
    编码视频 + 嵌入字幕

    Args:
        video_path: 输入视频路径
        task_id: 任务 ID
        segments: 字幕片段 (带 translation 字段)
        output_format: 输出格式
        video_bitrate: 视频码率
        audio_bitrate: 音频码率
        max_width: 最大宽度
        embed_logo: 是否嵌入水印

    Returns:
        {
            "output_path": "/path/to/output.mp4",
            "subtitle_path": "/path/to/subtitles.ass",
            "file_size": 12345678
        }
    """
    # Set task context for structured logging
    logger.set_task(task_id)
    logger.set_stage("encoding")
    logger.info(f"开始编码: {video_path}")

    self.update_state(state="ENCODING", meta={"progress": 0, "stage": "encoding"})

    task_dir = os.path.join(OUTPUT_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    output_path = os.path.join(task_dir, f"output.{output_format}")
    subtitle_path = os.path.join(task_dir, "subtitles.ass") if segments else None

    try:
        # 1. 生成 ASS 字幕文件
        if segments:
            _generate_ass_file(segments, subtitle_path, video_path)
            logger.info(f"字幕文件已生成: {subtitle_path}")

        # 2. 获取视频尺寸
        width, height = _get_video_dimensions(video_path)
        aspect_ratio = width / height if height > 0 else 1.78

        # 计算缩放
        if aspect_ratio < 1:  # 竖屏
            scale_filter = f"scale=-2:{max_width}"
        else:  # 横屏
            scale_filter = f"scale={max_width}:-2"

        # 3. 构建 ffmpeg 命令
        encoder = _get_video_encoder()
        logger.info(f"使用编码器: {encoder}")

        filter_parts = []

        # 字幕滤镜
        if subtitle_path and os.path.exists(subtitle_path):
            # 转义路径中的特殊字符
            escaped_path = subtitle_path.replace("'", "'\\''").replace(":", "\\:")
            filter_parts.append(f"ass='{escaped_path}'")

        # 缩放滤镜
        filter_parts.append(scale_filter)

        filter_complex = ",".join(filter_parts) if filter_parts else scale_filter

        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-vf", filter_complex,
            "-c:v", encoder,
            "-b:v", video_bitrate,
            "-c:a", "aac",
            "-b:a", audio_bitrate,
            "-movflags", "+faststart",
        ]

        # h264_videotoolbox 不支持 -preset 和 -crf
        if encoder == "libx264":
            cmd.extend(["-preset", "medium", "-crf", "23"])
        elif encoder == "h264_nvenc":
            cmd.extend(["-preset", "p4", "-rc", "vbr"])

        cmd.append(output_path)

        # 4. 执行编码
        logger.info(f"执行 ffmpeg 命令...")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=1800  # 30分钟超时
        )

        if result.returncode != 0:
            raise Exception(f"ffmpeg 失败: {result.stderr[-500:]}")

        file_size = os.path.getsize(output_path)
        logger.info(f"编码完成: {output_path} ({file_size / 1024 / 1024:.2f} MB)")
        logger.clear()

        return {
            "output_path": output_path,
            "subtitle_path": subtitle_path,
            "file_size": file_size,
            "encoder": encoder
        }

    except subprocess.TimeoutExpired:
        logger.error(f"编码超时")
        logger.clear()
        raise

    except Exception as e:
        logger.error(f"编码失败: {e}")
        logger.clear()
        raise


def _get_video_dimensions(video_path: str) -> tuple:
    """获取视频尺寸"""
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0",
            video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split(",")
            if len(parts) >= 2:
                return int(parts[0]), int(parts[1])
    except:
        pass
    return 1280, 720  # 默认


def _generate_ass_file(segments: List[Dict], output_path: str, video_path: str):
    """生成 ASS 字幕文件"""
    width, height = _get_video_dimensions(video_path)
    aspect_ratio = width / height

    # 根据屏幕方向选择字体大小
    if aspect_ratio < 1:  # 竖屏
        source_size = int(min(width * 0.028, 100))
        trans_size = int(min(width * 0.07, 180))
        margin_lr = int(width * 0.08)
        margin_v = int(height * 0.04)
    else:  # 横屏
        source_size = int(min(height * 0.035, 100))
        trans_size = int(min(height * 0.07, 180))
        margin_lr = int(width * 0.02)
        margin_v = int(height * 0.06)

    # 选择字体
    system = platform.system()
    if system == "Darwin":
        chinese_font = "PingFang SC"
        source_font = "Helvetica Neue"
    elif system == "Linux":
        chinese_font = "Noto Sans CJK SC"
        source_font = "DejaVu Sans"
    else:
        chinese_font = "Microsoft YaHei"
        source_font = "Segoe UI"

    # ASS 头部
    ass_header = f"""[Script Info]
ScriptType: v4.00+
Collisions: Normal
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Chinese,{chinese_font},{trans_size},&H00FFFFFF,&H000000FF,&H00000000,&H78000000,0,0,0,0,100,100,0,0,4,4,0,2,{margin_lr},{margin_lr},{margin_v},1
Style: Source,{source_font},{source_size},&H00E0FFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,3,1.5,2,{margin_lr},{margin_lr},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    # 根据屏幕方向计算换行参数
    is_portrait = width < height
    max_chars_per_line = 16 if is_portrait else 28

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(ass_header)

        line_spacing = int(trans_size * 0.3)
        upper_margin_v = margin_v + source_size + line_spacing

        for seg in segments:
            start_time = _seconds_to_ass_time(seg["start"])
            end_time = _seconds_to_ass_time(seg["end"])
            source_text = seg["text"].replace("\n", " ")
            trans_text = seg.get("translation", "").replace("\n", " ")

            # 判断是否是中文原文
            is_chinese = seg.get("language", "").lower() in ["zh", "zh-cn", "zh-tw", "chinese"]

            if is_chinese:
                # 中文原文在上，英文翻译在下（英文不换行）
                f.write(f"Dialogue: 0,{start_time},{end_time},Chinese,,0,0,{upper_margin_v},,{source_text}\n")
                if trans_text:
                    f.write(f"Dialogue: 0,{start_time},{end_time},Source,,0,0,0,,{trans_text}\n")
            else:
                # 中文翻译在上，原文在下（中文需要智能换行）
                if trans_text:
                    wrapped_trans = _wrap_text_with_newlines(trans_text, max_chars=max_chars_per_line)
                    f.write(f"Dialogue: 0,{start_time},{end_time},Chinese,,0,0,{upper_margin_v},,{wrapped_trans}\n")
                f.write(f"Dialogue: 0,{start_time},{end_time},Source,,0,0,0,,{source_text}\n")


def _seconds_to_ass_time(seconds: float) -> str:
    """秒数转 ASS 时间格式"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centiseconds = int((seconds - int(seconds)) * 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centiseconds:02d}"


def _wrap_text_with_newlines(text: str, max_chars: int = 18) -> str:
    """
    智能中文换行，使用 ASS 的 \\N 强制换行符。
    优先在标点符号处断开，其次在连接词处断开，最后强制断开。
    """
    if len(text) <= max_chars:
        return text

    # 优先断开的标点符号
    punctuation = ['，', '。', '、', '；', '：', '！', '？', ',', '.', ';', ':', '!', '?']
    # 连接词
    connectives = ['因为', '所以', '因此', '但是', '可是', '然而', '不过',
                   '而且', '并且', '同时', '另外', '此外', '然后', '接着',
                   '如果', '那么', '虽然', '即使', '或者', '还是']

    lines = []
    current_line = ""
    i = 0

    while i < len(text):
        char = text[i]
        current_line += char

        if len(current_line) >= max_chars:
            # 尝试在标点处断开
            best_break = -1
            for p in punctuation:
                pos = current_line.rfind(p)
                if pos > len(current_line) // 3:
                    best_break = max(best_break, pos + len(p))

            if best_break > 0:
                lines.append(current_line[:best_break])
                current_line = current_line[best_break:]
            else:
                # 尝试在连接词处断开
                for conn in connectives:
                    pos = current_line.rfind(conn)
                    if pos > len(current_line) // 3:
                        lines.append(current_line[:pos])
                        current_line = current_line[pos:]
                        break
                else:
                    # 强制断开
                    if len(current_line) > max_chars + 3:
                        lines.append(current_line)
                        current_line = ""

        i += 1

    if current_line:
        lines.append(current_line)

    return r"\N".join(lines)
