"""Exercise real model research decisions without modifying tasks or calling web tools."""

import asyncio
import tempfile
import uuid
from pathlib import Path

from along.api import create_app

from along.config import Settings
from along.engine import RESEARCH_POLICY
from along.providers.vlm import create_vlm_provider
from along.schemas import ChatInput, Mutation, ResearchDecision
from along.store import dump


async def no_web_flow():
    class NoWeb:
        async def search(self, query):
            raise AssertionError("A common task unexpectedly requested search")

        async def fetch(self, url):
            raise AssertionError("A common task unexpectedly requested a webpage")

    with tempfile.TemporaryDirectory(prefix="along-research-") as tmp:
        app = create_app(
            Settings(data_dir=Path(tmp), worker_enabled=False, semantic_enabled=False), web=NoWeb()
        )
        async with app.router.lifespan_context(app):
            engine = app.state.engine
            task = app.state.store.create_task("整理书桌上的书、本子、笔，已有两个收纳盒")
            task = await engine.prepare(task["id"])
            assert task["plan"] and task["plan"]["basis"] == "general_knowledge", task
            task = engine.mutate(
                task["id"], Mutation(version=task["version"], action="confirm_plan", value="已核对适用条件")
            )
            reply = await engine.chat(
                ChatInput(
                    request_id=uuid.uuid4().hex,
                    conversation_id="research_probe",
                    mode="task",
                    task_id=task["id"],
                    text="本子应该按用途还是大小分类更方便取用？",
                )
            )
            assert reply["task_id"] == task["id"] and not reply["source_ids"]
            print("NO WEB TASK FLOW PASS:", reply["reply"], flush=True)


async def main():
    provider = create_vlm_provider(Settings())
    cases = [
        ("把熟香蕉切片与原味酸奶拌成一碗早餐，已有干净碗勺和刀具", {"direct"}),
        ("整理书桌上的书、本子、笔，已有两个收纳盒", {"direct"}),
        ("修理家里无法工作的机器，不知道品牌、型号和哪里坏了", {"clarify"}),
        ("查询 Sony ILCE-7M4 相机截至2026年10月的最新固件版本及更新方法", {"search"}),
        ("第一次钻到汽车下面维修，没有确认车型、支撑位置，也没有安全支架", {"clarify"}),
    ]
    try:
        for goal, expected in cases:
            result = await provider.structured(
                RESEARCH_POLICY
                + "决定本次准备方案是否需要搜索。\n"
                + dump(
                    {
                        "goal": goal,
                        "user_notes": [],
                        "revision_reason": "",
                        "previous_plan": None,
                        "sources": [],
                    }
                ),
                ResearchDecision,
            )
            print(dump({"goal": goal, "decision": result.model_dump()}), flush=True)
            assert result.action in expected, f"Expected {expected}, got {result.action}"
        print("RESEARCH DECISION PROBE PASS", flush=True)
    finally:
        await provider.close()
    await no_web_flow()


if __name__ == "__main__":
    asyncio.run(main())
