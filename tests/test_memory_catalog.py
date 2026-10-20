import json
import time

from along.memory_tools import MemoryTools
from along.schemas import Answer, MemoryCall, Mutation
from along.store import dump
from test_flows import ask
from test_memory_agent import event
from test_store import image_bytes


def scene(app):
    store, engine = app.state.store, app.state.engine
    task = store.create_task("一起锻炼手腕")
    plan = {
        "steps": [
            {"id": "s1", "title": "手腕前后摆动", "instruction": "轻轻前后摆动"},
            {"id": "s2", "title": "手腕左右摆动", "instruction": "轻轻左右摆动"},
            {"id": "s3", "title": "结束", "instruction": "放下双手"},
        ]
    }
    base = time.time() - 60

    def ready(row, state, old_plan, db):
        state.update(plan_revision=1, steps={s["id"]: "unknown" for s in plan["steps"]})
        return "active", state, plan, "confirm_plan", {}

    task = store.change(task["id"], task["version"], ready)
    with store.tx() as db:
        db.execute("UPDATE checkpoints SET created=? WHERE task_id=?", (base - 1, task["id"]))
    first = [
        app.state.memory.media.save(f"first_{i}", image_bytes(), task["id"], "conversation1", base + i, i)
        for i in range(18)
    ]
    task = engine.mutate(
        task["id"], Mutation(version=task["version"], action="step", target="s1", value="user_reported_done")
    )
    with store.tx() as db:
        db.execute(
            "UPDATE checkpoints SET created=? WHERE task_id=? AND version=?",
            (base + 18, task["id"], task["version"]),
        )
    typing = [
        app.state.memory.media.save(
            f"typing_{i}", image_bytes(), task["id"], "conversation1", base + 20 + i, 100 + i
        )
        for i in range(20)
    ]
    return task, first, typing


def test_directory_contains_every_step_and_unindexed_capture(app):
    task, first, typing = scene(app)
    catalog = MemoryTools(app.state.memory, task["id"]).catalog(task).payload()
    assert catalog["mode"] == "full"
    assert [step["step_id"] for step in catalog["steps"]] == ["s1", "s2", "s3"]
    assert catalog["current_ui_step"]["step_id"] == "s2"
    assert catalog["last_user_completed_step"]["step_id"] == "s1"
    assert catalog["last_user_completed_step"]["user_marked_done_at"] < typing[0]["captured"]
    assert catalog["previous_plan_step"]["step_id"] == "s1"
    s1, s2, s3 = catalog["steps"]
    assert s1["frame_count"] == 18 and s2["frame_count"] == 20 and s3["frame_count"] == 0
    assert s1["start"] == first[0]["captured"] and s1["end"] == first[-1]["captured"]
    assert s1["events"][0]["status"] == "pending_summary" and s3["events"] == []


async def test_indexed_events_and_late_frames_both_appear(app):
    task, first, typing = scene(app)
    event(app.state.store, task["id"], "first_event", first, "双手抬起，随后放下")
    event(app.state.store, task["id"], "partial_second", typing[:5], "手部位于画面下缘")
    tools = MemoryTools(app.state.memory, task["id"])
    directory = tools.catalog(task).payload()
    s1, s2, _ = directory["steps"]
    assert [e["id"] for e in s1["events"]] == ["first_event"]
    assert s1["events"][0]["summary_not_fact"] == "双手抬起，随后放下"
    assert [e["status"] for e in s2["events"]] == ["indexed", "pending_summary"]
    result, _ = await tools.execute(MemoryCall(tool="browse", step_id="s2", plan_revision=1), 12)
    assert result["unindexed_windows"] and result["events"][0]["id"] == "partial_second"
    assert result["unindexed_windows"][0]["start"] == typing[5]["captured"]


async def test_open_step_covers_entire_step_not_last_eight_seconds(app):
    task, first, _ = scene(app)
    tools = MemoryTools(app.state.memory, task["id"])
    result, images = await tools.execute(MemoryCall(tool="open", step_id="s1", plan_revision=1), 12)
    assert len(images) == 8
    assert images[0].id == first[0]["id"] and images[-1].id == first[-1]["id"]
    assert all(frame["step_id"] == "s1" for frame in result["frames"])
    result, images = await tools.execute(MemoryCall(tool="open", step_id="s1"), 12)
    assert not images and "plan_revision" in result["error"]


