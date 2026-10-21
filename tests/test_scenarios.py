import json

import httpx
import pytest

from along.observation import observe_sequence
from along.providers.vlm import CodexProxyProvider, ProviderError
from along.schemas import (
    AnswerAudit,
    Answer,
    MemoryCall,
    ResearchDecision,
    ReviewFocus,
    Route,
    VisualChange,
    VisualReview,
)
from along.config import Settings
from test_flows import ask, change, start, upload


def unconfirmed_harness(app, task):
    return app.state.store.change(
        task["id"],
        task["version"],
        lambda row, state, plan, db: (
            row["status"],
            {**state, "plan_confirmed": False},
            plan,
            "test_unconfirmed_harness",
            {},
        ),
    )


@pytest.mark.parametrize("scene", ["installation", "exercise"])
def test_preparation_persists_scene_and_loads_skill(app, client, scene):
    app.state.engine.vlm.research_decision = ResearchDecision(
        action="direct",
        reason="常见操作",
        scenario=scene,
        device_model="Demo device A1",
        model_evidence="user",
    )
    task = start(client)
    response = client.post(f"/api/tasks/{task['id']}/prepare", json={})
    assert response.status_code == 200, response.text
    assert response.json()["state"]["scenario"] == scene


def scene_route(original, scene, focus):
    async def respond(prompt, schema, images=None):
        if issubclass(schema, Route):
            return Route(
                intent="ask",
                related_to_task=True,
                needs_history=True,
                scenario=scene,
                review_focus=focus,
                review_kind="motion",
            )
        if schema is AnswerAudit:
            return AnswerAudit(action="keep")
        if schema is VisualReview:
            assert "recent_messages" not in prompt and "summary_not_fact" not in prompt
            return VisualReview(
                visibility="limited",
                summary="对象相对支撑处改变方向。",
                changes=[VisualChange(claim="前后方向不同", frame_ids=[images[0].id, images[-1].id])],
            )
        return await original(prompt, schema, images)

    return respond


def test_unconfirmed_review_isolated_and_focus_persists_without_completing_step(app, client):
    app.state.engine.vlm.research_decision = ResearchDecision(
        action="direct",
        reason="普通设备",
        scenario="installation",
        device_model="Demo device A1",
        model_evidence="user",
    )
    task = change(client, start(client), "confirm_plan").json()
    task = unconfirmed_harness(app, task)
    for _ in range(8):
        upload(client, task)
    focus = ReviewFocus(subject="连接件的方向", step_id="s1", plan_revision=1)
    original = scene_route(app.state.engine.vlm.structured, "installation", focus)
    rounds = 0

    async def respond(prompt, schema, images=None):
        nonlocal rounds
        if issubclass(schema, Answer):
            rounds += 1
            context = json.loads(prompt.split("\n")[-1])
            if rounds == 1:
                return Answer(reply="", memory=MemoryCall(tool="open", frames=8))
            assert context["visual_reviews"][0]["result"]["changes"]
            assert context["review_focus"]["subject"] == "连接件的方向"
            return Answer(reply="能看到朝向变化，不能确认连接牢固。", evidence_ids=[images[0].id])
        return await original(prompt, schema, images)

    app.state.engine.vlm.structured = respond
    response = ask(client, "刚才接好了吗", task_id=task["id"])
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["retrieval"]["visual_reviews"]
    state = app.state.store.task(task["id"])["state"]
    assert state["review_focus"]["subject"] == focus.subject
    assert state["steps"] == task["state"]["steps"]


@pytest.mark.parametrize("bad_ids", [["foreign", "other"], ["a", "a"], ["b", "a"]])
async def test_observation_rejects_fabricated_duplicate_or_reverse_evidence(bad_ids):
    from along.providers.vlm import ImageInput
    from pathlib import Path

    class Provider:
        async def structured(self, *args):
            return VisualReview(
                visibility="clear", summary="变化", changes=[VisualChange(claim="变化", frame_ids=bad_ids)]
            )

    with pytest.raises(ProviderError):
        await observe_sequence(
            Provider(), "exercise", {}, [ImageInput("a", Path("a")), ImageInput("b", Path("b"))]
        )


