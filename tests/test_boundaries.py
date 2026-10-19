import io
import json
import time

import pytest
from PIL import Image

from along.config import Settings
from along.media import MediaStore
from along.providers.vlm import ProviderError
from along.schemas import ChatInput, Mutation
from along.store import Conflict, Store, dump


def jpeg():
    out = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(out, "JPEG")
    return out.getvalue()


async def ready(app):
    task = app.state.store.create_task("组装测试")
    task = await app.state.engine.prepare(task["id"])
    return app.state.engine.mutate(task["id"], Mutation(version=task["version"], action="confirm_plan"))


async def test_delayed_upload_accepts_only_active_interval(app):
    task = await ready(app)
    captured = time.time()
    task = app.state.engine.mutate(task["id"], Mutation(version=task["version"], action="pause"))
    media = app.state.memory.media
    media.save("late_frame", jpeg(), task["id"], "conversation1", captured, 1)
    with pytest.raises(Conflict):
        media.save("paused_frame", jpeg(), task["id"], "conversation1", time.time(), 2)
    app.state.store.close()


async def test_stale_answer_cannot_override_changed_task(app):
    task = await ready(app)
    provider = app.state.engine.vlm
    original = provider.structured

    async def changing(prompt, schema, images=None):
        result = await original(prompt, schema, images)
        if schema.__name__ == "Answer":
            app.state.engine.mutate(task["id"], Mutation(version=task["version"], action="pause"))
        return result

    provider.structured = changing
    with pytest.raises(Conflict):
        await app.state.engine.chat(
            ChatInput(
                request_id="stale_request",
                conversation_id="conversation1",
                text="检查这一步",
                task_id=task["id"],
            )
        )
    current = app.state.store.task(task["id"])
    assert current["status"] == "paused" and not current["suggestions"]
    app.state.store.close()


async def test_missing_event_evidence_is_rejected(app):
    task = await ready(app)
    memory = app.state.memory
    memory.media.save("test_frame", jpeg(), task["id"], "conversation1", time.time() - 40, 1)
    memory.schedule()
    original = memory.vlm.structured

    async def bad(prompt, schema, images=None):
        result = await original(prompt, schema, images)
        if schema.__name__ == "EventDraft":
            result.observations[0].frame_ids = ["invented"]
        return result

    memory.vlm.structured = bad
    with pytest.raises(ProviderError):
        await memory.process(app.state.store.one("SELECT * FROM jobs"))
    assert not app.state.store.rows("SELECT * FROM events")
    app.state.store.close()


async def test_old_relevant_event_is_not_cut_off(app):
    task = await ready(app)
    store, memory = app.state.store, app.state.memory
    media = memory.media
    old_id = "frame_old"
    media.save(old_id, jpeg(), task["id"], "conversation1", time.time() - 3000, 1)
    with store.tx() as db:
        for index in range(40):
            data = {
                "summary": "滑轨安装方向有疑问" if index == 0 else "清理工作台",
                "objects": ["滑轨"] if index == 0 else ["桌子"],
                "observations": [],
                "unknowns": [],
            }
            db.execute(
                "INSERT INTO events(id,task_id,start,end,data,frame_ids) VALUES(?,?,?,?,?,?)",
                (
                    f"evt_{index:03}",
                    task["id"],
                    time.time() - 3000 + index * 30,
                    time.time() - 2980 + index * 30,
                    dump(data),
                    dump([old_id]),
                ),
            )
    events, images, _ = await memory.retrieve(task["id"], "滑轨为什么推不进去")
    assert "evt_000" in [e["id"] for e in events]
    assert old_id in [im.id for im in images]
    assert len(images) <= memory.settings.max_images
    store.close()


async def test_original_source_survives_new_unused_sources(app):
    task = await ready(app)
    adopted = task["state"]["plan_source_ids"]
    for i in range(6):
        app.state.engine.save_source(task["id"], str(i), "user-provided", "unrelated reference content")
    seen = []
    original = app.state.engine.vlm.structured

    async def inspect(prompt, schema, images=None):
        if schema.__name__ == "Answer":
            context = json.loads(prompt.split("\n")[-1])
            seen.extend(s["id"] for s in context["reference_sources"])
        return await original(prompt, schema, images)

    app.state.engine.vlm.structured = inspect
    await app.state.engine.chat(
        ChatInput(
            request_id="source_request",
            conversation_id="conversation1",
            text="这一步怎么办",
            task_id=task["id"],
        )
    )
    assert seen == adopted
    app.state.store.close()


def test_media_limits_and_missing_file(tmp_path):
    store = Store(tmp_path)
    media = MediaStore(store, Settings(data_dir=tmp_path, max_storage_mb=100))
    with pytest.raises(ValueError):
        media.save("f_bad", b"not image", None, "conversation1", time.time(), 1)
    with pytest.raises(ValueError):
        media.save("f_nan", jpeg(), None, "conversation1", float("nan"), 1)
    frame = media.save("f_good", jpeg(), None, "conversation1", time.time(), 1)
    (store.directory / frame["path"]).unlink()
    with pytest.raises(KeyError):
        media.path(frame["id"])
    store.close()


def test_instant_followup_can_reuse_picture(client):
    response = client.post(
        "/api/media",
        data={
            "media_id": "frame_previous",
            "conversation_id": "conversation1",
            "captured": time.time(),
            "seq": 1,
        },
        files={"file": ("test.jpg", jpeg())},
    )
    assert response.status_code == 200
    for request_id, frame_id in [("request_initial", "frame_previous"), ("request_followup", None)]:
        result = client.post(
            "/api/chat",
            json={
                "request_id": request_id,
                "conversation_id": "conversation1",
                "text": "右边那个呢",
                "mode": "instant",
                "frame_id": frame_id,
            },
        )
        assert result.status_code == 200
        assert result.json()["evidence_ids"] == ["frame_previous"]


def test_retention_does_not_remove_unfinished_task(tmp_path):
    store = Store(tmp_path)
    media = MediaStore(store, Settings(data_dir=tmp_path))
    task = store.create_task("unfinished")
    store.change(task["id"], 1, lambda row, state, plan, db: ("active", state, plan, "start", {}))
    frame = media.save("f_active", jpeg(), task["id"], "conversation1", time.time(), 1)
    instant = media.save("f_instant", jpeg(), None, "conversation1", time.time(), 1)
    with store.tx() as db:
        db.execute("UPDATE media SET received=?", (time.time() - 86400 * 9,))
    media.cleanup()
    assert media.path(frame["id"])
    with pytest.raises(KeyError):
        media.path(instant["id"])
    store.close()


def test_quota_exhaustion_is_distinct_from_invalid_input(app, client):
    from along.media import CapacityError

    media = app.state.memory.media
    media.save("quota_seed", jpeg(), None, "conversation1", time.time(), 1)
    with app.state.store.tx() as db:
        db.execute("UPDATE media SET bytes=?", (app.state.memory.settings.max_storage_mb * 1024 * 1024,))
    with pytest.raises(CapacityError):
        media.save("quota_over", jpeg(), None, "conversation1", time.time(), 2)
    response = client.post(
        "/api/media",
        data={
            "media_id": "quota_http",
            "conversation_id": "conversation1",
            "captured": time.time(),
            "seq": 3,
        },
        files={"file": ("test.jpg", jpeg())},
    )
    assert response.status_code == 507
