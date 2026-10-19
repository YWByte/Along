"""Real-model integration using synthetic visual fixtures, not a user-study benchmark."""

import asyncio
import io
import tempfile
import time
import uuid
from pathlib import Path

import httpx
from PIL import Image, ImageDraw

from along.api import create_app
from along.config import Settings


def fixture(swapped=False):
    image = Image.new("RGB", (500, 260), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((40 if not swapped else 340, 80, 140 if not swapped else 440, 180), fill="red")
    draw.ellipse((340 if not swapped else 40, 80, 440 if not swapped else 140, 180), fill="blue")
    output = io.BytesIO()
    image.save(output, "JPEG")
    return output.getvalue()


async def main():
    with tempfile.TemporaryDirectory(prefix="along-integration-") as tmp:
        app = create_app(Settings(data_dir=Path(tmp), worker_enabled=False))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url="http://testserver",
                headers={"X-Along-Token": app.state.token},
                timeout=300,
            ) as client:

                async def post(path, **kwargs):
                    response = await client.post(path, **kwargs)
                    assert response.is_success, (response.status_code, response.text)
                    return response.json()

                async def upload(swapped, task_id=None):
                    return await post(
                        "/api/media",
                        data={
                            "media_id": uuid.uuid4().hex,
                            "conversation_id": "integration_session",
                            "captured": time.time(),
                            "seq": time.time_ns() // 1000,
                            "origin": "upload",
                            **({"task_id": task_id} if task_id else {}),
                        },
                        files={"file": ("fixture.jpg", fixture(swapped), "image/jpeg")},
                    )

                async def ask(text, **kwargs):
                    return await post(
                        "/api/chat",
                        json={
                            "request_id": uuid.uuid4().hex,
                            "conversation_id": "integration_session",
                            "text": text,
                            **kwargs,
                        },
                    )

                clarify = await ask("我们开始吧")
                assert clarify["kind"] == "clarify", clarify
                print("CLARIFY:", clarify["reply"], flush=True)
                frame = await upload(False)
                answer = await ask("左右两个物体分别是什么颜色和形状？", mode="instant", frame_id=frame["id"])
                assert "红" in answer["reply"] and "蓝" in answer["reply"], answer
                print("INSTANT:", answer["reply"], flush=True)
                task = await post(
                    "/api/tasks", json={"goal": "按照桌面摆放说明，把红色正方形放左边、蓝色圆形放右边"}
                )
                await post(
                    f"/api/tasks/{task['id']}/sources",
                    json={
                        "title": "测试桌面摆放说明",
                        "content": "适用条件：桌面上有一个红色正方形和一个蓝色圆形。这是无危险的图形排列任务。"
                        "第一步观察两个图形的位置。第二步将红色正方形放在左侧，蓝色圆形放在右侧。"
                        "第三步分别确认红色在左、蓝色在右。不需要加热、切割或任何动力设备。",
                    },
                )
                task = await post(f"/api/tasks/{task['id']}/prepare", json={})
                print("PLAN:", task["plan"], flush=True)
                task = await post(
                    f"/api/tasks/{task['id']}/change",
                    json={"version": task["version"], "action": "confirm_plan", "value": "已核对适用条件"},
                )
                before = await upload(False, task["id"])
                await asyncio.sleep(1)
                after = await upload(True, task["id"])
                task = await post(
                    f"/api/tasks/{task['id']}/change", json={"version": task["version"], "action": "pause"}
                )
                app.state.memory.schedule()
                for job in app.state.store.rows("SELECT * FROM jobs WHERE status='pending'"):
                    await app.state.memory.process(job)
                answer = await ask(
                    "刚才两个图形的位置发生了什么变化？现在是否符合操作表？",
                    task_id=task["id"],
                    frame_id=after["id"],
                )
                print("HISTORY:", answer, flush=True)
                assert answer["task_id"] == task["id"]
                assert before["id"] in answer["retrieval"]["frames"], answer
                assert after["id"] in answer["retrieval"]["frames"], answer
                task = app.state.store.task(task["id"])
                task = await post(
                    f"/api/tasks/{task['id']}/change", json={"version": task["version"], "action": "finish"}
                )
                assert task["status"] == "ended"
                print("REAL INTEGRATION PASS (synthetic fixtures; no physical safety claims)", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