def test_unconfirmed_window_replacement_keeps_audit_and_allows_old_evidence(app, client):
    task = change(client, start(client), "confirm_plan").json()
    task = unconfirmed_harness(app, task)
    for _ in range(16):
        upload(client, task)
    original = app.state.engine.vlm.structured
    calls = 0
    first_id = None

    async def respond(prompt, schema, images=None):
        nonlocal calls, first_id
        if issubclass(schema, Answer):
            calls += 1
            assert len(images or []) <= 12
            if calls == 1:
                return Answer(reply="", memory=MemoryCall(tool="open", frames=12))
            if calls == 2:
                first_id = images[0].id
                return Answer(reply="", memory=MemoryCall(tool="open", frames=6, replace_images=True))
            assert len(images) == 6
            return Answer(reply="看到了变化", evidence_ids=[first_id])
        return await original(prompt, schema, images)

    app.state.engine.vlm.structured = respond
    result = ask(client, "过程怎么样", task_id=task["id"])
    assert result.status_code == 200, result.text
    assert first_id in result.json()["retrieval"]["frames"]
    assert len(result.json()["retrieval"]["tools"]) == 2


def test_unconfirmed_unusable_observation_cannot_publish_negative_verdict(app, client):
    app.state.engine.vlm.research_decision = ResearchDecision(
        action="direct", reason="普通放松", scenario="exercise"
    )
    task = change(client, start(client), "confirm_plan").json()
    task = unconfirmed_harness(app, task)
    for _ in range(4):
        upload(client, task)
    original = scene_route(app.state.engine.vlm.structured, "exercise", ReviewFocus(subject="肩部活动"))
    count = 0

    async def respond(prompt, schema, images=None):
        nonlocal count
        if schema is VisualReview:
            return VisualReview(visibility="unusable", summary="活动部位被遮挡")
        if issubclass(schema, Answer):
            count += 1
            if count == 1:
                return Answer(reply="", memory=MemoryCall(tool="open"))
            return Answer(reply="做错了，重复十次", suggestion="重复十次")
        return await original(prompt, schema, images)

    app.state.engine.vlm.structured = respond
    response = ask(client, "刚才怎么样", task_id=task["id"])
    assert response.status_code == 200, response.text
    assert response.json()["suggestion"] is None
    assert "重复十次" not in response.json()["reply"]


async def test_provider_rejects_silent_image_truncation():
    p = CodexProxyProvider(Settings(max_images=2))
    await p.client.aclose()
    p.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("must not send")))
    try:
        with pytest.raises(ProviderError, match="截断"):
            await p.text("test", [None] * 3)
    finally:
        await p.close()


def test_unconfirmed_audit_removes_unsupported_advice_and_records_correction(app, client):
    app.state.engine.vlm.research_decision = ResearchDecision(
        action="direct",
        reason="普通设备",
        scenario="installation",
        device_model="Demo device A1",
        model_evidence="user",
    )
    task = change(client, start(client), "confirm_plan").json()
    task = unconfirmed_harness(app, task)
    frame = upload(client, task)
    original = app.state.engine.vlm.structured

    async def respond(prompt, schema, images=None):
        if issubclass(schema, Answer):
            return Answer(reply="已经接牢，可以安全使用", suggestion="立刻通电", evidence_conflict=True)
        if schema is AnswerAudit:
            return AnswerAudit(
                action="revise",
                unsupported_claims=["外观不能认证连接安全"],
                corrected_reply="能看到接头相邻，但无法确认内部接触与安全状态。",
                evidence_ids=[frame["id"]],
            )
        return await original(prompt, schema, images)

    app.state.engine.vlm.structured = respond
    response = ask(client, "这个接头怎么样", task_id=task["id"], frame_id=frame["id"])
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["suggestion"] is None and result["checks"] == []
    assert "安全使用" not in result["reply"]
    assert result["retrieval"]["answer_audit"]["action"] == "revise"
    assert not app.state.store.rows("SELECT * FROM suggestions")


