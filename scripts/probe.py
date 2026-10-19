"""Real provider contract smoke test; never uses mock answers."""

import asyncio
import tempfile
from pathlib import Path
from PIL import Image, ImageDraw
from along.config import Settings
from along.providers.vlm import CodexProxyProvider, ImageInput
from along.providers.web import MCPWebProvider


async def main():
    settings = Settings()
    provider = CodexProxyProvider(settings)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.jpg"
            image = Image.new("RGB", (360, 180), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((20, 40, 110, 130), fill="red")
            draw.ellipse((210, 40, 300, 130), fill="blue")
            image.save(path)
            reply = await provider.text("描述左右两个图形的形状和颜色。", [ImageInput("probe", path)])
            print("VISION:", reply, flush=True)
            assert "红" in reply and "蓝" in reply, "Vision smoke assertion failed"
        web = MCPWebProvider(settings)
        results = await web.search("BBC Good Food easy biscuits recipe")
        print("SEARCH:", results[:1], flush=True)
        assert results, "No search results"
        content = await web.fetch(results[0]["url"])
        print("FETCH:", content[:800], flush=True)
        print("PROBE PASS", flush=True)
    finally:
        await provider.close()


if __name__ == "__main__":
    asyncio.run(main())
