import json
import re

import pytest
from fastapi.testclient import TestClient

from along.api import create_app
from along.config import Settings
from along.schemas import (
    Answer,
    EventDraft,
    FrameSelection,
    Observation,
    PlanDraft,
    Rank,
    ResearchDecision,
    Route,
    SearchQuery,
    Step,
)


class FakeVLM:
    """Deterministic contract double, used only by tests."""

    def __init__(self):
        self.calls = []
        self.bad_evidence = False
        self.research_decision = None

    async def structured(self, prompt, schema, images=None):
        self.calls.append((schema.__name__, images or []))
        if schema is Route:
            context = json.loads(prompt.split("\n")[-1])
            text = context["text"]
            if text == "我们开始吧" and not context["task"]:
                return Route(intent="clarify", clarification="想做什么？")
            if text.startswith("我们一起"):
                return Route(intent="start", goal=text)
            return Route(
                intent="ask",
                related_to_task=bool(context["task"]) and "无关" not in text,
                needs_history="刚才" in text,
            )
        if schema is ResearchDecision:
            if self.research_decision:
                return self.research_decision
            ids = re.findall(r'"id":"(src_[a-z0-9]+)"', prompt)
            return (
                ResearchDecision(action="direct", reason="已有适用资料", source_ids=ids[:1])
                if ids
                else ResearchDecision(
                    action="search",
                    reason="型号资料缺失",
                    information_gap="对应型号安装方向",
                    query="测试型号官方安装方向",
                )
            )
        if schema is PlanDraft:
            ids = re.findall(r'"id":"(src_[a-z0-9]+)"', prompt)
            return PlanDraft(
                title="测试计划",
                steps=[
                    Step(
                        id="s1", title="准备", instruction="对照材料", checks=["确认材料"], source_ids=ids[:1]
                    )
                ],
            )
        if schema is Answer:
            return Answer(
                reply="请核对图示方向，当前证据不能认证安全。",
                evidence_ids=["nonexistent"] if self.bad_evidence else [im.id for im in (images or [])],
                missing=["无法确认内部结构"],
                blocking_missing=["无法确认内部结构"],
                checks=["对照原始说明"],
                suggestion="先查看对应型号说明书",
            )
        if schema is EventDraft:
            return EventDraft(
                summary="手放置滑轨",
                objects=["滑轨"],
                observations=[Observation(claim="可见滑轨", frame_ids=[images[0].id])],
                unknowns=["安装方向未确认"],
            )
        if schema is SearchQuery:
            return SearchQuery(queries=["滑轨 安装"], objects=["滑轨"])
        if schema is FrameSelection:
            return FrameSelection(frame_ids=[im.id for im in (images or [])[:2]])
        if schema is Rank:
            ids = re.findall(r'"id":"(evt_[a-z0-9]+)"', prompt)
            return Rank(event_ids=ids[:5])
        raise AssertionError(schema)

    async def close(self):
        pass


class FakeWeb:
    def __init__(self):
        self.searches = []
        self.fetches = []

    async def search(self, query):
        self.searches.append(query)
        return [{"title": "测试说明", "url": "https://example.com/manual"}]

    async def fetch(self, url):
        self.fetches.append(url)
        return "说明书：确认材料，按对应型号组装。安装方向需要检查。"


@pytest.fixture
def app(tmp_path):
    return create_app(
        Settings(data_dir=tmp_path, worker_enabled=False, semantic_enabled=False), FakeVLM(), FakeWeb()
    )


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        client.headers["X-Along-Token"] = app.state.token
        yield client