async def test_event_open_respects_explicit_subrange_and_before_time(app):
    from test_memory_agent import active_frames, event
    from along.memory_tools import MemoryTools

    task, frames = active_frames(app)
    event(app.state.store, task["id"], "window", frames)
    tools = MemoryTools(app.state.memory, task["id"])
    result, images = await tools.execute(
        MemoryCall(
            tool="open",
            event_id="window",
            start_time=frames[10]["captured"],
            end_time=frames[18]["captured"],
            before_time=frames[15]["captured"],
            frames=12,
        ),
        12,
    )
    assert len(images) == 1
    assert list(images[0].frame_ids) == [f["id"] for f in frames[10:15]]
    assert result["selected_gap_seconds"] == 1


def test_unconfirmed_route_receives_completed_anchor_and_prior_focus(app, client):
    task = change(client, start(client), "confirm_plan").json()
    task = unconfirmed_harness(app, task)
    task = change(client, task, "step", target="s1", value="user_reported_done").json()
    original = app.state.engine.vlm.structured

    async def respond(prompt, schema, images=None):
        if issubclass(schema, Route):
            context = json.loads(prompt.split("\n")[-1])
            assert context["anchors"]["last_user_completed_step"]["step_id"] == "s1"
            assert "previous_review_focus" in context["task"]
        return await original(prompt, schema, images)

    app.state.engine.vlm.structured = respond
    response = ask(client, "上一步呢", task_id=task["id"])
    assert response.status_code == 200


def test_stop_preparation_cannot_be_confirmed_and_never_searches(app, client):
    app.state.engine.vlm.research_decision = ResearchDecision(
        action="stop", scenario="installation", reason="请停止带电拆修并联系专业人员。"
    )
    task = client.post("/api/tasks", json={"goal": "带电拆修"}).json()
    response = client.post(f"/api/tasks/{task['id']}/prepare", json={})
    assert response.status_code == 200
    result = response.json()
    assert result["plan"] is None and not result["state"]["plan_confirmed"]
    assert result["state"]["error"] and not result["state"]["pending_questions"]
    assert not app.state.engine.web.searches
    assert change(client, result, "confirm_plan").status_code == 409


def test_stop_from_chat_does_not_claim_plan_is_ready(app, client):
    app.state.engine.vlm.research_decision = ResearchDecision(
        action="stop", reason="需要专业帮助", scenario="exercise"
    )
    response = ask(client, "我们一起诊断损伤并做术后康复")
    assert response.status_code == 200
    assert response.json()["kind"] == "plan_error"
    assert response.json()["reply"] == "需要专业帮助"


def test_revised_plan_is_validated_before_adoption(app, client):
    from along.schemas import PlanAudit, PlanDraft, Step

    app.state.engine.vlm.research_decision = ResearchDecision(
        action="direct",
        reason="设备使用",
        scenario="installation",
        device_model="Demo device A1",
        model_evidence="user",
    )
    original = app.state.engine.vlm.structured

    async def respond(prompt, schema, images=None):
        if schema is PlanAudit:
            return PlanAudit(
                action="revise",
                revised_plan=PlanDraft(
                    title="修正版",
                    steps=[
                        Step(id="s1", title="使用", instruction="参照标识", source_ids=["invented-source"])
                    ],
                ),
            )
        return await original(prompt, schema, images)

    app.state.engine.vlm.structured = respond
    task = client.post("/api/tasks", json={"goal": "连接音箱"}).json()
    response = client.post(f"/api/tasks/{task['id']}/prepare", json={})
    assert response.status_code == 502
    assert app.state.store.task(task["id"])["plan"] is None


