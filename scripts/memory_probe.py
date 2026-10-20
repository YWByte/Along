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
from along.schemas import ChatInput, PlanDraft
from along.store import dump


class NoWeb:
    async def search(self, query):
        raise AssertionError("An offline memory replay must not request web search")

    async def fetch(self, url):
        raise AssertionError("An offline memory replay must not fetch webpages")


def snapshot(source_dir, target, task_id, cutoff, raw_only=False):
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
        destination.execute("UPDATE tasks SET status='ended' WHERE id<>?", (task_id,))
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
        if raw_only:
            destination.execute("DELETE FROM events WHERE task_id=?", (task_id,))
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


async def backfill_look_for(app, task_id):
    """Older plans predate look_for; regenerate only those fields under the current planning rules."""
    from along.scenarios import skill

    store, engine = app.state.store, app.state.engine
    task = store.task(task_id)
    revised = await engine.vlm.structured(
        "为已确认的操作表补充每步的 look_for，其余字段原样保留，步骤数量、ID、顺序不变。"
        "look_for 为每步写1至3条镜头能直接看到的完成标准，写具体部位/对象的位置、朝向或运动方向；"
        "不写感受、次数、力度、速度等画面无法判断的内容。"
        + skill(task["state"].get("scenario"))["planning"]
        + "\n"
        + dump(task["plan"]),
        PlanDraft,
    )
    by_id = {step.id: step.look_for for step in revised.steps}
    plan = task["plan"]
    for step in plan["steps"]:
        step["look_for"] = by_id.get(step["id"], [])
    with store.tx() as db:
        db.execute("UPDATE tasks SET plan=? WHERE id=?", (dump(plan), task_id))
        for row in db.execute("SELECT id,state FROM checkpoints WHERE task_id=?", (task_id,)).fetchall():
            state = json.loads(row["state"])
            if state.get("plan"):
                for step in state["plan"]["steps"]:
                    step["look_for"] = by_id.get(step["id"], [])
                db.execute("UPDATE checkpoints SET state=? WHERE id=?", (dump(state), row["id"]))
    print(dump({"backfilled_look_for": by_id}), flush=True)


async def main(args):
    with tempfile.TemporaryDirectory(prefix="along-memory-replay-") as tmp:
        directory = Path(tmp)
        snapshot(args.source_dir, directory, args.task_id, args.cutoff, args.raw_only)
        app = create_app(
            Settings(data_dir=directory, worker_enabled=False, semantic_enabled=False), web=NoWeb()
        )
        async with app.router.lifespan_context(app):
            if args.with_look_for:
                await backfill_look_for(app, args.task_id)
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
            assert 4 <= len(result["retrieval"]["frames"]) <= 72, "Agent exceeded session image budget"
            assert all(len(t["result"].get("frames", [])) <= 24 for t in tools)
            if args.expected_after is not None:
                new_frames = {
                    f["id"]
                    for t in tools
                    for f in t["result"].get("frames", [])
                    if f["captured"] > args.expected_after
                }
                assert len(new_frames) >= 4, "Agent reviewed stale footage instead of the new attempt"
            if args.expected_focus:
                assert result.get("review_focus", {}).get("step_id") == args.expected_focus, (
                    "Discussion target drifted"
                )
            if args.expected_step:
                directory_info = result["retrieval"]["directory"]
                assert directory_info["last_user_completed_step"]["step_id"] == args.expected_step
                reviewed = {
                    frame["id"]
                    for entry in tools
                    if entry["call"]["tool"] == "open"
                    for frame in entry["result"].get("frames", [])
                    if frame["step_id"] == args.expected_step
                    and frame["plan_revision"] == args.expected_revision
                }
                assert len(reviewed) >= 4, "Agent failed to inspect the previous task step"
                print(
                    dump(
                        {
                            "expected_step": args.expected_step,
                            "reviewed_target_frames": len(reviewed),
                            "raw_only": args.raw_only,
                            "directory": directory_info,
                        }
                    ),
                    flush=True,
                )
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
    parser.add_argument("--expected-step")
    parser.add_argument("--expected-focus")
    parser.add_argument("--expected-after", type=float)
    parser.add_argument("--expected-revision", type=int, default=1)
    parser.add_argument("--with-look-for", action="store_true", help="Backfill look_for for older plans")
    parser.add_argument("--raw-only", action="store_true", help="Simulate summaries not yet being available")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main(parser.parse_args()))
