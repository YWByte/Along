"""Replay a recorded task at a historical cutoff in a temporary SQLite/media copy.

No web access, recording, cleanup or writes to the source directory are permitted.
The cutoff is a simulated observation time, not proof of a current live scene.
"""

import argparse
import asyncio
import json
import logging
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from unittest.mock import patch
import uuid

from along.api import create_app
from along.config import Settings
from along.memory_tools import MemoryTools
from along.schemas import ChatInput
from along.store import dump


class NoWeb:
    async def search(self, query):
        raise AssertionError("A simple wrist replay must not request web search")

    async def fetch(self, url):
        raise AssertionError("A simple wrist replay must not fetch webpages")


def snapshot(source_dir, target, task_id, cutoff):
    source = sqlite3.connect((source_dir / "along.sqlite3").as_uri() + "?mode=ro", uri=True)
    destination = sqlite3.connect(target / "along.sqlite3")
    try:
        source.backup(destination)
        destination.row_factory = sqlite3.Row
        checkpoint = destination.execute(
            "SELECT * FROM checkpoints WHERE task_id=? AND created<? ORDER BY created DESC LIMIT 1",
            (task_id, cutoff),
        ).fetchone()
        if not checkpoint:
            raise ValueError("No task checkpoint exists before the requested cutoff")
        state = json.loads(checkpoint["state"])
        destination.execute(
            "UPDATE tasks SET status=?,state=?,plan=?,version=?,updated=? WHERE id=?",
            (
                state["status"],
                dump(state["state"]),
                dump(state["plan"]),
                checkpoint["version"],
                cutoff,
                task_id,
            ),
        )
        destination.execute("DELETE FROM messages WHERE created>=?", (cutoff,))
        destination.execute("DELETE FROM checkpoints WHERE created>=?", (cutoff,))
        destination.execute("DELETE FROM ledger WHERE created>=?", (cutoff,))
        destination.execute("DELETE FROM events WHERE end>=?", (cutoff,))
        destination.execute("DELETE FROM media WHERE captured>=?", (cutoff,))
        destination.execute("DELETE FROM requests")
        destination.execute("DELETE FROM jobs")
        destination.commit()
        frames = destination.execute(
            "SELECT path FROM media WHERE task_id=? AND expired=0", (task_id,)
        ).fetchall()
        (target / "media").mkdir()
        for row in frames:
            path = source_dir / row["path"]
            if path.is_file():
                shutil.copy2(path, target / row["path"])
        print(
            dump({"replay_cutoff": cutoff, "copied_frames": len(frames), "source_read_only": True}),
            flush=True,
        )
    finally:
        source.close()
        destination.close()


async def main(args):
    with tempfile.TemporaryDirectory(prefix="along-memory-replay-") as tmp:
        directory = Path(tmp)
        snapshot(args.source_dir, directory, args.task_id, args.cutoff)
        app = create_app(
            Settings(data_dir=directory, worker_enabled=False, semantic_enabled=False), web=NoWeb()
        )
        async with app.router.lifespan_context(app):
            frame = app.state.store.one(
                "SELECT id FROM media WHERE task_id=? AND kind='frame' ORDER BY captured DESC LIMIT 1",
                (args.task_id,),
            )
            started = time.monotonic()
            with patch(
                "along.engine.MemoryTools",
                side_effect=lambda memory, task_id: MemoryTools(memory, task_id, args.cutoff),
            ):
                result = await app.state.engine.chat(
                    ChatInput(
                        request_id=uuid.uuid4().hex,
                        conversation_id="memory_replay",
                        task_id=args.task_id,
                        mode="task",
                        text=args.question,
                        frame_id=frame["id"] if frame else None,
                    )
                )
            print(
                dump({"elapsed_seconds": round(time.monotonic() - started, 2), "result": result}), flush=True
            )
            tools = result.get("retrieval", {}).get("tools", [])
            assert any(entry["call"]["tool"] == "open" for entry in tools), "Agent did not open a clip"
            assert 4 <= len(result["retrieval"]["frames"]) <= 12, "Agent did not review a bounded sequence"
            assert not result["source_ids"], "Simple wrist replay unexpectedly relied on web sources"
            assert len(result["missing"]) <= 1, "Too many interruptions"
            print(
                "MEMORY REPLAY PASS (retrieval/interaction contract, not motion correctness certification)",
                flush=True,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--cutoff", type=float, required=True)
    parser.add_argument("--question", default="我现在做的过程对吗")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main(parser.parse_args()))
