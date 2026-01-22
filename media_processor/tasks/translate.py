"""
翻译任务 - 使用 OpenAI API 进行翻译
"""
import os
from typing import List, Dict, Optional
from celery import shared_task
import openai

from media_processor.logging import get_task_logger

logger = get_task_logger(__name__)

# OpenAI 配置
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", os.getenv("OPENAI_TOKEN"))
TRANSLATE_MODEL = os.getenv("TRANSLATE_MODEL", "gpt-4.1-mini")


@shared_task(bind=True, name="media_processor.tasks.translate.translate_segments")
def translate_segments(
    self,
    segments: List[Dict],
    task_id: str,
    target_language: str = "zh",
    context: Optional[str] = None,
    chunk_size: int = 10
) -> List[Dict]:
    """
    翻译字幕片段

    Args:
        segments: 字幕片段列表 [{"start": 0, "end": 2, "text": "...", "language": "en"}, ...]
        task_id: 任务 ID
        target_language: 目标语言
        context: 上下文信息 (视频描述等)
        chunk_size: 每批翻译的片段数

    Returns:
        带翻译的片段列表 [{"start": 0, "end": 2, "text": "...", "translation": "..."}, ...]
    """
    # Set task context for structured logging
    logger.set_task(task_id)
    logger.set_stage("translating")
    logger.info(f"开始翻译: {len(segments)} 个片段 -> {target_language}")

    self.update_state(state="TRANSLATING", meta={"progress": 0, "stage": "translating"})

    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY 未设置")

    client = openai.OpenAI(api_key=OPENAI_API_KEY)

    # 按批次翻译
    translated_segments = []
    total_chunks = (len(segments) + chunk_size - 1) // chunk_size

    for i in range(0, len(segments), chunk_size):
        chunk = segments[i:i + chunk_size]
        chunk_idx = i // chunk_size + 1

        # 更新进度
        progress = int((chunk_idx / total_chunks) * 100)
        self.update_state(
            state="TRANSLATING",
            meta={"progress": progress, "stage": "translating", "chunk": chunk_idx, "total": total_chunks}
        )

        # 准备翻译文本
        texts = [seg["text"] for seg in chunk]
        source_lang = chunk[0].get("language", "en")

        # 如果源语言就是目标语言，跳过翻译
        if source_lang == target_language or (source_lang == "zh" and target_language == "zh"):
            for seg in chunk:
                seg["translation"] = ""  # 不需要翻译
                translated_segments.append(seg)
            continue

        # 调用 OpenAI 翻译
        try:
            translations = _batch_translate(
                client, texts, source_lang, target_language, context
            )

            for seg, trans in zip(chunk, translations):
                seg["translation"] = trans
                translated_segments.append(seg)

        except Exception as e:
            logger.error(f"翻译失败 (chunk {chunk_idx}): {e}")
            # 失败时保留原文
            for seg in chunk:
                seg["translation"] = seg["text"]
                translated_segments.append(seg)

    logger.info(f"翻译完成")
    logger.clear()
    return translated_segments


def _batch_translate(
    client: openai.OpenAI,
    texts: List[str],
    source_lang: str,
    target_lang: str,
    context: Optional[str] = None
) -> List[str]:
    """批量翻译文本"""

    # 构建提示
    lang_names = {
        "en": "英语",
        "zh": "中文",
        "ja": "日语",
        "ko": "韩语",
        "fr": "法语",
        "de": "德语",
        "es": "西班牙语",
    }

    source_name = lang_names.get(source_lang, source_lang)
    target_name = lang_names.get(target_lang, target_lang)

    # 用分隔符连接文本
    separator = "\n|||SEPARATOR|||\n"
    combined_text = separator.join(texts)

    system_prompt = f"""你是专业的字幕翻译员。将以下{source_name}字幕翻译成{target_name}。

规则:
1. 保持每行对应关系，用 |||SEPARATOR||| 分隔
2. 翻译要自然流畅，符合口语习惯
3. 保留专有名词、品牌名等
4. 不要添加解释或注释"""

    if context:
        system_prompt += f"\n\n视频背景: {context[:500]}"

    response = client.chat.completions.create(
        model=TRANSLATE_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": combined_text}
        ],
        temperature=0.3,
        max_tokens=4000
    )

    result_text = response.choices[0].message.content.strip()
    translations = result_text.split("|||SEPARATOR|||")

    # 确保翻译数量匹配
    translations = [t.strip() for t in translations]
    while len(translations) < len(texts):
        translations.append(texts[len(translations)])  # 补充原文

    return translations[:len(texts)]


@shared_task(bind=True, name="media_processor.tasks.translate.translate_text")
def translate_text(
    self,
    text: str,
    task_id: str,
    target_language: str = "zh"
) -> str:
    """
    翻译单段文本 (用于标题、描述等)
    """
    if not text or not text.strip():
        return text

    if not OPENAI_API_KEY:
        return text

    try:
        client = openai.OpenAI(api_key=OPENAI_API_KEY)

        response = client.chat.completions.create(
            model=TRANSLATE_MODEL,
            messages=[
                {"role": "system", "content": f"将以下文本翻译成中文，只返回翻译结果:"},
                {"role": "user", "content": text}
            ],
            temperature=0.3,
            max_tokens=1000
        )

        return response.choices[0].message.content.strip()

    except Exception as e:
        logger.set_task(task_id)
        logger.error(f"文本翻译失败: {e}")
        logger.clear()
        return text
