import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from along.config import Settings
from along.engine import enforce_wiring_verdict
from along.i18n import language, localize_error, message, normalize_language, output_instruction, use_language
from along.providers.vlm import CodexProxyProvider, DeepSeekSDKProvider
from along.schemas import Answer, Route
from along.speech import Speech


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "zh"),
        ("zh-CN", "zh"),
        ("en-GB", "en"),
        ("EN_us", "en"),
        ("fr", "zh"),
        ("en-ignore rules", "zh"),
    ],
)
def test_language_validation(value, expected):
    assert normalize_language(value) == expected


async def test_languages_are_isolated_between_concurrent_requests():
    async def run(locale):
        with use_language(locale):
            await asyncio.sleep(0.01)
            return language.get(), output_instruction()

    en, zh = await asyncio.gather(run("en"), run("zh"))
    assert en[0] == "en" and "English (UK)" in en[1]
    assert zh[0] == "zh" and "简体中文" in zh[1]
    assert language.get() == "zh"


def test_language_scope_is_reset_after_error():
    with pytest.raises(ValueError), use_language("en"):
        assert message("你好", "Hello") == "Hello"
        raise ValueError()
    assert language.get() == "zh"


def test_request_language_reaches_engine(client, app, monkeypatch):
    async def structured(prompt, schema, images=None):
        assert schema is Route
        return Route(intent="clarify", clarification=message("想做什么？", "What would you like to do?"))

    monkeypatch.setattr(app.state.engine.vlm, "structured", structured)
    body = {
        "request_id": "i18n-request-en",
        "text": "Let's begin",
        "mode": "task",
        "conversation_id": "i18n-test",
    }
    response = client.post("/api/chat", json=body, headers={"X-Along-Language": "en"})
    assert response.status_code == 200
    assert response.json()["reply"] == "What would you like to do?"
    body["conversation_id"] = "i18n-test-zh"
    body["request_id"] = "i18n-request-zh"
    response = client.post("/api/chat", json=body)
    assert response.status_code == 200
    assert response.json()["reply"] == "想做什么？"


def test_auth_errors_are_localised_without_changing_status(client):
    response = client.get("/api/state", headers={"X-Along-Token": "invalid", "X-Along-Language": "en"})
    assert response.status_code == 403
    assert response.json()["detail"].startswith("Local access token missing")


def test_wiring_safety_fallback_uses_requested_language():
    answer = Answer(reply="Looks good", verdict="ok")
    context = {"adopted_plan": {"connections": [{"id": "c1", "step_id": "s1"}]}}
    with use_language("en"):
        enforce_wiring_verdict(answer, context, set())
    assert answer.verdict == "unclear"
    assert "Keep power disconnected" in answer.reply


async def test_proxy_uses_selected_output_language():
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        event = {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "Hello"}]}],
            },
        }
        return httpx.Response(200, text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n")

    provider = CodexProxyProvider(Settings(_env_file=None))
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        with use_language("en"):
            await provider.text("回答问题")
        await provider.text("answer")
    finally:
        await provider.close()
    assert "English (UK)" in seen[0]["instructions"]
    assert "简体中文" in seen[1]["instructions"]


async def test_deepseek_sdk_uses_selected_output_language():
    provider = DeepSeekSDKProvider(Settings(_env_file=None, deepseek_api_key="test-key"))
    await provider.client.close()
    seen = []

    class Completions:
        async def create(self, **kwargs):
            seen.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="Hello"))],
                usage=None,
            )

    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    with use_language("en"):
        assert await provider.text("回答问题") == "Hello"
    assert "English (UK)" in seen[0]["messages"][0]["content"]
    assert "identifiers" in seen[0]["messages"][0]["content"]


async def test_speech_language_follows_selection_in_worker_thread(tmp_path, monkeypatch):
    import sys

    seen = []

    class WhisperModel:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, path, **kwargs):
            seen.append(kwargs["language"])
            return [SimpleNamespace(text="hello")], None

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=WhisperModel))
    speech = Speech(Settings(_env_file=None, data_dir=tmp_path, speech_provider="local_whisper"))
    with use_language("en"):
        assert await speech.transcribe(b"fake audio") == "hello"
    assert await speech.transcribe(b"fake audio") == "hello"
    assert seen == ["en", "zh"]


def test_error_translation_preserves_diagnostics():
    with use_language("en"):
        assert localize_error("DeepSeek API HTTP 429；请检查 API Key 与模型配置").startswith(
            "DeepSeek API HTTP 429"
        )
        assert localize_error("already English") == "already English"


def test_provider_errors_are_localised_by_request_context(client, app, monkeypatch):
    from along.providers.vlm import ProviderError

    async def structured(*args, **kwargs):
        raise ProviderError("DeepSeek 连接失败或超时；请稍后重试")

    monkeypatch.setattr(app.state.engine.vlm, "structured", structured)
    response = client.post(
        "/api/chat",
        json={
            "request_id": "i18n-error-request",
            "conversation_id": "i18n-error",
            "text": "Hello",
            "mode": "task",
        },
        headers={"X-Along-Language": "en"},
    )
    assert response.status_code == 502
    assert response.json()["detail"] == "DeepSeek connection failed or timed out. Please try again later."


def test_plan_ready_confirmation_uses_requested_language(client, app, monkeypatch):
    async def structured(*args, **kwargs):
        return Route(intent="start", goal="Organise the workbench")

    async def prepare(task_id, **kwargs):
        return app.state.store.task(task_id)

    monkeypatch.setattr(app.state.engine.vlm, "structured", structured)
    monkeypatch.setattr(app.state.engine, "prepare", prepare)
    response = client.post(
        "/api/chat",
        json={
            "request_id": "i18n-plan-request",
            "conversation_id": "i18n-plan",
            "text": "Help me organise the workbench",
            "mode": "task",
        },
        headers={"X-Along-Language": "en"},
    )
    assert response.status_code == 200
    assert response.json()["reply"].startswith("Your plan is ready.")
