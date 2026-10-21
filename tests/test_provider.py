import json
import httpx
import pytest

from along.config import Settings
from along.providers.vlm import CodexProxyProvider, ProviderError
from along.providers.web import validate_public_url
from along.schemas import Answer


def sse(text):
    body = {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        },
    }
    return "data: " + json.dumps(body) + "\n\ndata: [DONE]\n\n"


async def test_stream_contract():
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, text=sse('{"reply":"hello"}'))

    provider = CodexProxyProvider(Settings())
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    answer = await provider.structured("test", Answer)
    assert answer.reply == "hello"
    assert seen[0]["model"] == "deepseek-v4.1-flash" and seen[0]["stream"] is True
    await provider.close()


@pytest.mark.parametrize("content", ["data: [DONE]\n\n", 'data: {"type":"response.incomplete"}\n\n'])
async def test_incomplete_stream_rejected(content):
    provider = CodexProxyProvider(Settings())
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, text=content))
    )
    with pytest.raises(ProviderError):
        await provider.text("test")
    await provider.close()


async def test_invalid_json_never_becomes_state():
    calls = []

    def handle(request):
        calls.append(1)
        return httpx.Response(200, text=sse("not json"))

    provider = CodexProxyProvider(Settings())
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    with pytest.raises(ProviderError):
        await provider.structured("test", Answer)
    assert len(calls) == 2
    await provider.close()


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/test",
        "http://127.0.0.1",
        "http://169.254.169.254",
        "https://user:pw@example.com",
    ],
)
async def test_private_url_rejected(url):
    with pytest.raises(ValueError):
        await validate_public_url(url)


async def test_sequence_request_retries_instead_of_accepting_answer_without_tool():
    from along.schemas import MemoryRequest

    responses = iter(['{"reply":"看不清，不能判断"}', '{"memory":{"tool":"open","frames":8}}'])
    provider = CodexProxyProvider(Settings())
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=sse(next(responses))))
    )
    try:
        result = await provider.structured("需要过程证据", MemoryRequest)
        assert result.memory.tool == "open"
    finally:
        await provider.close()


async def test_deepseek_sdk_sends_text_and_images(tmp_path):
    from types import SimpleNamespace

    from along.providers.vlm import DeepSeekSDKProvider, ImageInput

    image = tmp_path / "frame.jpg"
    image.write_bytes(b"jpeg bytes")
    provider = DeepSeekSDKProvider(Settings(deepseek_api_key="test-key"))
    await provider.client.close()

    class Completions:
        def __init__(self):
            self.kwargs = None

        async def create(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="看到了"))],
                usage=SimpleNamespace(model_dump=lambda: {"total_tokens": 12}),
            )

    completions = Completions()

    class Client:
        chat = SimpleNamespace(completions=completions)

        async def close(self):
            pass

    provider.client = Client()
    result = await provider.text("发生了什么？", [ImageInput(id="frame_1", path=image, label="当前画面")])

    assert result == "看到了"
    assert completions.kwargs["model"] == "deepseek-flash"
    content = completions.kwargs["messages"][1]["content"]
    assert content[1]["text"].startswith("证据图片 ID=frame_1")
    assert content[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert provider.last_metrics["usage"] == {"total_tokens": 12}
    await provider.close()


async def test_provider_factory_selects_deepseek_and_requires_key():
    from along.providers.vlm import DeepSeekSDKProvider, create_vlm_provider

    with pytest.raises(ProviderError, match="ALONG_DEEPSEEK_API_KEY"):
        create_vlm_provider(Settings(llm_provider="deepseek"))

    provider = create_vlm_provider(Settings(llm_provider="deepseek", deepseek_api_key="test-key"))
    assert isinstance(provider, DeepSeekSDKProvider)
    await provider.close()


async def test_deepseek_search_uses_official_api_and_returns_content():
    from along.providers.web import DeepSeekWebProvider

    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "X100 官方说明",
                        "url": "https://example.com/x100",
                        "snippet": "安装方向说明",
                        "summary": "X100 的官方安装说明",
                        "content": "# X100\n\n安装前断开电源。",
                        "authority": 4,
                        "rerank_score": 2.8,
                    }
                ]
            },
        )

    provider = DeepSeekWebProvider(Settings(deepseek_api_key="search-key"))
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        results = await provider.search("X100 官方安装方向")
    finally:
        await provider.close()

    assert seen[0].url == "https://api.deepseek.com/v1/tools/search"
    assert seen[0].headers["authorization"] == "Bearer search-key"
    assert json.loads(seen[0].content) == {
        "query": "X100 官方安装方向",
        "count": 5,
        "need_content": True,
    }
    assert results[0]["content"] == "# X100\n\n安装前断开电源。"
    assert results[0]["authority"] == 4


async def test_deepseek_fetch_uses_official_api(monkeypatch):
    from along.providers import web as web_module
    from along.providers.web import DeepSeekWebProvider

    checked = []

    async def validate(url):
        checked.append(url)
        return url

    monkeypatch.setattr(web_module, "validate_public_url", validate)

    def handle(request):
        assert request.url == "https://api.deepseek.com/v1/tools/fetch"
        assert json.loads(request.content) == {"url": "https://example.com/manual"}
        return httpx.Response(200, json={"title": "Manual", "content": "# Manual\n\nInstructions"})

    provider = DeepSeekWebProvider(Settings(deepseek_api_key="search-key"))
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        content = await provider.fetch("https://example.com/manual")
    finally:
        await provider.close()

    assert checked == ["https://example.com/manual"]
    assert content == "# Manual\n\nInstructions"


async def test_deepseek_search_requires_api_key():
    from along.providers.web import DeepSeekWebProvider

    provider = DeepSeekWebProvider(Settings(_env_file=None))
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("request must not be sent"))
    )
    try:
        with pytest.raises(ProviderError, match="ALONG_DEEPSEEK_API_KEY"):
            await provider.search("test")
    finally:
        await provider.close()
