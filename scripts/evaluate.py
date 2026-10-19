"""Evaluate labeled retrieval queries against an existing ALONG task without modifying its history.

Input JSON: [{"question":"...", "event_ids":["evt_..."], "frame_ids":["..."]}].
Run while the ALONG server is stopped, or against a copy of its data directory.
"""

import argparse
import asyncio
import json
from pathlib import Path

from along.config import Settings
from along.media import MediaStore
from along.memory import Memory
from along.providers.vlm import CodexProxyProvider
from along.store import Store


async def evaluate(args):
    import fcntl

    settings = Settings(data_dir=args.data_dir)
    if not (settings.data_dir / "along.sqlite3").exists():
        raise ValueError("数据目录没有 ALONG 数据库")
    lock = (settings.data_dir / "server.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("请先停止服务，或使用数据目录的副本") from None
    store = Store(settings.data_dir)
    provider = CodexProxyProvider(settings)
    memory = Memory(store, MediaStore(store, settings), provider, settings)
    try:
        store.task(args.task_id)
        labels = json.loads(args.labels.read_text())
        totals = []
        for label in labels:
            events, images, warnings = await memory.retrieve(args.task_id, label["question"])
            found_events, found_frames = {e["id"] for e in events}, {im.id for im in images}
            wanted_events, wanted_frames = set(label.get("event_ids", [])), set(label.get("frame_ids", []))
            result = {
                "question": label["question"],
                "event_recall": len(found_events & wanted_events) / len(wanted_events)
                if wanted_events
                else None,
                "frame_recall": len(found_frames & wanted_frames) / len(wanted_frames)
                if wanted_frames
                else None,
                "events_returned": len(events),
                "frames_returned": len(images),
                "warnings": warnings,
            }
            totals.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
        for key in ("event_recall", "frame_recall"):
            values = [r[key] for r in totals if r[key] is not None]
            print(f"{key} mean={sum(values) / len(values) if values else 'unlabeled'}", flush=True)
    finally:
        await provider.close()
        store.close()
        lock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--labels", type=Path, required=True)
    asyncio.run(evaluate(parser.parse_args()))
