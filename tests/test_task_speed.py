import json

import pytest

from along.engine import installation_decision
from along.schemas import AnswerAudit, ResearchDecision, CompanionTurn, CompanionReply, Lookback
from along.store import dump
from test_flows import ask, change, start, upload


def confirmed(app, client, scene="installation", critical=False):
    task = change(client, start(client), "confirm_plan").json()
    with app.state.store.tx() as db:
        state = task["state"]
        state.update(scenario=scene, research={"verification_required": critical})
        db.execute("UPDATE tasks SET state=? WHERE id=?", (dump(state), task["id"]))
    return task


def test_unknown_installation_model_blocks_search_and_plan(app, client):
    app.state.engine.vlm.research_decision = ResearchDecision(
        scenario="installation", action="direct", reason="普通耳机"
    )
    task = client.post("/api/tasks", json={"goal": "重新连接耳机"}).json()
    response = client.post(f"/api/tasks/{task['id']}/prepare", json={})
    assert response.status_code == 200
    task = response.json()
    assert task["plan"] is None and len(task["state"]["pending_questions"]) == 1
    assert "型号" in task["state"]["pending_questions"][0]
    assert not app.state.engine.web.searches
    assert not any(schema == "PlanDraft" for schema, _ in app.state.engine.vlm.calls)


@pytest.mark.parametrize("evidence", ["user", "label"])
def test_known_installation_model_searches_before_plan(app, client, evidence):
    vlm = app.state.engine.vlm
    vlm.research_decision = ResearchDecision(
        scenario="installation",
        action="search",
        reason="配对方法随型号变化",
        information_gap="该型号配对方法",
        query="Device A1 official manual",
        device_model="Device A1",
        model_evidence=evidence,
    )
    task = client.post("/api/tasks", json={"goal": "设置 Device A1"}).json()
    task = client.post(f"/api/tasks/{task['id']}/prepare", json={}).json()
    assert task["state"]["device"] == {"model": "Device A1", "evidence": evidence}
    assert "Device A1" in app.state.engine.web.searches[0]
    assert task["plan"]["basis"] == "references"
    assert not task["state"]["pending_questions"]


def test_brand_or_unconfirmed_label_is_not_a_model():
    decision = ResearchDecision(
        scenario="installation", action="direct", reason="外观相似", device_model="EARFREE"
    )
    assert installation_decision(decision, None).action == "clarify"


def test_stop_is_never_overridden_by_model_gate():
    decision = ResearchDecision(scenario="installation", action="stop", reason="停止带电拆修")
    assert installation_decision(decision, "installation").action == "stop"


def test_initial_route_installation_cannot_be_downgraded_to_generic():
    decision = ResearchDecision(action="direct", reason="普通设备")
    assert installation_decision(decision, "installation").action == "clarify"


def test_installation_reuses_selected_sources_without_search(app, client):
    vlm = app.state.engine.vlm
    vlm.research_decision = ResearchDecision(
        scenario="installation",
        action="search",
        reason="已提供型号",
        information_gap="该型号说明",
        query="A1 official manual",
        device_model="A1",
        model_evidence="user",
    )
    task = client.post("/api/tasks", json={"goal": "设置 A1"}).json()
    task = client.post(f"/api/tasks/{task['id']}/prepare", json={}).json()
    source_ids = task["state"]["plan_source_ids"]
    vlm.research_decision = ResearchDecision(
        scenario="installation",
        action="direct",
        reason="复用适用说明",
        device_model="A1",
        model_evidence="user",
        source_ids=source_ids,
    )
    result = client.post(f"/api/tasks/{task['id']}/prepare", json={})
    assert result.status_code == 200
    assert len(app.state.engine.web.searches) == 1