def test_unconfirmed_in_task_stop_does_not_create_suggestion(app, client):
    task = change(client, start(client), "confirm_plan").json()
    task = unconfirmed_harness(app, task)
    original = app.state.engine.vlm.structured

    async def respond(prompt, schema, images=None):
        if issubclass(schema, Answer):
            return Answer(reply="", research=ResearchDecision(action="stop", reason="停止危险操作"))
        return await original(prompt, schema, images)

    app.state.engine.vlm.structured = respond
    response = ask(client, "我打算带电拆开", task_id=task["id"])
    assert response.status_code == 200
    assert response.json()["reply"] == "停止危险操作"
    assert response.json()["suggestion"] is None
    assert not app.state.store.rows("SELECT * FROM suggestions")


def test_single_question_and_single_tool_are_schema_contracts():
    from pydantic import ValidationError
    from along.schemas import PlanDraft, Step

    with pytest.raises(ValidationError):
        ResearchDecision(action="clarify", reason="缺少条件", questions=["型号？", "尺寸？"])
    with pytest.raises(ValidationError):
        PlanDraft(
            title="计划",
            steps=[Step(id="s1", title="准备", instruction="看看")],
            questions=["型号？", "尺寸？"],
        )
    with pytest.raises(ValidationError):
        Answer(
            reply="",
            memory=MemoryCall(tool="open"),
            research=ResearchDecision(action="direct", reason="无需搜索"),
        )


async def test_audit_cannot_cite_an_unseen_image():
    from along.observation import audit_answer

    class Provider:
        async def structured(self, *args):
            return AnswerAudit(action="revise", corrected_reply="修改后的回答", evidence_ids=["foreign"])

    with pytest.raises(ProviderError):
        await audit_answer(Provider(), "installation", {}, Answer(reply="已装好"), [], [])


def test_followup_rejects_old_window_and_agent_selects_new_range(app, client):
    from test_memory_agent import active_frames

    task, frames = active_frames(app, count=20)
    boundary = frames[9]["captured"]
    app.state.store.message("conversation1", task["id"], "assistant", "上次只讨论连接方向")
    with app.state.store.tx() as db:
        db.execute("UPDATE messages SET created=? WHERE task_id=?", (boundary, task["id"]))
    focus = ReviewFocus(subject="连接方向", temporal_scope="after_previous_answer")
    original = scene_route(app.state.engine.vlm.structured, "installation", focus)
    calls = 0

    async def respond(prompt, schema, images=None):
        nonlocal calls
        if issubclass(schema, Answer):
            calls += 1
            context = json.loads(prompt.split("\n")[-1])
            if calls == 1:
                return Answer(
                    reply="",
                    memory=MemoryCall(tool="open", start_time=frames[0]["captured"], end_time=boundary),
                )
            if calls == 2:
                assert context["memory_tool_results"][0]["result"]["error"]
                assert not images
                return Answer(
                    reply="",
                    memory=MemoryCall(
                        tool="open", start_time=frames[10]["captured"], end_time=frames[-1]["captured"]
                    ),
                )
            assert all(im.id in {f["id"] for f in frames[10:]} for im in images)
            return Answer(reply="新一轮能看到方向变化", evidence_ids=[images[0].id])
        return await original(prompt, schema, images)

    app.state.engine.vlm.structured = respond
    response = ask(client, "这次呢", task_id=task["id"])
    assert response.status_code == 200, response.text
    tools = response.json()["retrieval"]["tools"]
    assert len(tools) == 2 and "frames" not in tools[0]["result"]
    assert response.json()["review_focus"]["temporal_scope"] == "after_previous_answer"


