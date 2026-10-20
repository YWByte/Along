import json

import pytest
from pydantic import ValidationError

from along.providers.vlm import ProviderError
from along.schemas import Answer, Mutation, PlanDraft, ResearchDecision
from test_flows import ask


def direct():
    return ResearchDecision(action="direct", reason="常见低风险任务，不需要外部资料")


def search(critical=False):
    return ResearchDecision(
        action="search",
        reason="需要核实对应型号参数",
        information_gap="X100 的安装方向",
        query="X100 官方安装方向",
        verification_required=critical,
    )


def create(client):
    return client.post("/api/tasks", json={"goal": "整理桌面"}).json()


def prepare(client, task, **body):
    return client.post(f"/api/tasks/{task['id']}/prepare", json=body)


def test_common_task_does_not_search_or_fetch(app, client):
    app.state.engine.vlm.research_decision = direct()
    task = prepare(client, create(client)).json()
    assert task["plan"]["basis"] == "general_knowledge"
    assert task["plan"]["steps"][0]["source_ids"] == []
    assert not app.state.engine.web.searches and not app.state.engine.web.fetches
    assert "未采用外部资料" in task["state"]["warnings"][0]
    assert task["state"]["research"]["action"] == "direct"
    assert (
        client.post(
            f"/api/tasks/{task['id']}/change", json={"version": task["version"], "action": "confirm_plan"}
        ).status_code
        == 200
    )


def test_clarify_before_plan_and_resume_after_answer(app, client):
    vlm = app.state.engine.vlm
    vlm.research_decision = ResearchDecision(
        action="clarify", reason="缺少型号", questions=["设备的型号是什么？"]
    )
    result = ask(client, "我们一起修理设备").json()
    assert result["kind"] == "clarify" and "型号" in result["reply"]
    task = client.get("/api/state").json()["task"]
    assert task["plan"] is None and not task["state"]["preparing"]
    assert not app.state.engine.web.searches
    assert not any(c[0] == "PlanDraft" for c in vlm.calls)
    assert (
        client.post(
            f"/api/tasks/{task['id']}/change", json={"version": task["version"], "action": "confirm_plan"}
        ).status_code
        == 409
    )
    vlm.research_decision = direct()
    task = prepare(client, task, reason="只整理设备旁边桌面，不维修设备").json()
    assert not task["state"]["pending_questions"] and task["plan"]


def test_clarification_invalidates_old_plan_confirmation(app, client):
    engine = app.state.engine
    engine.vlm.research_decision = direct()
    task = prepare(client, create(client)).json()
    task = engine.mutate(task["id"], Mutation(version=task["version"], action="confirm_plan"))
    task = engine.mutate(task["id"], Mutation(version=task["version"], action="pause"))
    engine.vlm.research_decision = ResearchDecision(
        action="clarify", reason="材料已更换", questions=["新材料是什么？"]
    )
    task = prepare(client, task).json()
    assert task["plan"] and not task["state"]["plan_confirmed"]
    assert (
        client.post(
            f"/api/tasks/{task['id']}/change", json={"version": task["version"], "action": "confirm_plan"}
        ).status_code
        == 409
    )


def test_targeted_search_then_reuse_existing_sources(app, client):
    task = prepare(client, create(client)).json()
    assert app.state.engine.web.searches == ["测试型号官方安装方向"]
    assert task["plan"]["basis"] == "references"
    prepare(client, task, reason="调整顺序")
    assert len(app.state.engine.web.searches) == 1


def test_user_url_does_not_trigger_additional_search(app, client):
    task = prepare(client, create(client), url="https://example.com/user-manual").json()
    assert task["plan"]["basis"] == "references"
    assert app.state.engine.web.fetches == ["https://example.com/user-manual"]
    assert app.state.engine.web.searches == []


def test_bad_plan_reference_is_rejected_even_without_search(app, client):
    vlm = app.state.engine.vlm
    vlm.research_decision = direct()
    original = vlm.structured

    async def bad(prompt, schema, images=None):
        answer = await original(prompt, schema, images)
        if schema is PlanDraft:
            answer.steps[0].source_ids = ["invented"]
        return answer

    vlm.structured = bad
    result = prepare(client, create(client))
    assert result.status_code == 502 and "未提供" in result.json()["detail"]


