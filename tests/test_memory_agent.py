import json
import time

import numpy as np
import pytest

from along.config import Settings
from along.memory_tools import MemoryTools
from along.schemas import Answer, MemoryCall
from along.store import Store, dump
from test_flows import ask, change, start, upload
from test_store import image_bytes


def active_frames(app, count=34):
    store = app.state.store
    task = store.create_task("日常活动手腕")
    task = store.change(task["id"], 1, lambda r, s, p, db: ("active", s, p, "start", {}))
    base = time.time() - count - 1
    frames = [
        app.state.memory.media.save(f"frame_{i}", image_bytes(), task["id"], "conversation1", base + i, i)
        for i in range(count)
    ]
    return task, frames


def event(store, task_id, eid, frames, summary="手腕活动", embedding=None):
    with store.tx() as db:
        db.execute(
            "INSERT INTO events(id,task_id,start,end,data,frame_ids,embedding,embedding_model) VALUES(?,?,?,?,?,?,?,?)",
            (
                eid,
                task_id,
                frames[0]["captured"],
                frames[-1]["captured"],
                dump({"summary": summary, "objects": [summary]}),
                dump([f["id"] for f in frames]),
                dump(embedding) if embedding else None,
                "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2" if embedding else None,
            ),
        )


def test_defaults_one_frame_per_second():
    settings = Settings(_env_file=None)
    assert settings.frame_interval == 1
    assert settings.clip_frames == 8 and settings.max_images == 12
    assert settings.memory_rounds == 3


async def test_unindexed_recent_clip_is_ordered_and_bounded(app):
    task, frames = active_frames(app)
    tools = MemoryTools(app.state.memory, task["id"])
    info, images = await tools.execute(MemoryCall(tool="open"), 12)
    assert 6 <= len(images) <= 8
    times = [f["captured"] for f in info["frames"]]
    assert times == sorted(times)
    assert images[-1].id == frames[-1]["id"]
    assert not app.state.store.rows("SELECT * FROM events")
    assert info["largest_recorded_gap_seconds"] == 1
    info, images = await tools.execute(MemoryCall(tool="open", seconds=120), 3)
    assert len(images) == 3
    assert images[0].id == frames[0]["id"] and images[-1].id == frames[-1]["id"]


async def test_browse_search_open_and_step_filter(app):
    task, frames = active_frames(app)
    store = app.state.store
    event(store, task["id"], "old", frames[:8], "面团冷藏")
    event(store, task["id"], "new", frames[20:], "手腕屈伸")
    with store.tx() as db:
        db.execute("UPDATE media SET plan_revision=1,step_id='s1' WHERE seq<8")
        db.execute("UPDATE media SET plan_revision=2,step_id='s2' WHERE seq>=20")
    tools = MemoryTools(app.state.memory, task["id"])
    result, images = await tools.execute(MemoryCall(tool="search", query="面团冷藏"), 12)
    assert not images and result["events"][0]["id"] == "old"
    result, _ = await tools.execute(MemoryCall(tool="search", query="面团冷藏", step_id="s2"), 12)
    assert not result["events"]
    result, _ = await tools.execute(MemoryCall(tool="browse", plan_revision=1, step_id="s1"), 12)
    assert [e["id"] for e in result["events"]] == ["old"]
    result, images = await tools.execute(MemoryCall(tool="open", event_id="old"), 12)
    assert images and result["start"] < frames[0]["captured"]
    with store.tx() as db:
        db.execute("UPDATE events SET data=? WHERE id='old'", (dump({"summary": "饼干整形"}),))
    result, _ = await tools.execute(MemoryCall(tool="search", query="饼干"), 12)
    assert result["events"][0]["id"] == "old"
    store.delete_task(task["id"])
    assert not store.rows("SELECT * FROM event_search")


async def test_temporal_semantic_and_empty_results(app):
    task, frames = active_frames(app)
    store = app.state.store
    event(store, task["id"], "old", frames[:8], "冷藏", [1, 0])
    event(store, task["id"], "new", frames[20:], "屈伸", [0, 1])

    async def encode(texts):
        return [[1, 0]]

    app.state.memory.embeddings.encode = encode
    tools = MemoryTools(app.state.memory, task["id"])
    result, _ = await tools.execute(MemoryCall(tool="search", query="降低温度"), 12)
    assert result["events"][0]["id"] == "old"
    result, _ = await tools.execute(
        MemoryCall(tool="search", query="降低温度", start_time=frames[20]["captured"]), 12
    )
    assert not result["events"]
    result, images = await tools.execute(MemoryCall(tool="open", start_time=1, end_time=10), 12)
    assert not images and result["error"]
    result, images = await tools.execute(MemoryCall(tool="open", start_time=1, end_time=1000), 12)
    assert not images and "120" in result["error"]


