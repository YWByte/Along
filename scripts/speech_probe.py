"""Verify local transcription with a generated speech file, without microphone access."""

import argparse
import asyncio
from pathlib import Path
from along.config import Settings
from along.speech import Speech


async def main(path):
    text = await Speech(Settings()).transcribe(path.read_bytes())
    print("TRANSCRIPT:", text, flush=True)
    if not ("抽屉" in text or "检查" in text):
        raise AssertionError("未识别出测试语音中的预期关键词")
    print("LOCAL SPEECH PASS (synthetic speech; physical microphone not tested)", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    asyncio.run(main(parser.parse_args().audio))
