"""Real-provider wiring workflow probe with explicitly synthetic, isolated evidence."""

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from along.config import Settings
from along.engine import Engine, enforce_wiring_verdict
from along.media import MediaStore
from along.memory import Memory
from along.providers.vlm import ImageInput, create_vlm_provider
from along.providers.web import DeepSeekWebProvider
from along.schemas import Answer, ChatInput, Mutation
from along.store import Store, uid

BOARD_URL = "https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32/esp32-devkitc/user_guide.html"
SENSOR_URL = "https://learn.adafruit.com/adafruit-bme280-humidity-barometric-pressure-temperature-sensor-breakout/pinouts"
LEFT = [
    "3V3",
    "EN",
    "VP",
    "VN",
    "IO34",
    "IO35",
    "IO32",
    "IO33",
    "IO25",
    "IO26",
    "IO27",
    "IO14",
    "IO12",
    "GND",
    "IO13",
    "D2",
    "D3",
    "CMD",
    "5V",
]
RIGHT = [
    "GND",
    "IO23",
    "IO22",
    "TX",
    "RX",
    "IO21",
    "GND",
    "IO19",
    "IO18",
    "IO5",
    "IO17",
    "IO16",
    "IO4",
    "IO0",
    "IO2",
    "IO15",
    "D1",
    "D0",
    "CLK",
]
EXPECTED = [
    ("3V3", "Vin", "red"),
    ("GND", "GND", "black"),
    ("IO21", "SDI", "orange"),
    ("IO22", "SCK", "blue"),
]


def diagram(path, count=4, wrong=False, obscured=False, ground_side="right"):
    image = Image.new("RGB", (1280, 960), "#edf0f2")
    draw = ImageDraw.Draw(image)
    font = (
        ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 21)
        if Path("/System/Library/Fonts/Supplemental/Arial.ttf").exists()
        else ImageFont.load_default(size=21)
    )
    draw.text(
        (30, 18), "SIMULATED TOPOLOGY - NOT A PHYSICAL BOARD / POWER DISCONNECTED", font=font, fill="#333333"
    )
    draw.rounded_rectangle((110, 70, 420, 920), radius=16, fill="#175a4b")
    draw.text((155, 85), "ESP32-DevKitC V4", font=font, fill="white")
    draw.text((145, 114), "WROOM-32E", font=font, fill="white")
    positions = {}
    for side, labels, x in [("left", LEFT, 120), ("right", RIGHT, 410)]:
        for i, label in enumerate(labels):
            y = 163 + i * 39
            draw.ellipse((x - 6, y - 6, x + 6, y + 6), fill="#d5bc68", outline="black")
            draw.text((x + 15 if side == "left" else x - 75, y - 12), label, font=font, fill="white")
            positions.setdefault(label, (x, y))
    positions["GND"] = (120, 163 + 13 * 39) if ground_side == "left" else (410, 163)
    draw.rounded_rectangle((930, 250, 1240, 660), radius=15, fill="#19558a")
    draw.text((950, 275), "Adafruit BME280", font=font, fill="white")
    draw.text((950, 305), "PID 2652", font=font, fill="white")
    sensor_positions = {}
    for i, label in enumerate(["Vin", "3Vo", "GND", "SCK", "SDO", "SDI", "CS"]):
        y = 355 + i * 42
        sensor_positions[label] = (940, y)
        draw.ellipse((934, y - 6, 946, y + 6), fill="#d5bc68", outline="black")
        draw.text((960, y - 12), label, font=font, fill="white")
    for index, (board, sensor, color) in enumerate(EXPECTED[:count]):
        board = "IO19" if wrong and board == "IO21" else board
        start, end = positions[board], sensor_positions[sensor]
        via = 460 + index * 105
        points = (
            [
                start,
                (55 + index * 20, start[1]),
                (55 + index * 20, 940 - index * 12),
                (850 + index * 35, 940 - index * 12),
                (850 + index * 35, end[1]),
                end,
            ]
            if start[0] < 200
            else [start, (via, start[1]), (via, end[1]), end]
        )
        draw.line(points, fill=color, width=7, joint="curve")
        for x, y in (start, end):
            draw.ellipse((x - 7, y - 7, x + 7, y + 7), fill=color, outline="white", width=2)
    if obscured:
        draw.rounded_rectangle((300, 285, 480, 635), radius=30, fill="#c8c8c8")
        draw.text((322, 525), "OCCLUDED", font=font, fill="#333333")
    image.save(path, quality=94)
    return path


