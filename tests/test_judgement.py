import json

from PIL import Image

from along.engine import look_for
from along.memory_tools import MemoryTools
from along.observation import audit_answer
from along.providers.vlm import ImageInput
from along.schemas import Answer, AnswerAudit, MemoryCall
from test_flows import ask, change, start, upload
from test_memory_agent import active_frames


async def test_sheet_packs_dense_frames_into_one_image(app):
    task, frames = active_frames(app, count=30)
    tools = MemoryTools(app.state.memory, task["id"])
    info, images = await tools.execute(MemoryCall(tool="open", seconds=15, frames=20, sheet=True), 1)
    assert len(images) == 1 and info["layout"] == "sheet"
    sheet = images[0]
    assert len(sheet.frame_ids) == len(info["frames"]) >= 15
    assert [f["tile"] for f in info["frames"]] == list(range(1, len(info["frames"]) + 1))
    # Consecutive 1 fps frames, not a thinned sample.
    assert info["selected_gap_seconds"] <= 1.01
    with Image.open(sheet.path) as composite:
        assert composite.width > 32 * 4
    assert sheet.frame_ids[-1] == frames[-1]["id"]


async def test_sheet_needs_an_image_slot_and_large_counts_need_sheet(app):
    task, _ = active_frames(app, count=10)
    tools = MemoryTools(app.state.memory, task["id"])
    info, images = await tools.execute(MemoryCall(tool="open", frames=8, sheet=True), 0)
    assert not images and info["error"]
    try:
        MemoryCall(tool="open", frames=20)
    except ValueError:
        pass
    else:
        raise AssertionError("more than 12 loose frames must require a sheet")


def test_look_for_falls_back_to_instruction():
    assert look_for(None) == []
    assert look_for({"instruction": "左右摆", "look_for": []}) == ["左右摆"]
    assert look_for({"instruction": "x", "look_for": ["手背朝上"]}) == ["手背朝上"]


def test_current_step_question_includes_sheet_and_maps_evidence(app, client):
    from along.schemas import CompanionTurn, CompanionReply

    task = change(client, start(client), "confirm_plan").json()
    for _ in range(12):
        upload(client, task)
    calls = []

    async def respond(prompt, schema, images=None):
        calls.append(schema.__name__)
        assert schema is CompanionTurn
        context = json.loads(prompt.split("\n")[-1])
        assert context["current_step"]["id"] == "s1"
        assert len(images) == 2 and not images[0].frame_ids and images[1].frame_ids
        return CompanionTurn(
            answer=CompanionReply(
                reply="可见的左右摆动方向正确。",
                headline="对的，摆动方向正确",
                verdict="ok",
                evidence_ids=[images[1].id],
            )
        )

    app.state.engine.vlm.structured = respond
    response = ask(client, "我现在做的对吗", task_id=task["id"])
    assert response.status_code == 200, response.text
    result = response.json()
    assert calls == ["CompanionTurn"] and result["verdict"] == "ok"
    frames = result["retrieval"]["frames"]
    assert len(frames) >= 10 and not any(f.startswith("sheet_") for f in frames)
    assert set(result["evidence_ids"]) <= set(frames)
    tool = result["retrieval"]["tools"][0]["result"]
    assert tool["layout"] == "sheet"
    sheet = client.get(f"/api/sheets/{tool['sheet_id']}")
    assert sheet.status_code == 200 and sheet.headers["content-type"] == "image/jpeg"
    assert client.get("/api/sheets/..%2Faccess-token").status_code in (404, 422)
    assert len(result["retrieval"]["question_frames"]) == 1


async def test_audit_correction_keeps_visual_verdict_and_adjustment(app):
    calls = []

    class Judge:
        async def structured(self, prompt, schema, images=None):
            calls.append(prompt)
            return AnswerAudit(
                action="revise",
                corrected_reply="前臂跟着抬起了。",
                corrected_headline="差一点：前臂跟着动了",
                corrected_verdict="adjust",
                corrected_suggestion="前臂贴住桌面，只动手掌",
                evidence_ids=["f2"],
            )

    image = ImageInput("sheet_x", app.state.store.directory / "none.jpg", "", ("f1", "f2"))
    audit = await audit_answer(
        Judge(), "exercise", {"subject": "上下摆"}, Answer(reply="做得很好"), [image], [], ["前臂不动"]
    )
    assert audit.corrected_verdict == "adjust" and audit.corrected_suggestion
    assert "前臂不动" in calls[0] and "not_met" in calls[0]


def test_tile_numbers_resolve_to_frames():
    from along.observation import resolve_tiles

    ids = ("a", "b", "c")
    assert resolve_tiles(["1", "第3格", "b", "#2"], ids) == ["a", "c", "b", "b"]
    assert resolve_tiles(["9", "x"], ids) == ["9", "x"]