async def test_scope_expiry_and_current_age(app):
    task, frames = active_frames(app)
    event(app.state.store, task["id"], "secret", frames[:8])
    other = MemoryTools(app.state.memory, "different_task")
    for call in [
        MemoryCall(tool="browse"),
        MemoryCall(tool="search", query="手腕"),
        MemoryCall(tool="open", event_id="secret"),
        MemoryCall(tool="current"),
    ]:
        result, images = await other.execute(call, 12)
        assert not images and not result.get("events")
    tools = MemoryTools(app.state.memory, task["id"])
    result, images = await tools.execute(MemoryCall(tool="current"), 12)
    assert len(images) == 1 and result["last_frame_age_seconds"] >= 1
    _, path = app.state.memory.media.path(frames[-1]["id"])
    path.unlink()
    result, images = await tools.execute(MemoryCall(tool="current"), 12)
    assert not images and result["missing_files"] == 1
    result, images = await tools.execute(MemoryCall(tool="open"), 0)
    assert not images and "预算" in result["error"]


def test_capture_metadata_tracks_capture_time_not_upload_time(app, client):
    task = change(client, start(client), "confirm_plan").json()
    first = upload(client, task)
    assert first["step_id"] == "s1" and first["plan_revision"] == 1
    captured = time.time()
    task = change(client, task, "step", target="s1", value="user_reported_done").json()
    task = change(client, task, "pause").json()
    task = client.post(f"/api/tasks/{task['id']}/prepare", json={}).json()
    assert task["state"]["plan_revision"] == 2
    delayed = app.state.memory.media.save("late", image_bytes(), task["id"], "conversation1", captured, 8888)
    assert delayed["step_id"] == "s1" and delayed["plan_revision"] == 1


def test_migration_backfills_metadata_and_search(app, client, tmp_path):
    task = change(client, start(client), "confirm_plan").json()
    frame = upload(client, task)
    store = app.state.store
    event(store, task["id"], "original", [frame])
    import sqlite3

    copy_dir = tmp_path / "migration"
    copy_dir.mkdir()
    copy = sqlite3.connect(copy_dir / "along.sqlite3")
    store.conn.backup(copy)
    copy.executescript(
        "DROP INDEX media_step; ALTER TABLE media DROP COLUMN plan_revision; "
        "ALTER TABLE media DROP COLUMN step_id; DROP TRIGGER event_search_insert; "
        "DROP TRIGGER event_search_update; DROP TRIGGER event_search_delete; "
        "DROP TABLE event_search; PRAGMA user_version=2;"
    )
    copy.close()
    restored = Store(copy_dir)
    try:
        migrated = restored.one("SELECT * FROM media WHERE id=?", (frame["id"],))
        assert migrated["step_id"] == "s1" and migrated["plan_revision"] == 1
        assert restored.one("SELECT COUNT(*) AS n FROM event_search")["n"] == 1
        assert restored.one("PRAGMA user_version")["user_version"] == 3
    finally:
        restored.close()


def test_agent_reads_sequence_despite_route_without_history(app, client):
    task = change(client, start(client), "confirm_plan").json()
    frames = [upload(client, task) for _ in range(9)]
    provider = app.state.engine.vlm
    original = provider.structured
    contexts, viewed = [], []

    async def respond(prompt, schema, images=None):
        if not issubclass(schema, Answer):
            return await original(prompt, schema, images)
        context = json.loads(prompt.split("\n")[-1])
        contexts.append(context)
        viewed.append(list(images or []))
        if not context["memory_tool_results"]:
            return Answer(reply="", memory=MemoryCall(tool="open"))
        return Answer(
            reply="已回看片段中的变化，不能据此确认精确次数。",
            evidence_ids=[im.id for im in images],
            missing=["稀疏图片不能确认完整次数"],
        )

    provider.structured = respond
    result = ask(client, "我现在做的过程对吗", task_id=task["id"], frame_id=frames[-1]["id"]).json()
    assert len(viewed[0]) == 1 and len(viewed[-1]) >= 6
    assert len(result["retrieval"]["tools"]) == 1
    assert contexts[0]["task_state"]["current_ui_step_not_observed_action"]["id"] == "s1"
    assert not app.state.store.task(task["id"])["state"]["open_issues"]


