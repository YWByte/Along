import asyncio
import tempfile
from pathlib import Path
from typing import Protocol

import httpx

from along.config import Settings
from along.i18n import language
from along.providers.vlm import ProviderError


class SpeechProvider(Protocol):
    async def transcribe(self, raw: bytes, filename: str, content_type: str) -> str: ...


class SiliconFlowSpeechProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def transcribe(self, raw: bytes, filename: str, content_type: str) -> str:
        key = self.settings.siliconflow_api_key
        if key is None or not key.get_secret_value().strip():
            raise ProviderError("未配置硅基流动语音 API Key，请设置 ALONG_SILICONFLOW_API_KEY")
        # Use a known extension; browsers may record MP4 even when the upload is named WebM.
        formats = {
            "audio/webm": ".webm",
            "video/webm": ".webm",
            "audio/mp4": ".m4a",
            "video/mp4": ".mp4",
            "audio/ogg": ".ogg",
            "audio/wav": ".wav",
            "audio/x-wav": ".wav",
            "audio/mpeg": ".mp3",
            "audio/flac": ".flac",
            "audio/x-flac": ".flac",
        }
        mime = content_type.split(";", 1)[0].strip().lower()
        suffix = formats.get(mime)
        if suffix is None and mime == "application/octet-stream":
            extension = Path(filename).suffix.lower()
            if extension in set(formats.values()):
                suffix = extension
        if suffix is None:
            raise ValueError("不支持的录音格式，请使用 WebM、WAV、MP3、MP4、OGG 或 FLAC")
        try:
            async with asyncio.timeout(self.settings.speech_timeout):
                async with httpx.AsyncClient(timeout=self.settings.speech_timeout) as client:
                    response = await client.post(
                        "https://api.siliconflow.cn/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {key.get_secret_value()}"},
                        data={"model": self.settings.siliconflow_asr_model},
                        files={"file": (f"voice{suffix}", raw, mime)},
                    )
        except (TimeoutError, httpx.TimeoutException):
            raise ProviderError("语音识别超时，请重试或输入文字") from None
        except httpx.RequestError:
            raise ProviderError("无法连接语音识别服务，请检查网络或输入文字") from None
        if response.status_code in (401, 403):
            raise ProviderError("语音识别认证失败，请检查硅基流动 API Key 和权限")
        if response.status_code == 429:
            raise ProviderError("语音识别服务限流，请稍后重试")
        if response.status_code >= 500:
            raise ProviderError("语音识别服务暂时不可用，请稍后重试")
        if not response.is_success:
            raise ProviderError("语音识别请求被拒绝，请检查录音格式和模型配置")
        try:
            result = response.json()
        except ValueError:
            raise ProviderError("语音识别服务返回了无效结果，请重试") from None
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise ProviderError("语音识别服务返回了无效结果，请重试")
        return result["text"].strip()


class LocalWhisperSpeechProvider:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = None

    async def transcribe(self, raw: bytes, filename: str, content_type: str) -> str:
        def run():
            try:
                from faster_whisper import WhisperModel

                if self.model is None:
                    self.model = WhisperModel(
                        self.settings.whisper_model,
                        device="cpu",
                        compute_type="int8",
                        local_files_only=True,
                    )
                with tempfile.TemporaryDirectory(prefix="along-speech-") as folder:
                    path = Path(folder) / "input.audio"
                    path.write_bytes(raw)
                    segments, _ = self.model.transcribe(
                        str(path),
                        language=language.get(),
                        vad_filter=True,
                        beam_size=3,
                        condition_on_previous_text=False,
                    )
                    return "".join(segment.text for segment in segments).strip()
            except Exception:
                raise ProviderError(
                    "本地语音识别不可用；请先运行 along prepare-speech，或使用文字输入"
                ) from None

        return await asyncio.to_thread(run)
