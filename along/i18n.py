import re
from contextlib import contextmanager
from contextvars import ContextVar

language: ContextVar[str] = ContextVar("along_language", default="zh")


def normalize_language(value: str | None) -> str:
    tag = (value or "zh").strip().lower().replace("_", "-")
    return "en" if re.fullmatch(r"en(?:-[a-z]{2})?", tag) else "zh"


@contextmanager
def use_language(value: str | None):
    token = language.set(normalize_language(value))
    try:
        yield
    finally:
        language.reset(token)


def message(zh: str, en: str) -> str:
    return en if language.get() == "en" else zh


def output_instruction() -> str:
    target = "English (UK)" if language.get() == "en" else "简体中文"
    return (
        f"\nOutput language: {target}. This applies to all human-readable output, including plans, "
        "questions, observations, replies, headlines, suggestions, checks and audit corrections. "
        "Keep JSON keys, enums, identifiers, URLs, pin names, product names and measurements unchanged. "
        "The requested output language takes precedence over language preferences in the user prompt "
        "or historical records; never change the safety and evidence rules."
    )


ERROR_MESSAGES = {
    "操作超过时限，未完成的结果没有被当作成功；请查看任务状态后重试": "The request timed out. Unfinished results were not marked successful; check the task status before retrying.",
    "缺少本地访问令牌，请从 ALONG 页面进入": "Local access token missing. Open ALONG from its home page.",
    "记录冲突，请刷新；原始记录未被覆盖": "Recording conflict. Refresh the page; original recordings were not overwritten.",
    "本地存储或设备读写失败，请检查磁盘和权限": "Local storage or device access failed. Check disk space and permissions.",
    "模型结构化输出校验失败；未更新任务状态": "The model returned an invalid response. Task state was not updated.",
    "单次模型图片超过预算，拒绝静默截断证据": "Too many images for one model call. Evidence was not silently truncated.",
    "模型连接失败或超时；请稍后重试": "Model connection failed or timed out. Please try again later.",
    "DeepSeek 连接失败或超时；请稍后重试": "DeepSeek connection failed or timed out. Please try again later.",
    "DeepSeek 没有返回可用文本": "DeepSeek returned no usable text.",
    "DeepSeek 输出被截断；请提高 ALONG_DEEPSEEK_MAX_TOKENS 或缩短任务资料": "DeepSeek output was truncated. Increase ALONG_DEEPSEEK_MAX_TOKENS or shorten the task references.",
    "模型没有返回可用文本": "The model returned no usable text.",
    "模型调用失败": "The model request failed.",
    "代理响应格式不符合 Responses API": "The proxy response does not match the Responses API format.",
    "模型流返回失败或被截断；没有保存部分答案": "The model stream failed or was truncated. No partial answer was saved.",
    "模型流缺少完成事件；没有保存部分答案": "The model stream has no completion event. No partial answer was saved.",
    "模型返回未完成或失败结果；没有将部分结果保存为事实": "The model response was incomplete or failed. Partial results were not saved as facts.",
    "已选择 DeepSeek SDK，但未配置 ALONG_DEEPSEEK_API_KEY": "DeepSeek SDK selected, but ALONG_DEEPSEEK_API_KEY is not configured.",
    "回答引用了未提供的图片，已拒绝展示该结果": "The answer referenced images that were not supplied. The result was rejected.",
    "回答引用了不存在的资料，已拒绝展示该结果": "The answer referenced unavailable sources. The result was rejected.",
    "接线判断引用了无效连接或图片，未采用": "The wiring judgment referenced invalid connections or images and was rejected.",
    "回答期间任务发生变化，请重新提问": "The task changed while the answer was being prepared. Please ask again.",
    "关键结论缺少资料支持，未采用；请补充适用说明书": "A key conclusion lacks source evidence and was rejected. Please provide the applicable manual.",
    "未配置硅基流动语音 API Key，请设置 ALONG_SILICONFLOW_API_KEY": "Set ALONG_SILICONFLOW_API_KEY to enable cloud speech recognition.",
    "不支持的录音格式，请使用 WebM、WAV、MP3、MP4、OGG 或 FLAC": "Unsupported recording format. Use WebM, WAV, MP3, MP4, OGG or FLAC.",
    "语音识别超时，请重试或输入文字": "Speech recognition timed out. Try again or type your question.",
    "无法连接语音识别服务，请检查网络或输入文字": "Cannot connect to speech recognition. Check your network or type your question.",
    "语音识别认证失败，请检查硅基流动 API Key 和权限": "Speech authentication failed. Check your SiliconFlow API key and permissions.",
    "语音识别服务限流，请稍后重试": "Speech recognition is rate-limited. Please try again later.",
    "语音识别服务暂时不可用，请稍后重试": "Speech recognition is temporarily unavailable. Please try again later.",
    "语音识别请求被拒绝，请检查录音格式和模型配置": "Speech request rejected. Check your recording format and model configuration.",
    "语音识别服务返回了无效结果，请重试": "Speech recognition returned an invalid result. Please try again.",
    "语音过长，请控制在一分钟内": "Keep voice input under one minute.",
    "正在识别上一段语音": "The previous voice input is still being transcribed.",
    "本地语音识别不可用；请先运行 along prepare-speech，或使用文字输入": "Local speech recognition is unavailable. Run along prepare-speech first, or type your question.",
    "没有识别到有效语音，请重试或输入文字": "No speech was recognised. Please try again or type your question.",
    "上传超过 40 MB": "Upload exceeds 40 MB.",
}


def localize_error(text: str) -> str:
    if language.get() != "en":
        return text
    if text in ERROR_MESSAGES:
        return ERROR_MESSAGES[text]
    match = re.fullmatch(r"(DeepSeek API|模型接口) HTTP (\d+)；请检查.*", text)
    if match:
        return f"{match[1] if match[1] == 'DeepSeek API' else 'Model API'} HTTP {match[2]}. Check your provider and model configuration."
    return text
