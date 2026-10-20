import json

from PIL import Image

from along.engine import look_for
from along.memory_tools import MemoryTools
from along.observation import audit_answer
from along.providers.vlm import ImageInput
from along.schemas import Answer, AnswerAudit, CriterionResult, MemoryCall, ReviewFocus, Route
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


def test_current_step_question_opens_current_step_sheet_and_maps_evidence(app, client):
    task = change(client, start(client), "confirm_plan").json()
    for _ in range(12):
        upload(client, task)
    provider = app.state.engine.vlm
    original = provider.structured
    seen = {}

    async def respond(prompt, schema, images=None):
        if schema is Route:
            return Route(
                intent="ask",
                related_to_task=True,
                needs_history=True,
                review_focus=ReviewFocus(temporal_scope="current_step", subject="手腕摆动"),
            )
        if schema is AnswerAudit:
            seen["audit_prompt"] = prompt
            return AnswerAudit(action="keep")
        if issubclass(schema, Answer):
            context = json.loads(prompt.split("\n")[-1])
            if not context["memory_tool_results"]:
                seen["clip"] = context["current_step_clip"]
                clip = {k: v for k, v in context["current_step_clip"].items() if k != "note"}
                return Answer(reply="", memory=MemoryCall(**clip))
            sheet = images[0]
            return Answer(
                reply="手掌左右摆动到位。",
                headline="对的，左右摆幅度到位",
                verdict="ok",
                criteria=[CriterionResult(criterion="对照材料", status="met", frame_ids=[sheet.id])],
                evidence_ids=[sheet.id],
            )
        return await original(prompt, schema, images)

    provider.structured = respond
    result = ask(client, "我现在做的对吗", task_id=task["id"]).json()
    assert seen["clip"]["step_id"] == "s1" and seen["clip"]["sheet"] is True
    assert result["verdict"] == "ok" and result["headline"].startswith("对的")
    frames = result["retrieval"]["frames"]
    assert len(frames) >= 10 and not any(f.startswith("sheet_") for f in frames)
    assert result["evidence_ids"] and set(result["evidence_ids"]) <= set(frames)
    assert result["criteria"][0]["frame_ids"] == []


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
