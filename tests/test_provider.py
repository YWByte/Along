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
