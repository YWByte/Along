import io
import json
import time
import uuid

from PIL import Image


def ask(client, text, **extra):
    return client.post(
        "/api/chat",
        json={"request_id": uuid.uuid4().hex, "conversation_id": "conversation1", "text": text, **extra},
    )


def start(client):
    task = client.post("/api/tasks", json={"goal": "组装测试物件"}).json()
    response = client.post(f"/api/tasks/{task['id']}/prepare", json={})
    assert response.status_code == 200, response.text
    task = response.json()
    return task


def change(client, task, action, **extra):
    return client.post(
        f"/api/tasks/{task['id']}/change", json={"version": task["version"], "action": action, **extra}
    )


def upload(client, task=None):
    out = io.BytesIO()
    Image.new("RGB", (64, 64), "red").save(out, "JPEG")
    response = client.post(
        "/api/media",
        data={
            "media_id": uuid.uuid4().hex,
            "conversation_id": "conversation1",
            "captured": time.time(),
            "seq": int(time.time() * 1e6),
            **({"task_id": task["id"]} if task else {}),
        },
        files={"file": ("test.jpg", out.getvalue(), "image/jpeg")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_instant_image_does_not_create_task(client):
    frame = upload(client)
    result = ask(client, "这是什么？", mode="instant", frame_id=frame["id"])
    assert result.status_code == 200, result.text
    assert result.json()["evidence_ids"] == [frame["id"]]
    assert client.get("/api/state").json()["task"] is None


def test_start_requires_goal(client):
    response = ask(client, "我们开始吧")
    assert response.json()["kind"] == "clarify"
    assert client.get("/api/state").json()["task"] is None


def test_voice_start_path(client):
    response = ask(client, "我们一起组装一个物品")
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "plan"
    assert client.get("/api/state").json()["task"]["status"] == "draft"


def test_full_task_and_suggestion_lifecycle(client):
    task = start(client)
    assert task["plan"]["steps"][0]["source_ids"]
    task = change(client, task, "confirm_plan").json()
    assert task["status"] == "active"
    frame = upload(client, task)
    result = ask(client, "这一步怎么办？", task_id=task["id"], frame_id=frame["id"]).json()
    suggestion_id = result["suggestion_id"]
    task = client.get("/api/state").json()["task"]
    assert task["state"]["open_issues"][0]["status"] == "open"
    assert change(client, task, "suggestion", target=suggestion_id, value="executed").status_code == 409
    for status in ["accepted", "executed", "observed_failure", "needs_recheck"]:
        task = change(client, task, "suggestion", target=suggestion_id, value=status).json()
        assert task["suggestions"][0]["status"] == status
    task = change(client, task, "step", target="s1", value="user_reported_done").json()
    task = change(client, task, "pause").json()
    assert task["status"] == "paused"
    task = client.post(f"/api/tasks/{task['id']}/prepare", json={"reason": "返工"}).json()
    assert task["state"]["steps"]["s1"] == "unknown"
    assert task["state"]["plan_revision"] == 2
    assert change(client, task, "resume").status_code == 409
    task = change(client, task, "confirm_plan").json()
    task = change(client, task, "finish").json()
    assert task["status"] == "ended"
    timeline = client.get(f"/api/tasks/{task['id']}/timeline").json()
    assert any(entry["kind"] == "plan_proposed" for entry in timeline["ledger"])
    assert client.delete(f"/api/tasks/{task['id']}").status_code == 200
    assert client.get(f"/api/media/{frame['id']}").status_code == 404


def test_unrelated_question_preserves_task(client):
    task = start(client)
    response = ask(client, "一个无关问题", task_id=task["id"])
    assert response.status_code == 200
    assert response.json()["task_id"] is None
    current = client.get("/api/state").json()["task"]
    assert current["version"] == task["version"]
    assert not current["suggestions"]


def test_bad_evidence_is_rejected(app, client):
    app.state.engine.vlm.bad_evidence = True
    response = ask(client, "这个是什么", mode="instant")
    assert response.status_code == 502
    assert not app.state.store.rows("SELECT * FROM messages")


def test_idempotent_chat(client):
    body = {
        "request_id": "fixed_request",
        "conversation_id": "conversation1",
        "text": "hi",
        "mode": "instant",
    }
    a = client.post("/api/chat", json=body)
    b = client.post("/api/chat", json=body)
    assert a.json() == b.json()
    assert len(client.get("/api/messages/conversation1").json()) == 2


def test_security_and_validation(client):
    assert client.get("/api/state", headers={"X-Along-Token": "wrong"}).status_code == 403
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400
    assert client.post("/api/tasks", json={"goal": ""}).status_code == 422
    assert client.get("/").status_code == 200
    assert "frame-ancestors 'none'" in client.get("/").headers["content-security-policy"]
    bad = client.post(
        "/api/media",
        data={"media_id": "../escape", "conversation_id": "conversation1", "captured": time.time(), "seq": 1},
        files={"file": ("x", b"bad")},
    )
    assert bad.status_code == 422


def test_plan_failure_preserves_recoverable_draft(client, app):
    from along.providers.vlm import ProviderError

    async def broken(query):
        raise ProviderError("network unavailable")

    app.state.engine.web.search = broken
    task = client.post("/api/tasks", json={"goal": "组装物品"}).json()
    assert client.post(f"/api/tasks/{task['id']}/prepare", json={}).status_code == 502
    task = client.get("/api/state").json()["task"]
    assert task["status"] == "draft" and not task["state"]["preparing"]
    assert task["state"]["error"] == "network unavailable"


async def test_event_generation_and_recent_fallback(app):
    store, memory = app.state.store, app.state.memory
    task = store.create_task("组装")
    task = store.change(task["id"], 1, lambda row, state, plan, db: ("active", state, plan, "start", {}))
    out = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(out, "JPEG")
    frame = memory.media.save(
        "frame_test_1", out.getvalue(), task["id"], "conversation1", time.time() - 40, 1
    )
    events, images, warnings = await memory.retrieve(task["id"], "刚才做了什么")
    assert not events and images[0].id == frame["id"]
    memory.schedule()
    memory.schedule()
    jobs = store.rows("SELECT * FROM jobs")
    assert len(jobs) == 1
    await memory.process(jobs[0])
    events, images, warnings = await memory.retrieve(task["id"], "滑轨")
    assert len(events) == 1 and images[0].id == frame["id"]
    data = json.loads(events[0]["data"])
    assert data["unknowns"] == ["安装方向未确认"]
    assert data["observations"][0]["frame_ids"] == [frame["id"]]
    await app.state.engine.vlm.close()
    store.close()


def test_document_import_and_scanned_pdf_error(client):
    from pypdf import PdfWriter

    task = client.post("/api/tasks", json={"goal": "导入说明书测试"}).json()
    path = f"/api/tasks/{task['id']}/documents"
    result = client.post(
        path,
        files={"file": ("guide.txt", "确认材料和工具，按型号说明完成安装，最后核对方向与位置。".encode())},
    )
    assert result.status_code == 200
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    output = io.BytesIO()
    writer.write(output)
    result = client.post(path, files={"file": ("scan.pdf", output.getvalue(), "application/pdf")})
    assert result.status_code == 422 and "OCR" in result.json()["detail"]
    result = client.post(path, files={"file": ("bad.txt", b"\xff\xfe\x00")})
    assert result.status_code == 422