async def test_event_neighbors_do_not_cross_step_boundary(app):
    task, first, typing = scene(app)
    event(app.state.store, task["id"], "first_event", first)
    # Put a next-step frame within the two-second neighbor extension.
    with app.state.store.tx() as db:
        db.execute("UPDATE media SET captured=? WHERE id=?", (first[-1]["captured"] + 1, typing[0]["id"]))
    result, images = await MemoryTools(app.state.memory, task["id"]).execute(
        MemoryCall(tool="open", event_id="first_event"), 12
    )
    assert images and result["event_id"] == "first_event"
    assert all(frame["step_id"] == "s1" for frame in result["frames"])


def test_failed_index_expired_frames_and_unknown_step_are_not_hidden(app):
    task, first, typing = scene(app)
    with app.state.store.tx() as db:
        db.execute(
            "INSERT INTO jobs(id,task_id,frame_ids,status,available) VALUES(?,?,?,'failed',?)",
            ("bad_job", task["id"], dump([first[0]["id"]]), time.time()),
        )
        db.execute("UPDATE media SET indexed=1 WHERE id=?", (first[0]["id"],))
        db.execute("UPDATE media SET expired=1 WHERE id=?", (first[1]["id"],))
        db.execute("UPDATE media SET plan_revision=NULL,step_id=NULL WHERE id=?", (typing[-1]["id"],))
    directory = MemoryTools(app.state.memory, task["id"]).catalog(task).payload()
    s1 = directory["steps"][0]
    assert s1["available_frame_count"] == 17
    assert any(e["status"] == "index_failed" for e in s1["events"])
    unknown = next(s for s in directory["steps"] if s["step_id"] is None)
    assert unknown["frame_count"] == 1 and unknown["user_state"] == "unknown"


def test_revision_history_preserves_same_named_steps(app):
    task, first, _ = scene(app)
    event(app.state.store, task["id"], "old", first)

    def revise(row, state, plan, db):
        state.update(plan_revision=2, steps={"s1": "unknown"})
        return (
            "paused",
            state,
            {"steps": [{"id": "s1", "title": "新方案第一步", "instruction": "新要求"}]},
            "plan_proposed",
            {},
        )

    task = app.state.store.change(task["id"], task["version"], revise)
    directory = MemoryTools(app.state.memory, task["id"]).catalog(task).payload()
    assert directory["current_ui_step"]["title"] == "新方案第一步"
    assert directory["last_user_completed_step"] is None
    assert directory["steps"][0]["plan_revision"] == 2
    old = next(s for s in directory["steps"] if s["plan_revision"] == 1 and s["step_id"] == "s1")
    assert old["title"] == "手腕前后摆动" and old["events"][0]["id"] == "old"


def test_last_completed_is_not_always_previous_in_plan(app):
    task, _, _ = scene(app)
    task = app.state.engine.mutate(
        task["id"], Mutation(version=task["version"], action="step", target="s3", value="user_reported_done")
    )
    directory = MemoryTools(app.state.memory, task["id"]).catalog(task).payload()
    assert directory["current_ui_step"]["step_id"] == "s2"
    assert directory["last_user_completed_step"]["step_id"] == "s3"
    assert directory["previous_plan_step"]["step_id"] == "s1"
    task = app.state.engine.mutate(
        task["id"], Mutation(version=task["version"], action="step", target="s3", value="needs_recheck")
    )
    directory = MemoryTools(app.state.memory, task["id"]).catalog(task).payload()
    assert directory["last_user_completed_step"]["step_id"] == "s1"


async def test_compressed_directory_preserves_every_step_and_page(app):
    task, first, typing = scene(app)
    for i in range(80):
        event(app.state.store, task["id"], f"old_{i:03}", first, "旧事件" * 50)
    for i in range(5):
        event(app.state.store, task["id"], f"new_{i}", typing, "新事件")
    app.state.memory.settings.memory_catalog_chars = 4000
    tools = MemoryTools(app.state.memory, task["id"])
    directory = tools.catalog(task).payload()
    assert directory["mode"] == "step_rollups"
    assert len(directory["steps"]) == 3
    assert directory["steps"][0]["event_count"] == 80
    assert len(dump(directory)) < 4000
    call = MemoryCall(**directory["steps"][0]["browse"])
    found = []
    while True:
        result, images = await tools.execute(call, 12)
        assert len(result["events"]) <= 5 and not images
        found.extend(e["id"] for e in result["events"])
        if not result["next_page"]:
            break
        call = MemoryCall(**result["next_page"])
    assert len(found) == len(set(found)) == 80