def test_failed_clip_cannot_be_treated_as_reviewed_evidence(app, client):
    from test_memory_agent import active_frames

    task, _ = active_frames(app, count=3)
    original = scene_route(app.state.engine.vlm.structured, "exercise", ReviewFocus(subject="肩部运动"))
    calls = 0

    async def respond(prompt, schema, images=None):
        nonlocal calls
        if issubclass(schema, Answer):
            calls += 1
            if calls == 1:
                return Answer(reply="", memory=MemoryCall(tool="open", event_id="nonexistent"))
            return Answer(reply="做得完全正确", suggestion="继续增加强度")
        return await original(prompt, schema, images)

    app.state.engine.vlm.structured = respond
    response = ask(client, "刚才过程呢", task_id=task["id"])
    assert response.status_code == 200
    assert "完全正确" not in response.json()["reply"]
    assert response.json()["suggestion"] is None


def test_recent_can_trace_back_after_inspecting_latest_window(app, client):
    from test_memory_agent import active_frames

    task, frames = active_frames(app, count=80)
    original = scene_route(
        app.state.engine.vlm.structured, "exercise", ReviewFocus(subject="肩部活动", temporal_scope="recent")
    )
    calls = 0

    async def respond(prompt, schema, images=None):
        nonlocal calls
        if issubclass(schema, Answer):
            calls += 1
            if calls == 1:
                return Answer(
                    reply="",
                    memory=MemoryCall(
                        tool="open", start_time=frames[0]["captured"], end_time=frames[8]["captured"]
                    ),
                )
            if calls == 2:
                assert not images
                return Answer(
                    reply="",
                    memory=MemoryCall(
                        tool="open", start_time=frames[65]["captured"], end_time=frames[79]["captured"]
                    ),
                )
            if calls == 3:
                return Answer(
                    reply="",
                    memory=MemoryCall(
                        tool="open",
                        start_time=frames[0]["captured"],
                        end_time=frames[8]["captured"],
                        allow_older=True,
                        reason="最近窗口已看过，只有等待，向前寻找最近实际操作。",
                        replace_images=True,
                    ),
                )
            assert {im.id for im in images} <= {f["id"] for f in frames[:9]}
            return Answer(reply="回查更早的实际活动记录", evidence_ids=[images[0].id])
        return await original(prompt, schema, images)

    app.state.engine.vlm.structured = respond
    response = ask(client, "最近做得怎么样", task_id=task["id"])
    assert response.status_code == 200, response.text
    tools = response.json()["retrieval"]["tools"]
    assert tools[0]["result"]["error"] and len(tools[-1]["result"]["frames"]) >= 2


def test_older_override_requires_reason():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        MemoryCall(tool="open", allow_older=True)
    with pytest.raises(ValidationError):
        MemoryCall(tool="current", allow_older=True, reason="test")


def test_unconfirmed_process_planning_uses_required_tool_schema_even_with_current_snapshot(app, client):
    from along.schemas import MemoryRequest

    task = change(client, start(client), "confirm_plan").json()
    task = unconfirmed_harness(app, task)
    frames = [upload(client, task) for _ in range(4)]
    original = app.state.engine.vlm.structured
    calls = 0

    async def respond(prompt, schema, images=None):
        nonlocal calls
        if issubclass(schema, Answer):
            calls += 1
            if calls == 1:
                assert schema is MemoryRequest and not images
                return MemoryRequest(memory=MemoryCall(tool="open"))
            assert schema is Answer and len(images) >= 2
            return Answer(reply="查看后再描述变化")
        return await original(prompt, schema, images)

    app.state.engine.vlm.structured = respond
    response = ask(client, "刚才做得怎么样", task_id=task["id"], frame_id=frames[-1]["id"])
    assert response.status_code == 200


def test_sequence_request_cannot_substitute_single_current_frame():
    from pydantic import ValidationError
    from along.schemas import MemoryRequest

    with pytest.raises(ValidationError):
        MemoryRequest(memory=MemoryCall(tool="current"))
