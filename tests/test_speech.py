import asyncio

import httpx
import pytest
from pydantic import SecretStr

from along.config import Settings
from along.i18n import localize_error, use_language
from along.providers.vlm import ProviderError
from along.speech import Speech


HTTP_CLIENT = httpx.AsyncClient


def settings(tmp_path, **kwargs):
    return Settings(_env_file=None, data_dir=tmp_path, siliconflow_api_key=SecretStr("test-secret"), **kwargs)


def mock_cloud(monkeypatch, handler):
    original = HTTP_CLIENT
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )


async def test_cloud_upload_keeps_audio_and_uses_safe_filename(tmp_path, monkeypatch):
    seen = []

    def handle(request):
        seen.append(request)
        assert request.url == "https://api.siliconflow.cn/v1/audio/transcriptions"
        assert request.headers["Authorization"] == "Bearer test-secret"
        assert b"FunAudioLLM/SenseVoiceSmall" in request.content
        assert b'filename="voice.m4a"' in request.content
        assert b"audio/mp4" in request.content
        assert b"test audio" in request.content
        assert b"../../" not in request.content
        return httpx.Response(200, json={"text": " GPIO21 to SDA "})

    mock_cloud(monkeypatch, handle)
    speech = Speech(settings(tmp_path))
    with use_language("en"):
        assert (
            await speech.transcribe(b"test audio", "../../voice.webm", "audio/mp4; codecs=mp4a")
            == "GPIO21 to SDA"
        )
    assert len(seen) == 1
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "认证失败"),
        (403, "认证失败"),
        (429, "限流"),
        (500, "暂时不可用"),
        (503, "暂时不可用"),
        (400, "请求被拒绝"),
        (404, "请求被拒绝"),
    ],
)
async def test_cloud_errors_are_safe_and_lock_is_released(tmp_path, monkeypatch, status, expected):
    mock_cloud(
        monkeypatch, lambda req: httpx.Response(status, text="test-secret private provider diagnostic")
    )
    speech = Speech(settings(tmp_path))
    with pytest.raises(ProviderError, match=expected) as caught:
        await speech.transcribe(b"audio")
    assert "test-secret" not in str(caught.value)
    assert not speech.lock.locked()
    with use_language("en"):
        assert localize_error(str(caught.value)) != str(caught.value)


@pytest.mark.parametrize("body", [{}, {"text": None}, {"text": 123}, []])
async def test_invalid_result(tmp_path, monkeypatch, body):
    mock_cloud(monkeypatch, lambda req: httpx.Response(200, json=body))
    with pytest.raises(ProviderError, match="无效结果"):
        await Speech(settings(tmp_path)).transcribe(b"audio")


async def test_empty_and_non_json_result(tmp_path, monkeypatch):
    mock_cloud(monkeypatch, lambda req: httpx.Response(200, json={"text": "  "}))
    with pytest.raises(ValueError, match="没有识别"):
        await Speech(settings(tmp_path)).transcribe(b"audio")
    mock_cloud(monkeypatch, lambda req: httpx.Response(200, text="not json"))
    with pytest.raises(ProviderError, match="无效结果"):
        await Speech(settings(tmp_path)).transcribe(b"audio")


@pytest.mark.parametrize(
    "exc,expected",
    [
        (httpx.ReadTimeout("test-secret"), "超时"),
        (httpx.ConnectError("test-secret"), "无法连接"),
    ],
)
async def test_network_errors_do_not_leak_secret(tmp_path, monkeypatch, exc, expected):
    def handle(request):
        raise exc

    mock_cloud(monkeypatch, handle)
    with pytest.raises(ProviderError, match=expected) as caught:
        await Speech(settings(tmp_path)).transcribe(b"audio")
    assert "test-secret" not in str(caught.value)
    assert caught.value.__suppress_context__


async def test_missing_key_and_invalid_audio_do_not_call_network(tmp_path, monkeypatch):
    def handle(request):
        pytest.fail("invalid input must not reach the cloud")

    mock_cloud(monkeypatch, handle)
    speech = Speech(settings(tmp_path))
    for raw in [b"", b"a" * (12 * 1024 * 1024 + 1)]:
        with pytest.raises(ValueError):
            await speech.transcribe(raw)
    with pytest.raises(ValueError, match="不支持"):
        await speech.transcribe(b"audio", "voice.html", "text/html")
    speech = Speech(Settings(_env_file=None, data_dir=tmp_path, siliconflow_api_key=None))
    with pytest.raises(ProviderError, match="未配置"):
        await speech.transcribe(b"audio")


async def test_simultaneous_requests_and_cancellation(tmp_path, monkeypatch):
    started = asyncio.Event()
    finish = asyncio.Event()

    async def handle(request):
        started.set()
        await finish.wait()
        return httpx.Response(200, json={"text": "hello"})

    mock_cloud(monkeypatch, handle)
    speech = Speech(settings(tmp_path))
    pending = asyncio.create_task(speech.transcribe(b"audio"))
    await started.wait()
    with pytest.raises(ProviderError, match="上一段"):
        await speech.transcribe(b"another audio")
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not speech.lock.locked()
    finish.set()
    assert await speech.transcribe(b"retry") == "hello"


async def test_wall_clock_timeout(tmp_path, monkeypatch):
    async def handle(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={"text": "hello"})

    mock_cloud(monkeypatch, handle)
    config = settings(tmp_path)
    config.speech_timeout = 0.01
    speech = Speech(config)
    with pytest.raises(ProviderError, match="超时"):
        await speech.transcribe(b"audio")
    assert not speech.lock.locked()


def test_speech_api_upload_and_english_error(client, monkeypatch):
    async def transcribe(self, raw, filename, content_type):
        assert raw == b"webm audio"
        assert filename == "voice.webm"
        assert content_type == "audio/webm"
        raise ProviderError("语音识别服务限流，请稍后重试")

    monkeypatch.setattr(Speech, "transcribe", transcribe)
    response = client.post(
        "/api/speech",
        files={"file": ("voice.webm", b"webm audio", "audio/webm")},
        headers={"X-Along-Language": "en"},
    )
    assert response.status_code == 502
    assert "rate-limited" in response.json()["detail"]