def test_directory_is_scoped_and_read_only(app):
    task, _, _ = scene(app)
    before = app.state.store.task(task["id"])
    tools = MemoryTools(app.state.memory, task["id"])
    tools.catalog(task).payload()
    assert app.state.store.task(task["id"]) == before
    app.state.engine.mutate(task["id"], Mutation(version=task["version"], action="finish"))
    other = app.state.store.create_task("新任务")
    directory = MemoryTools(app.state.memory, other["id"]).catalog(other).payload()
    assert directory["steps"] == [] and directory["last_user_completed_step"] is None


def test_previous_step_question_sees_directory_before_any_tool(app, client):
    task, first, typing = scene(app)
    event(app.state.store, task["id"], "correct_event", first, "手腕前后活动")
    original = app.state.engine.vlm.structured

    async def respond(prompt, schema, images=None):
        if schema is not Answer:
            return await original(prompt, schema, images)
        context = json.loads(prompt.split("\n")[-1])
        directory = context["task_event_directory"]
        if not context["memory_tool_results"]:
            assert len(images) == 1 and images[0].id == typing[-1]["id"]
            assert directory["current_ui_step"]["step_id"] == "s2"
            assert directory["last_user_completed_step"]["step_id"] == "s1"
            assert len(directory["steps"]) == 3
            return Answer(
                reply="", memory=MemoryCall(tool="open", event_id=directory["steps"][0]["events"][0]["id"])
            )
        actual = context["memory_tool_results"][0]["result"]["frames"]
        assert all(f["step_id"] == "s1" for f in actual)
        return Answer(reply="回看上一步前后摆动的记录，而非打字画面。", evidence_ids=[actual[0]["id"]])

    app.state.engine.vlm.structured = respond
    response = ask(client, "我上一步做的对吗？", mode="task", task_id=task["id"], frame_id=typing[-1]["id"])
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["retrieval"]["events"] == ["correct_event"]
    assert result["retrieval"]["directory"]["last_user_completed_step"]["step_id"] == "s1"


def test_instant_mode_has_no_task_directory(app, client):
    scene(app)
    original = app.state.engine.vlm.structured

    async def respond(prompt, schema, images=None):
        if schema is Answer:
            context = json.loads(prompt.split("\n")[-1])
            assert context["task_event_directory"] is None and context["adopted_plan"] is None
        return await original(prompt, schema, images)

    app.state.engine.vlm.structured = respond
    assert ask(client, "这是什么", mode="instant").status_code == 200


async def test_large_step_requires_explicit_segment_instead_of_recent_fallback(app):
    task, first, _ = scene(app)
    with app.state.store.tx() as db:
        db.execute("UPDATE media SET captured=captured-200 WHERE id=?", (first[0]["id"],))
    result, images = await MemoryTools(app.state.memory, task["id"]).execute(
        MemoryCall(tool="open", step_id="s1", plan_revision=1), 12
    )
    assert not images and "120" in result["error"]
    assert result["end"] - result["start"] > 120


def test_directory_does_not_include_future_images_or_future_event_windows(app):
    task, first, typing = scene(app)
    event(app.state.store, task["id"], "future_event", typing)
    directory = (
        MemoryTools(app.state.memory, task["id"], cutoff=typing[2]["captured"]).catalog(task).payload()
    )
    assert directory["indexed_event_count"] == 0
    assert directory["steps"][1]["frame_count"] == 3
    assert directory["steps"][1]["end"] == typing[2]["captured"]
    assert directory["steps"][0]["frame_count"] == len(first)


def test_overlapping_events_do_not_double_count_step_frames(app):
    task, first, _ = scene(app)
    event(app.state.store, task["id"], "one", first[:12])
    event(app.state.store, task["id"], "two", first[10:])
    s1 = MemoryTools(app.state.memory, task["id"]).catalog(task).payload()["steps"][0]
    assert s1["event_count"] == 2 and s1["frame_count"] == 18
    assert sum(e["frame_count"] for e in s1["events"]) == 20
    assert all(e["status"] == "indexed" for e in s1["events"])


def test_raw_windows_survive_directory_compression(app):
    task, first, _ = scene(app)
    app.state.memory.settings.memory_catalog_chars = 4000
    app.state.memory.settings.event_window = 1
    catalog = MemoryTools(app.state.memory, task["id"]).catalog(task)
    directory = catalog.payload()
    assert directory["mode"] == "step_rollups"
    assert directory["steps"][0]["pending_window_count"] > 1
    result = catalog.browse(MemoryCall(**directory["steps"][0]["browse"]), 5)
    assert result["unindexed_windows"] and result["next_page"]
    assert not result["events"]
