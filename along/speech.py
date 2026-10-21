import asyncio

from along.config import Settings
from along.providers.speech import LocalWhisperSpeechProvider, SiliconFlowSpeechProvider, SpeechProvider
from along.providers.vlm import ProviderError


class Speech:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.lock = asyncio.Lock()
        self.provider: SpeechProvider = (
            SiliconFlowSpeechProvider(settings)
            if settings.speech_provider == "siliconflow"
            else LocalWhisperSpeechProvider(settings)
        )

    async def transcribe(self, raw: bytes, filename="voice.webm", content_type="audio/webm"):
        if len(raw) > 12 * 1024 * 1024:
            raise ValueError("语音过长，请控制在一分钟内")
        if not raw:
            raise ValueError("没有识别到有效语音，请重试或输入文字")
        if self.lock.locked():
            raise ProviderError("正在识别上一段语音")
        async with self.lock:
            text = await self.provider.transcribe(raw, filename, content_type)
            if not text:
                raise ValueError("没有识别到有效语音，请重试或输入文字")
            return text