def test_state_confirmation_is_one_call_with_current_photo(app, client):
    task = confirmed(app, client)
    frame = upload(client, task)
    calls = []

    async def respond(prompt, schema, images=None):
        calls.append(schema.__name__)
        assert schema is CompanionTurn
        assert images[0].id == frame["id"]
        return CompanionTurn(
            answer=CompanionReply(reply="盖子已经打开。", verdict="ok", evidence_ids=[frame["id"]])
        )

    app.state.engine.vlm.structured = respond
    response = ask(client, "现在盖子开着吗？", task_id=task["id"], frame_id=frame["id"])
    assert response.status_code == 200, response.text
    result = response.json()
    assert calls == ["CompanionTurn"]
    assert result["verdict"] == "ok"
    assert result["retrieval"]["question_frames"][0]["id"] == frame["id"]
    assert result["retrieval"]["visual_reviews"] == []
    assert result["retrieval"]["answer_audit"] is None
    assert len(result["timings"]["stages"]) == 1


def test_process_question_uses_attached_overview_without_extra_calls(app, client):
    task = confirmed(app, client)
    frames = [upload(client, task) for _ in range(15)]
    calls = []

    async def respond(prompt, schema, images=None):
        calls.append(schema.__name__)
        context = json.loads(prompt.split("\n")[-1])
        assert context["adopted_plan"] and context["task_event_directory"]
        assert len(images) == 2 and not images[0].frame_ids and len(images[1].frame_ids) == 12
        return CompanionTurn(answer=CompanionReply(reply="已放入零件。", verdict="ok", evidence_ids=["1"]))

    app.state.engine.vlm.structured = respond
    result = ask(client, "刚才放入了吗？", task_id=task["id"]).json()
    assert calls == ["CompanionTurn"]
    assert result["retrieval"]["tools"][0]["automatic"]
    assert result["retrieval"]["tools"][0]["result"]["layout"] == "sheet"
    assert len(result["evidence_ids"]) == 1
    assert result["evidence_ids"][0] in {f["id"] for f in frames}