def test_agent_three_round_limit_and_duplicate_detection(app, client):
    task = change(client, start(client), "confirm_plan").json()
    upload(client, task)
    original = app.state.engine.vlm.structured

    async def respond(prompt, schema, images=None):
        if issubclass(schema, Answer):
            return Answer(reply="", memory=MemoryCall(tool="open"))
        return await original(prompt, schema, images)

    app.state.engine.vlm.structured = respond
    result = ask(client, "过程如何", task_id=task["id"]).json()
    assert len(result["retrieval"]["tools"]) == 3
    assert "已执行同一查询" in result["retrieval"]["tools"][1]["result"]["error"]
    assert result["warnings"] and not result["evidence_ids"]


def test_instant_cannot_read_task_images(app, client):
    task = change(client, start(client), "confirm_plan").json()
    frame = upload(client, task)
    assert ask(client, "是什么", mode="instant", frame_id=frame["id"]).status_code == 422


async def test_index_windows_do_not_cross_ui_revision(app):
    task, frames = active_frames(app, 60)
    store = app.state.store
    with store.tx() as db:
        db.execute("UPDATE media SET plan_revision=1,step_id='s1' WHERE seq<10")
        db.execute("UPDATE media SET plan_revision=2,step_id='s2' WHERE seq>=10")
    app.state.memory.schedule()
    app.state.memory.schedule()
    jobs = store.rows("SELECT * FROM jobs ORDER BY available")
    assert len(jobs) == 2
    assert set(json.loads(jobs[0]["frame_ids"])) == {f["id"] for f in frames[:10]}
    assert set(json.loads(jobs[1]["frame_ids"])) <= {f["id"] for f in frames[10:]}


async def test_semantic_sampling_bounded_across_long_task(app):
    task, frames = active_frames(app, 100)
    memory, store = app.state.memory, app.state.store
    memory.settings.memory_candidates = 40
    for i, frame in enumerate(frames):
        event(store, task["id"], f"e{i}", [frame], "早期独特线索" if i == 1 else "一般事件", [1, 0])

    async def encode(texts):
        return [[1, 0]]

    memory.embeddings.encode = encode
    result, _ = await MemoryTools(memory, task["id"]).execute(MemoryCall(tool="search", query="独特线索"), 12)
    assert "e1" in {e["id"] for e in result["events"]}
    assert result["warnings"]
    assert len(result["events"]) <= 5


@pytest.mark.parametrize(
    "values",
    [
        {"tool": "search"},
        {"tool": "open", "start_time": np.inf},
        {"tool": "open", "start_time": 2, "end_time": 1},
    ],
)
def test_tool_argument_validation(values):
    with pytest.raises(ValueError):
        MemoryCall(**values)


def test_agent_search_then_open_rechecks_raw_images(app, client):
    task = change(client, start(client), "confirm_plan").json()
    frames = [upload(client, task) for _ in range(10)]
    event(app.state.store, task["id"], "target", frames, "手腕侧偏")
    original = app.state.engine.vlm.structured

    async def respond(prompt, schema, images=None):
        if not issubclass(schema, Answer):
            return await original(prompt, schema, images)
        context = json.loads(prompt.split("\n")[-1])
        trace = context["memory_tool_results"]
        if not trace:
            return Answer(reply="", memory=MemoryCall(tool="search", query="手腕侧偏"))
        if len(trace) == 1:
            assert not images
            assert trace[0]["result"]["events"][0]["id"] == "target"
            return Answer(reply="", memory=MemoryCall(tool="open", event_id="target"))
        assert len(images) == 8
        return Answer(reply="只说明可见的动作变化。", evidence_ids=[images[0].id])

    app.state.engine.vlm.structured = respond
    result = ask(client, "之前侧偏那段怎么样", task_id=task["id"]).json()
    assert result["retrieval"]["events"] == ["target"]
    assert len(result["retrieval"]["tools"]) == 2
    assert not result["missing"]


def test_memory_and_web_budgets_are_independent(app, client):
    from along.schemas import ResearchDecision

    task = change(client, start(client), "confirm_plan").json()
    upload(client, task)
    original = app.state.engine.vlm.structured
    responses = 0

    async def respond(prompt, schema, images=None):
        nonlocal responses
        if not issubclass(schema, Answer):
            return await original(prompt, schema, images)
        responses += 1
        context = json.loads(prompt.split("\n")[-1])
        if responses == 1:
            return Answer(reply="", memory=MemoryCall(tool="current"))
        if responses == 2:
            return Answer(
                reply="",
                research=ResearchDecision(
                    action="search",
                    reason="型号参数需要核验",
                    query="对应型号 参数",
                    information_gap="额定参数",
                ),
            )
        assert context["research_available"] is False
        assert context["memory_available"] is True
        return Answer(reply="已根据适用资料给出说明。", source_ids=[context["reference_sources"][-1]["id"]])

    app.state.engine.vlm.structured = respond
    before = len(app.state.engine.web.searches)
    result = ask(client, "画面上这个型号参数是多少", task_id=task["id"]).json()
    assert len(app.state.engine.web.searches) == before + 1
    assert len(result["retrieval"]["tools"]) == 1
    assert result["source_ids"]