def test_empty_search_never_silently_falls_back_to_unverified_plan(app, client):
    async def empty(query):
        return []

    app.state.engine.web.search = empty
    result = prepare(client, create(client))
    assert result.status_code == 502 and "未使用通用知识替代" in result.json()["detail"]
    assert not any(c[0] == "PlanDraft" for c in app.state.engine.vlm.calls)


def test_decision_contract_requires_gap_query_and_safe_basis():
    with pytest.raises(ValidationError):
        ResearchDecision(action="search", reason="我想搜一下")
    with pytest.raises(ValidationError):
        ResearchDecision(action="direct", reason="模型自信", verification_required=True)
    with pytest.raises(ValidationError):
        ResearchDecision(action="clarify", reason="缺少条件")


def test_critical_steps_must_have_references(app, client):
    vlm = app.state.engine.vlm
    vlm.research_decision = search(critical=True)
    original = vlm.structured

    async def no_reference(prompt, schema, images=None):
        result = await original(prompt, schema, images)
        if schema is PlanDraft:
            result.steps[0].source_ids = []
        return result

    vlm.structured = no_reference
    result = prepare(client, create(client))
    assert result.status_code == 502 and "关键操作" in result.json()["detail"]


def test_in_task_search_is_bounded_and_does_not_rewrite_plan(app, client):
    engine = app.state.engine
    engine.vlm.research_decision = direct()
    task = prepare(client, create(client)).json()
    original = engine.vlm.structured
    rounds = []

    async def answer(prompt, schema, images=None):
        if issubclass(schema, Answer):
            context = json.loads(prompt.split("\n")[-1])
            rounds.append(context)
            if not context["reference_sources"]:
                return Answer(reply="需要查一下具体安装方向", research=search())
            return Answer(
                reply="资料中有对应方向，请对照。", source_ids=[context["reference_sources"][0]["id"]]
            )
        return await original(prompt, schema, images)

    engine.vlm.structured = answer
    response = ask(client, "X100 应该朝哪个方向安装？", task_id=task["id"])
    assert response.status_code == 200, response.text
    assert len(rounds) == 2 and rounds[-1]["research_available"] is False
    assert engine.web.searches == ["X100 官方安装方向"]
    assert client.get("/api/state").json()["task"]["plan"] == task["plan"]
    assert response.json()["research"]["information_gap"]
    # A follow-up reuses cited task evidence, not unrelated saved pages.
    response = ask(client, "再解释一下这个方向", task_id=task["id"])
    assert response.status_code == 200
    assert len(engine.web.searches) == 1


def test_repeated_tool_request_is_rejected_without_search_loop(app, client):
    engine = app.state.engine
    engine.vlm.research_decision = direct()
    task = prepare(client, create(client)).json()
    original = engine.vlm.structured

    async def always_search(prompt, schema, images=None):
        if issubclass(schema, Answer):
            return Answer(reply="再搜", research=search())
        return await original(prompt, schema, images)

    engine.vlm.structured = always_search
    response = ask(client, "查一下型号", task_id=task["id"])
    assert response.status_code == 502 and "上限" in response.json()["detail"]
    assert len(engine.web.searches) == 1


def test_search_failure_does_not_emit_intermediate_suggestion(app, client):
    engine = app.state.engine
    engine.vlm.research_decision = direct()
    task = prepare(client, create(client)).json()
    original = engine.vlm.structured

    async def request_search(prompt, schema, images=None):
        if issubclass(schema, Answer):
            return Answer(reply="待查", research=search(), suggestion="未经核验的动作")
        return await original(prompt, schema, images)

    async def failure(query):
        raise ProviderError("网络不可用")

    engine.vlm.structured, engine.web.search = request_search, failure
    response = ask(client, "核实型号", task_id=task["id"])
    assert response.status_code == 502
    assert client.get("/api/state").json()["task"]["suggestions"] == []


def test_ordinary_task_question_and_instant_question_do_not_search(app, client):
    app.state.engine.vlm.research_decision = direct()
    task = prepare(client, create(client)).json()
    assert ask(client, "现在桌面是什么状态？", task_id=task["id"]).status_code == 200
    assert ask(client, "这是什么", mode="instant").status_code == 200
    assert app.state.engine.web.searches == []