def test_explicit_lookback_is_at_most_one_extra_call(app, client):
    from test_memory_agent import event

    task = confirmed(app, client)
    frames = [upload(client, task) for _ in range(4)]
    event(app.state.store, task["id"], "evt_old", frames[:2])
    calls = []

    async def respond(prompt, schema, images=None):
        calls.append(schema.__name__)
        if schema is CompanionTurn:
            return CompanionTurn(
                lookback=Lookback(
                    event_id="evt_old",
                    reason="用户要求核对之前操作",
                    fallback_question="零件现在插到底了吗？",
                )
            )
        assert schema is CompanionReply
        assert len(images) == 2 and list(images[1].frame_ids) == [f["id"] for f in frames[:2]]
        return CompanionReply(reply="此前插入方向正确。", verdict="ok", evidence_ids=["1"])

    app.state.engine.vlm.structured = respond
    response = ask(client, "看一下之前那步的画面", task_id=task["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert calls == ["CompanionTurn", "CompanionReply"]
    assert body["retrieval"]["events"] == ["evt_old"]
    assert body["evidence_ids"] == [frames[0]["id"]]
    assert len(body["retrieval"]["tools"]) == 2


def test_ambiguous_question_asks_one_state_question_without_lookup(app, client):
    task = confirmed(app, client)
    calls = []

    async def respond(prompt, schema, images=None):
        calls.append(schema.__name__)
        assert "不为模糊询问翻历史" in prompt and "每轮最多一个必要问题" in prompt
        return CompanionTurn(answer=CompanionReply(reply="", ask_user="指示灯现在是什么颜色？"))

    app.state.engine.vlm.structured = respond
    response = ask(client, "我这样对吗？", task_id=task["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reply"] == "指示灯现在是什么颜色？"
    assert body["verdict"] == "none"
    assert not body["retrieval"]["tools"]
    assert calls == ["CompanionTurn"]


@pytest.mark.parametrize("missing_event", [True, False])
def test_unavailable_or_slow_lookback_returns_specific_question(app, client, missing_event):
    import asyncio
    from test_memory_agent import event

    task = confirmed(app, client)
    frame = upload(client, task)
    if not missing_event:
        event(app.state.store, task["id"], "evt_old", [frame])
    app.state.memory.settings.task_answer_timeout = 0.02

    async def respond(prompt, schema, images=None):
        if schema is CompanionTurn:
            return CompanionTurn(
                lookback=Lookback(event_id="evt_old", fallback_question="零件现在插到底了吗？")
            )
        await asyncio.sleep(1)
        return CompanionReply(reply="不应出现")

    app.state.engine.vlm.structured = respond
    response = ask(client, "看看之前的画面", task_id=task["id"])
    assert response.status_code == 200, response.text
    assert response.json()["reply"] == "零件现在插到底了吗？"
    assert not app.state.engine.interaction.locked() and not app.state.memory.user_busy


def test_initial_timeout_returns_question_and_is_idempotent(app, client):
    import asyncio

    task = confirmed(app, client)
    app.state.memory.settings.task_answer_timeout = 0.01
    calls = []

    async def slow(prompt, schema, images=None):
        calls.append(schema.__name__)
        await asyncio.sleep(1)
        return CompanionTurn(answer=CompanionReply(reply="不应展示"))

    app.state.engine.vlm.structured = slow
    payload = {
        "request_id": "timeout_request",
        "conversation_id": "conversation1",
        "text": "现在对吗",
        "task_id": task["id"],
    }
    result = client.post("/api/chat", json=payload)
    assert result.status_code == 200, result.text
    assert result.json()["reply"].endswith("？")
    assert client.post("/api/chat", json=payload).json() == result.json()
    assert calls == ["CompanionTurn"]
    assert not app.state.engine.interaction.locked() and not app.state.memory.user_busy


def test_safety_critical_answer_keeps_audit(app, client):
    task = confirmed(app, client)
    upload(client, task)
    calls = []

    async def respond(prompt, schema, images=None):
        calls.append(schema.__name__)
        if schema is CompanionTurn:
            return CompanionTurn(safety_critical=True, answer=CompanionReply(reply="先保持断电。"))
        assert schema is AnswerAudit
        return AnswerAudit(action="keep")

    app.state.engine.vlm.structured = respond
    response = ask(client, "可以通电了吗", task_id=task["id"])
    assert response.status_code == 200, response.text
    assert calls == ["CompanionTurn", "AnswerAudit"]
    assert response.json()["retrieval"]["answer_audit"]["action"] == "keep"


def test_unrelated_question_does_not_change_task(app, client):
    task = confirmed(app, client)

    async def respond(prompt, schema, images=None):
        return CompanionTurn(related_to_task=False, answer=CompanionReply(reply="这是常识。"))

    app.state.engine.vlm.structured = respond
    response = ask(client, "一个无关问题", task_id=task["id"])
    assert response.status_code == 200, response.text
    assert response.json()["task_id"] is None
    assert app.state.store.task(task["id"])["version"] == task["version"]


@pytest.mark.parametrize("intent", ["pause", "resume", "finish"])
def test_task_control_uses_single_decision(app, client, intent):
    task = confirmed(app, client)
    calls = []

    async def respond(prompt, schema, images=None):
        calls.append(schema.__name__)
        return CompanionTurn(intent=intent)

    app.state.engine.vlm.structured = respond
    response = ask(client, "确认操作", task_id=task["id"])
    assert response.status_code == 200, response.text
    assert response.json()["action"] == intent
    assert calls == ["CompanionTurn"]


def test_unprovided_image_reference_is_rejected(app, client):
    task = confirmed(app, client)

    async def respond(prompt, schema, images=None):
        return CompanionTurn(answer=CompanionReply(reply="正确", verdict="ok", evidence_ids=["invented"]))

    app.state.engine.vlm.structured = respond
    assert ask(client, "对吗", task_id=task["id"]).status_code == 502


def test_execution_catalog_keeps_every_event_index_and_summary():
    from along.engine import execution_directory

    catalog = {
        "mode": "full",
        "steps": [
            {
                "step_id": "s1",
                "events": [
                    {
                        "id": f"e{i}",
                        "summary_not_fact": f"summary {i}",
                        "observations_not_fact": ["duplicate claim"],
                    }
                    for i in range(5)
                ],
            }
        ],
    }
    compact = execution_directory(catalog)
    assert [e["id"] for e in compact["steps"][0]["events"]] == [f"e{i}" for i in range(5)]
    assert all(e["summary_not_fact"] for e in compact["steps"][0]["events"])
    assert all("observations_not_fact" not in e for e in compact["steps"][0]["events"])
    assert "observations_not_fact" in catalog["steps"][0]["events"][0]


def test_model_question_is_asked_only_once():
    brand_only = ResearchDecision(
        scenario="installation",
        action="clarify",
        reason="只有品牌",
        device_model="弱水石沙",
        questions=["型号？"],
    )
    assert installation_decision(brand_only, "installation", model_asked=True).action == "direct"
    brand_search = brand_only.model_copy(
        update={"action": "search", "information_gap": "配对方法", "query": "弱水石沙 配对", "questions": []}
    )
    assert installation_decision(brand_search, "installation", model_asked=True).action == "search"
    unknown = ResearchDecision(scenario="installation", action="clarify", reason="未知", questions=["型号？"])
    assert installation_decision(unknown, "installation", model_asked=True).action == "direct"


def test_known_model_ordinary_use_does_not_force_search():
    decision = ResearchDecision(
        scenario="installation", action="direct", reason="普通使用", device_model="A1", model_evidence="user"
    )
    assert installation_decision(decision, "installation").action == "direct"


def test_plan_cannot_reask_model_after_it_was_asked(app, client):
    vlm = app.state.engine.vlm
    vlm.research_decision = ResearchDecision(
        scenario="installation", action="clarify", reason="型号未知", questions=["型号？"]
    )
    task = client.post("/api/tasks", json={"goal": "重新连接耳机"}).json()
    task = client.post(f"/api/tasks/{task['id']}/prepare", json={}).json()
    assert task["state"]["model_question_asked"]
    task = client.post(f"/api/tasks/{task['id']}/prepare", json={}).json()
    assert not any("型号" in q for q in task["state"]["pending_questions"])
    assert task["plan"]


def test_implicit_visual_uncertainty_shows_only_state_question():
    from along.engine import Engine

    reply = CompanionReply(
        reply="照片没有看清灯光。", headline="画面看不清", ask_user="指示灯现在是什么颜色？"
    )
    implicit = Engine.companion_answer(reply)
    explicit = Engine.companion_answer(reply, visual_requested=True)
    assert implicit.reply == "指示灯现在是什么颜色？" and not implicit.headline
    assert "照片" in explicit.reply and explicit.reply.endswith("指示灯现在是什么颜色？")


def test_safety_audit_timeout_keeps_stop_instruction(app, client):
    import asyncio

    task = confirmed(app, client)
    upload(client, task)
    app.state.memory.settings.task_answer_timeout = 0.02

    async def respond(prompt, schema, images=None):
        if schema is CompanionTurn:
            return CompanionTurn(safety_critical=True, answer=CompanionReply(reply="可以通电"))
        await asyncio.sleep(1)
        return AnswerAudit(action="keep")

    app.state.engine.vlm.structured = respond
    response = ask(client, "可以通电吗", task_id=task["id"])
    assert response.status_code == 200, response.text
    assert response.json()["reply"].startswith("先不要进行通电")
    assert "可以通电" not in response.json()["reply"]
    assert response.json()["verdict"] == "none"


def test_requesting_new_task_does_not_replace_existing_task(app, client):
    task = confirmed(app, client)

    async def respond(prompt, schema, images=None):
        return CompanionTurn(intent="start", goal="安装另一台设备")

    app.state.engine.vlm.structured = respond
    response = ask(client, "我们开始安装另一台设备", task_id=task["id"])
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "clarify"
    assert response.json()["task_id"] == task["id"]
    assert app.state.store.task(task["id"])["version"] == task["version"]


def test_unresolved_state_question_cannot_publish_wrong_verdict():
    from along.engine import Engine

    reply = CompanionReply(
        reply="没看到变化，所以做错了。", verdict="adjust", suggestion="重做一次", ask_user="现在是什么状态？"
    )
    answer = Engine.companion_answer(reply)
    assert answer.verdict == "none" and answer.suggestion is None
    assert answer.reply == "现在是什么状态？"