def test_concurrent_task_change_rejects_memory_answer(app, client):
    task = change(client, start(client), "confirm_plan").json()
    upload(client, task)
    original = app.state.engine.vlm.structured
    responses = 0

    async def respond(prompt, schema, images=None):
        nonlocal responses
        if not issubclass(schema, Answer):
            return await original(prompt, schema, images)
        responses += 1
        if responses == 1:
            return Answer(reply="", memory=MemoryCall(tool="open"))
        app.state.store.change(task["id"], task["version"], lambda r, s, p, db: ("paused", s, p, "pause", {}))
        return Answer(reply="不能展示过期判断")

    app.state.engine.vlm.structured = respond
    response = ask(client, "过程如何", task_id=task["id"])
    assert response.status_code == 409
    assert not app.state.store.rows("SELECT * FROM messages WHERE role='assistant'")


def test_agent_timeout_releases_lock_and_preserves_request_retry(app, client):
    import asyncio

    original = app.state.engine.vlm.structured

    async def slow(*args, **kwargs):
        await asyncio.sleep(1)
        return await original(*args, **kwargs)

    app.state.engine.vlm.structured = slow
    app.state.memory.settings.agent_timeout = 0.01
    response = ask(client, "看看", mode="instant")
    assert response.status_code == 502 and "超时" in response.json()["detail"]
    assert not app.state.engine.interaction.locked() and not app.state.memory.user_busy
    assert app.state.store.one("SELECT status FROM requests")["status"] == "failed"
    assert not app.state.store.rows("SELECT * FROM messages")


async def test_future_frames_and_unknown_old_step_are_not_relabelled(app):
    task, frames = active_frames(app)
    tools = MemoryTools(app.state.memory, task["id"], cutoff=frames[10]["captured"])
    result, images = await tools.execute(MemoryCall(tool="open"), 12)
    assert images[-1].id == frames[10]["id"]
    assert all(frame["step_id"] is None for frame in result["frames"])
    result, images = await tools.execute(MemoryCall(tool="open", step_id="s1"), 12)
    assert not images and result["error"]


def test_process_answer_must_not_confuse_unread_memory_with_missing_evidence(app, client):
    task = change(client, start(client), "confirm_plan").json()
    frames = [upload(client, task) for _ in range(8)]
    original = app.state.engine.vlm.structured
    responses = 0

    async def respond(prompt, schema, images=None):
        nonlocal responses
        if not issubclass(schema, Answer):
            return await original(prompt, schema, images)
        responses += 1
        context = json.loads(prompt.split("\n")[-1])
        if responses == 1:
            assert context["sequence_evidence_required"]
            return Answer(reply="单帧不能判断过程，请再说明。")
        if responses == 2:
            assert context["evidence_review_feedback"]
            return Answer(reply="", memory=MemoryCall(tool="open"))
        return Answer(reply="根据回看的片段说明可见变化。", evidence_ids=[im.id for im in images])

    app.state.engine.vlm.structured = respond
    response = ask(client, "刚才过程如何", task_id=task["id"], frame_id=frames[-1]["id"])
    assert response.status_code == 200, response.text
    assert responses == 3 and len(response.json()["retrieval"]["frames"]) == 8
    assert "单帧不能" not in response.json()["reply"]


def test_ignored_evidence_feedback_never_publishes_premature_evaluation(app, client):
    task = change(client, start(client), "confirm_plan").json()
    for _ in range(3):
        upload(client, task)
    original = app.state.engine.vlm.structured

    async def respond(prompt, schema, images=None):
        if issubclass(schema, Answer):
            return Answer(reply="动作完全正确，无需查看", suggestion="直接继续")
        return await original(prompt, schema, images)

    app.state.engine.vlm.structured = respond
    result = ask(client, "刚才做的过程对吗", task_id=task["id"]).json()
    assert "完全正确" not in result["reply"] and result["suggestion"] is None
    assert result["warnings"]
