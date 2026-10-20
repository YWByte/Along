"""Optional native capture adapter; use a loopback SSH tunnel for a remote backend."""

import argparse
import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Protocol

import httpx

log = logging.getLogger(__name__)


class CaptureSource(Protocol):
    def frame(self) -> bytes: ...
    def close(self) -> None: ...


class OpenCVCamera:
    def __init__(self, index: int):
        import cv2

        self.cv2 = cv2
        self.camera = cv2.VideoCapture(index)
        if not self.camera.isOpened():
            raise RuntimeError("无法打开摄像头，请检查设备编号和系统权限")
        self.camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        self.camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

    def frame(self) -> bytes:
        ok, frame = self.camera.read()
        if not ok:
            raise RuntimeError("摄像头没有返回画面")
        ok, encoded = self.cv2.imencode(".jpg", frame, [self.cv2.IMWRITE_JPEG_QUALITY, 82])
        if not ok:
            raise RuntimeError("图片编码失败")
        return encoded.tobytes()

    def close(self):
        self.camera.release()


class CaptureSpool:
    def __init__(self, directory: Path, max_bytes=80 * 1024 * 1024):
        self.directory = directory
        self.max_bytes = max_bytes
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def save(self, raw: bytes, task_id: str):
        used = sum(p.stat().st_size for p in self.directory.iterdir() if p.is_file())
        if used + len(raw) * 2 > self.max_bytes:
            raise RuntimeError("本地补传队列已满，请恢复连接；不会静默丢弃记录")
        import base64

        entry = {
            "id": uuid.uuid4().hex,
            "task_id": task_id,
            "captured": time.time(),
            "seq": time.time_ns() // 1000,
            "data": base64.b64encode(raw).decode(),
        }
        path = self.directory / f"{entry['seq']}-{entry['id']}.json"
        temporary = path.with_suffix(".part")
        temporary.write_text(json.dumps(entry))
        os.replace(temporary, path)
        return path

    async def flush(self, client: httpx.AsyncClient):
        import base64

        for path in sorted(self.directory.glob("*.json")):
            entry = json.loads(path.read_text())
            response = await client.post(
                "/api/media",
                data={
                    "media_id": entry["id"],
                    "conversation_id": "native_device",
                    "task_id": entry["task_id"],
                    "captured": entry["captured"],
                    "seq": entry["seq"],
                    "kind": "frame",
                    "origin": "camera",
                },
                files={"file": ("frame.jpg", base64.b64decode(entry["data"]), "image/jpeg")},
            )
            if response.status_code in (404, 409):
                # Keep rejected evidence for inspection, but do not retry it forever.
                path.rename(path.with_suffix(".rejected"))
                log.warning("capture rejected status=%d id=%s", response.status_code, entry["id"])
                continue
            response.raise_for_status()
            path.unlink()
            log.info("capture delivered id=%s", entry["id"])


async def run(camera: CaptureSource, client: httpx.AsyncClient, spool: CaptureSpool, interval=1):
    active_task = None
    last_contact = 0
    try:
        while True:
            tick = time.monotonic()
            try:
                response = await client.get("/api/state")
                response.raise_for_status()
                task = response.json().get("task")
                active_task = task["id"] if task and task["status"] == "active" else None
                last_contact = time.monotonic()
            except httpx.HTTPError:
                log.warning("backend unreachable; recent active-task captures can be buffered for 60 seconds")
            if active_task and time.monotonic() - last_contact < 60:
                raw = await asyncio.to_thread(camera.frame)
                spool.save(raw, active_task)
                log.info("capture saved task=%s", active_task)
            try:
                await spool.flush(client)
            except httpx.HTTPError:
                log.warning("upload deferred; local evidence retained")
            await asyncio.sleep(max(0, interval - (time.monotonic() - tick)))
    finally:
        camera.close()


def main():
    parser = argparse.ArgumentParser(
        description="Optional native ALONG camera client; install the device extra first"
    )
    parser.add_argument("--server", default="http://127.0.0.1:8766")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--spool", type=Path, default=Path.home() / ".local/share/along-device")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.umask(0o077)

    async def start():
        async with httpx.AsyncClient(
            base_url=args.server, timeout=10, headers={"X-Along-Token": args.token_file.read_text().strip()}
        ) as client:
            await run(OpenCVCamera(args.camera), client, CaptureSpool(args.spool))

    try:
        asyncio.run(start())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