async def main(output):
    output.mkdir(parents=True, exist_ok=False)
    settings = Settings(data_dir=output / "data", worker_enabled=False, semantic_enabled=False)
    store = Store(settings.data_dir)
    provider, web = create_vlm_provider(settings), DeepSeekWebProvider(settings)
    media = MediaStore(store, settings)
    memory = Memory(store, media, provider, settings)
    engine = Engine(store, memory, provider, web)
    try:
        task = store.create_task(
            "用 ESP32-DevKitC V4（WROOM-32E）和 Adafruit BME280 PID 2652 通过 I2C 读取温湿度。用 GPIO21 SDA / GPIO22 SCL。仅做断电接线模拟，不实际通电。"
        )
        for url in (BOARD_URL, SENSOR_URL):
            print("FETCH", url, flush=True)
            engine.save_source(task["id"], url, url, await web.fetch(url))
        reference = diagram(output / "reference.jpg", count=0)
        row = media.save(
            uid("frame"),
            reference.read_bytes(),
            task["id"],
            "wiring-probe",
            time.time(),
            0,
            origin="reference",
        )
        task = await engine.prepare(task["id"], frame_id=row["id"])
        (output / "plan.json").write_text(json.dumps(task, ensure_ascii=False, indent=2))
        print(
            "PLAN",
            task["state"].get("scenario"),
            task["state"].get("error"),
            task["state"].get("pending_questions"),
            flush=True,
        )
        assert task["state"].get("scenario") == "wiring", "Wrong scenario routing"
        assert task["plan"] and task["plan"].get("connections"), "No grounded connection plan"
        assert not task["state"].get("pending_questions"), "Unexpected blocking question for known parts"
        task = engine.mutate(task["id"], Mutation(version=task["version"], action="confirm_plan"))
        # Replay timestamps are spaced at 1 Hz; the fixture does not claim live camera capture.
        ground = next(c for c in task["plan"]["connections"] if "GND" in c["to_pin"])
        ground_side = "left" if "J2" in ground["from_pin"] or "左" in ground["from_pin"] else "right"
        epoch = time.time() - 30
        for seq in range(20):
            image = diagram(
                output / f"frame-{seq + 1:02}.jpg",
                count=min(4, seq // 2 + 1),
                wrong=True,
                ground_side=ground_side,
            )
            media.save(
                uid("frame"),
                image.read_bytes(),
                task["id"],
                "wiring-probe",
                epoch + seq,
                seq + 1,
                origin="replay",
            )
        while True:
            memory.schedule()
            job = store.one("SELECT * FROM jobs WHERE status='pending' ORDER BY available LIMIT 1")
            if not job:
                break
            await memory.process(job)
        events = store.rows("SELECT * FROM events WHERE task_id=? ORDER BY start", (task["id"],))
        (output / "events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2))
        print("EVENTS", len(events), flush=True)
        result = await engine.chat(
            ChatInput(
                request_id=uid("req"),
                conversation_id="wiring-probe",
                task_id=task["id"],
                mode="task",
                text="请核对整个接线表。我刚才所有线接对了吗？请回看插线过程给出判断。",
            )
        )
        (output / "trace.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print("ANSWER", result.get("verdict"), result.get("reply"), flush=True)
        assert result.get("retrieval", {}).get("tools"), "Agent did not retrieve historical images"
        assert result.get("verdict") == "adjust", "Synthetic clearly wrong endpoint was not identified"
        for case, obscured, expected in [("correct", False, "ok"), ("occluded", True, "unclear")]:
            picture = diagram(output / f"{case}.jpg", obscured=obscured, ground_side=ground_side)
            frame_id = f"fixture_{case}"
            answer = await provider.structured(
                "核对这张明确标注的模拟接线图，不调用记忆或联网；仅验证图上的端点，不认证实物安全。"
                "对整个接线表逐条输出connection_checks（id、met/not_met/unseen、frame_ids）。"
                "两端清楚可见且匹配才met，清楚插错not_met，遮挡或无法辨认就是unseen。"
                "全部met才ok，有not_met为adjust，其余unclear。reply简短，不要求实际通电。\n"
                + json.dumps({"connections": task["plan"]["connections"]}, ensure_ascii=False),
                Answer,
                [ImageInput(frame_id, picture, "模拟图，非实物；供电断开")],
            )
            enforce_wiring_verdict(answer, {"adopted_plan": task["plan"]}, {frame_id})
            (output / f"{case}-answer.json").write_text(answer.model_dump_json(indent=2))
            print("FIXTURE", case, answer.verdict, answer.reply, flush=True)
            assert answer.verdict == expected, f"Unexpected verdict for {case}"
        print("WIRING WORKFLOW PASS - synthetic topology only", flush=True)
    finally:
        await provider.close()
        await web.close()
        store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main(args.output))
