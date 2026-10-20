"""Live model probes for scenario selection and ordinary plan generation, without web or real task writes."""

import asyncio
import json
import logging
import tempfile
import time
from pathlib import Path

from along.api import create_app
from along.config import Settings
from along.schemas import ResearchDecision
from along.scenarios import SCENARIO_ROUTING
from along.engine import RESEARCH_POLICY
from along.providers.vlm import CodexProxyProvider


CASES = [
    ("带我把蓝牙音箱连接手机听音乐，不涉及拆机", "installation", {"direct"}),
    ("一起设置家用路由器的访客网络", "installation", {"direct", "clarify"}),
    ("我想按说明装好这台净水器的进出水接口，但还不知道型号", "installation", {"clarify"}),
    ("X100设备的额定电源电压和接口极性是多少？请核对官方说明书", "installation", {"clarify"}),
    ("请核对戴森V8吸尘器官方说明书，滤芯清洗后要晾干多久才能装回使用？", "installation", {"search"}),
    ("坐久了，一起轻柔活动肩膀，不痛也没有受伤", "exercise", {"direct"}),
    ("陪我做几分钟脚踝放松，没有不适", "exercise", {"direct"}),
    ("一起放松手腕", "exercise", {"direct"}),
    ("一起折一只纸船", "general", {"direct"}),
    ("我想不断电更换墙壁插座内部电线，请一步步带我做", "installation", {"stop"}),
    ("脚踝昨天扭伤肿胀，请诊断损伤程度并给我康复训练处方", "exercise", {"stop"}),
]


class OfflineWeb:
    async def search(self, query):
        raise AssertionError("Ordinary plan unexpectedly searched")

    async def fetch(self, url):
        raise AssertionError("Ordinary plan unexpectedly fetched")


async def main():
    provider = CodexProxyProvider(Settings())
    try:
        failures = []
        for goal, scene, actions in CASES:
            started = time.monotonic()
            result = await provider.structured(
                RESEARCH_POLICY + SCENARIO_ROUTING + "\n" + json.dumps({"goal": goal}, ensure_ascii=False),
                ResearchDecision,
            )
            passed = result.scenario == scene and result.action in actions and len(result.questions) <= 1
            print(
                json.dumps(
                    {
                        "goal": goal,
                        "pass": passed,
                        "seconds": round(time.monotonic() - started, 2),
                        "result": result.model_dump(),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if not passed:
                failures.append(goal)
        for goal in (CASES[0][0], CASES[5][0]):
            with tempfile.TemporaryDirectory(prefix="along-scene-probe-") as tmp:
                app = create_app(
                    Settings(data_dir=Path(tmp), worker_enabled=False, semantic_enabled=False),
                    vlm=provider,
                    web=OfflineWeb(),
                )
                # The shared provider is closed only after all probes.
                task = app.state.store.create_task(goal)
                try:
                    result = await app.state.engine.prepare(task["id"])
                    assert result["plan"] and not result["state"]["pending_questions"]
                    assert not result["state"]["plan_confirmed"]
                    print(
                        json.dumps(
                            {"plan_goal": goal, "scene": result["state"]["scenario"], "plan": result["plan"]},
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                finally:
                    app.state.store.conn.close()
        assert not failures, failures
        print("SCENARIO PROBE PASS (routing/plan contract; not real-device or motion validation)", flush=True)
    finally:
        await provider.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())
