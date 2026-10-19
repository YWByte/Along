import asyncio
import tempfile
from pathlib import Path

from along.config import Settings
from along.providers.vlm import ProviderError


class Speech:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = None
        self.lock = asyncio.Lock()

    async def transcribe(self, raw: bytes):
        if len(raw) > 12 * 1024 * 1024:
            raise ValueError("语音过长，请控制在一分钟内")
        if self.lock.locked():
            raise ProviderError("正在识别上一段语音")
        async with self.lock:

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
                            language="zh",
                            vad_filter=True,
                            beam_size=3,
                            condition_on_previous_text=False,
                        )
                        return "".join(segment.text for segment in segments).strip()
                except Exception as exc:
                    raise ProviderError(
                        "本地语音识别不可用；请先运行 along prepare-speech，或使用文字输入"
                    ) from exc

            text = await asyncio.to_thread(run)
            if not text:
                raise ValueError("没有识别到有效语音，请重试或输入文字")
            return text
