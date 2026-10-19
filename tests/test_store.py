import hashlib
import io
import time

import pytest
from PIL import Image

from along.config import Settings
from along.media import MediaStore
from along.store import Conflict, Store


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path)
    yield result
    result.close()


def test_single_task_and_versions(store):
    task = store.create_task("test")
    with pytest.raises(Conflict):
        store.create_task("second")
    changed = store.change(
        task["id"], 1, lambda row, state, plan, db: ("active", state, plan, "confirmed", {})
    )
    assert changed["version"] == 2
    with pytest.raises(Conflict):
        store.change(task["id"], 1, lambda *args: None)
    assert len(store.rows("SELECT * FROM checkpoints")) == 1


def test_request_idempotency(store):
    assert store.begin_request("r1", "hash") is None
    with pytest.raises(Conflict):
        store.begin_request("r1", "hash")
    store.complete_request("r1", {"reply": "ok"})
    assert store.begin_request("r1", "hash")["reply"] == "ok"
    with pytest.raises(Conflict):
        store.begin_request("r1", "changed")


def test_recovery(tmp_path):
    store = Store(tmp_path)
    task = store.create_task("test")
    store.begin_request("r", "h")
    with store.tx() as db:
        db.execute("INSERT INTO jobs VALUES('j',?,'[]','running',1,0,NULL)", (task["id"],))
    store.close()
    store = Store(tmp_path)
    assert store.one("SELECT status FROM jobs")["status"] == "pending"
    assert store.begin_request("r", "h") is None
    store.close()


def image_bytes():
    out = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(out, "JPEG")
    return out.getvalue()


def test_media_duplicate_and_pause(store, tmp_path):
    media = MediaStore(store, Settings(data_dir=tmp_path))
    task = store.create_task("test")
    with pytest.raises(Conflict):
        media.save("f1", image_bytes(), task["id"], "conv", time.time(), 1)
    store.change(task["id"], 1, lambda row, state, plan, db: ("active", state, plan, "start", {}))
    a = media.save("f1", image_bytes(), task["id"], "conv", time.time(), 1)
    b = media.save("f1", image_bytes(), task["id"], "conv", time.time(), 1)
    assert a["id"] == b["id"]
    assert a["digest"] == hashlib.sha256(image_bytes()).hexdigest()
    store.delete_task(task["id"])
    assert not (store.directory / a["path"]).exists()
    assert not store.rows("SELECT * FROM media")


def test_messages_are_scoped(store):
    task = store.create_task("test")
    store.message("c", task["id"], "user", "task")
    store.message("c", None, "user", "instant")
    assert store.messages("c") == [{"role": "user", "text": "instant"}]
    assert store.messages("c", task_id=task["id"]) == [{"role": "user", "text": "task"}]
