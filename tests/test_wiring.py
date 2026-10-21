import pytest

from along.engine import enforce_wiring_verdict
from along.providers.vlm import ProviderError
from along.schemas import Answer, ConnectionCheck, PlanDraft, ResearchDecision, Route, Step
from test_flows import ask, start, upload


def context(scope="unspecified"):
    return {
        "adopted_plan": {"connections": [{"id": "c1", "step_id": "s1"}, {"id": "c2", "step_id": "s2"}]},
        "review_focus": {"temporal_scope": scope},
        "review_criteria": {"id": "s1"},
    }


@pytest.mark.parametrize(
    "statuses,verdict",
    [
        (["met", "met"], "ok"),
        (["met", "unseen"], "unclear"),
        (["met", "not_met"], "adjust"),
        (["unseen", "unseen"], "unclear"),
    ],
)
def test_every_target_connection_needs_evidence(statuses, verdict):
    answer = Answer(
        reply="接好了",
        verdict="ok",
        connection_checks=[
            ConnectionCheck(connection_id=f"c{i + 1}", status=status, frame_ids=["f1"])
            for i, status in enumerate(statuses)
        ],
    )
    enforce_wiring_verdict(answer, context(), {"f1"})
    assert answer.verdict == verdict


def test_current_step_does_not_require_future_connections():
    answer = Answer(
        reply="这一步对了",
        verdict="ok",
        connection_checks=[ConnectionCheck(connection_id="c1", status="met", frame_ids=["f1"])],
    )
    enforce_wiring_verdict(answer, context("current_step"), {"f1"})
    assert answer.verdict == "ok"


@pytest.mark.parametrize("checks", [[], [ConnectionCheck(connection_id="c1", status="met")]])
def test_missing_checks_or_frames_cannot_pass(checks):
    answer = Answer(reply="正确", verdict="ok", connection_checks=checks)
    enforce_wiring_verdict(answer, context(), {"f1"})
    assert answer.verdict == "unclear" and "证据不足" in answer.reply


@pytest.mark.parametrize(
    "checks",
    [
        [ConnectionCheck(connection_id="foreign", status="met", frame_ids=["f1"])],
        [ConnectionCheck(connection_id="c1", status="met", frame_ids=["foreign"])],
        [
            ConnectionCheck(connection_id="c1", status="met"),
            ConnectionCheck(connection_id="c1", status="met"),
        ],
    ],
)
def test_invalid_connection_evidence_rejected(checks):
    with pytest.raises(ProviderError):
        enforce_wiring_verdict(Answer(reply="", connection_checks=checks), context(), {"f1"})


def test_wiring_route_and_reference_image_reach_planning(app, client):
    task = start(client)
    frame = upload(client)
    with app.state.store.tx() as db:
        db.execute("UPDATE media SET task_id=?,origin='reference' WHERE id=?", (task["id"], frame["id"]))
    provider = app.state.engine.vlm
    original = provider.structured
    seen = []

    async def respond(prompt, schema, images=None):
        if schema is ResearchDecision:
            seen.append(images)
            return ResearchDecision(action="direct", reason="已有明确资料", scenario="wiring")
        if schema is PlanDraft:
            seen.append(images)
            return PlanDraft(
                title="请先确认版本",
                steps=[Step(id="s1", title="准备", instruction="保持断电")],
                questions=["开发板和模块的具体版本是什么？"],
            )
        return await original(prompt, schema, images)

    provider.structured = respond
    response = client.post(f"/api/tasks/{task['id']}/prepare", json={"frame_id": frame["id"]})
    assert response.status_code == 200, response.text
    assert response.json()["state"]["scenario"] == "wiring"
    assert all(images[0].id == frame["id"] for images in seen)
    # Re-preparing retains the known picture instead of asking for it again.
    response = client.post(f"/api/tasks/{task['id']}/prepare", json={})
    assert response.status_code == 200 and seen[-1][0].id == frame["id"]


def test_start_picture_attached_to_new_task(app, client):
    frame = upload(client)
    provider = app.state.engine.vlm
    original = provider.structured

    async def respond(prompt, schema, images=None):
        if issubclass(schema, Route):
            assert images[0].id == frame["id"]
            return Route(intent="start", goal="接线", scenario="wiring")
        return await original(prompt, schema, images)

    provider.structured = respond
    response = ask(client, "一起接线", frame_id=frame["id"], mode="task")
    assert response.status_code == 200, response.text
    assert (
        app.state.store.one("SELECT task_id FROM media WHERE id=?", (frame["id"],))["task_id"]
        == response.json()["task_id"]
    )


def test_reference_images_are_not_execution_events(app, client):
    import io
    import time
    from PIL import Image
    from along.memory_tools import MemoryTools

    task = start(client)
    out = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(out, "JPEG")
    response = client.post(
        "/api/media",
        data={
            "media_id": "reference-test",
            "conversation_id": "conversation1",
            "task_id": task["id"],
            "captured": time.time(),
            "seq": 1,
            "origin": "reference",
        },
        files={"file": ("reference.jpg", out.getvalue(), "image/jpeg")},
    )
    assert response.status_code == 200, response.text
    app.state.memory.schedule()
    assert not app.state.store.rows("SELECT * FROM jobs WHERE task_id=?", (task["id"],))
    tools = MemoryTools(app.state.memory, task["id"])
    assert tools.overview()["frames"] == 0
    assert all(group["frame_count"] == 0 for group in tools.catalog(task).groups.values())


def test_wiring_retrieval_includes_full_resolution_current_image(app, client):
    from along.schemas import CompanionTurn, CompanionReply
    from test_memory_agent import active_frames

    task, frames = active_frames(app, count=12)

    def wiring(row, state, plan, db):
        state.update(scenario="wiring", plan_confirmed=True)
        plan = {
            "steps": [
                {"id": "s1", "title": "接线", "instruction": "核对端点", "look_for": [], "source_ids": []}
            ],
            "connections": [
                {"id": "c1", "step_id": "s1", "from_pin": "board 3V3", "to_pin": "sensor VCC"},
                {"id": "c2", "step_id": "s1", "from_pin": "board GND", "to_pin": "sensor GND"},
            ],
        }
        return row["status"], state, plan, "test_wiring", {}

    task = app.state.store.change(task["id"], task["version"], wiring)

    async def respond(prompt, schema, images=None):
        assert schema is CompanionTurn
        assert len(images) == 2 and not images[0].frame_ids and images[1].frame_ids
        assert images[0].id == frames[-1]["id"]
        return CompanionTurn(
            answer=CompanionReply(
                reply="目标端点匹配。",
                verdict="ok",
                connection_checks=[
                    ConnectionCheck(connection_id=c, status="met", frame_ids=[images[0].id])
                    for c in ("c1", "c2")
                ],
            )
        )

    app.state.engine.vlm.structured = respond
    response = ask(client, "我所有线接对了吗", task_id=task["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verdict"] == "ok"
    assert body["retrieval"]["question_frames"][0]["id"] == frames[-1]["id"]


def test_companion_missing_wiring_endpoint_asks_state_not_for_camera():
    ctx = {**context(), "companion": True}
    answer = Answer(
        reply="接好了",
        verdict="ok",
        connection_checks=[ConnectionCheck(connection_id="c1", status="met", frame_ids=["f1"])],
    )
    enforce_wiring_verdict(answer, ctx, {"f1"})
    assert answer.verdict == "none" and "引脚" in answer.reply
    assert "近景" not in answer.reply and "画面" not in answer.reply
    assert len(answer.blocking_missing) == 1
