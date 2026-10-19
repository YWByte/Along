import asyncio
import ipaddress
import os
import re
import socket
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from dotenv import dotenv_values
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from along.config import Settings
from along.providers.vlm import ProviderError


class WebProvider(Protocol):
    async def search(self, query: str) -> list[dict]: ...
    async def fetch(self, url: str) -> str: ...


async def validate_public_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("只接受无凭据的公开 HTTP/HTTPS 网址")
    if parsed.port not in (None, 80, 443):
        raise ValueError("只接受标准网页端口")
    try:
        addresses = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, None)
    except OSError as exc:
        raise ValueError("无法解析网页地址") from exc
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("不允许抓取本机、私网或保留地址")
    return url


class MCPWebProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def call(self, directory: Path, tool: str, arguments: dict) -> str:
        executable = directory / "node_modules/.bin/tsx"
        if not executable.is_file():
            raise ProviderError(f"MCP 服务依赖未安装：{directory}")
        env = dict(os.environ)
        env["CODEBUDDY_TOKEN_FILE"] = str(self.settings.token_file)
        if tool == "fetch_web":
            token = dotenv_values(self.settings.token_file).get("COPILOT_TOKEN")
            if not token:
                raise ProviderError("网页 MCP 缺少可用凭据")
            env["CODEBUDDY_TOKEN"] = token
        params = StdioServerParameters(command=str(executable), args=[str(directory / "server.ts")],
                                       cwd=str(directory), env=env)
        try:
            async with asyncio.timeout(50):
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(tool, arguments)
                        if result.isError:
                            raise ProviderError(f"{tool} 返回错误；请检查网络和 MCP 服务")
                        text = "\n".join(p.text for p in result.content if p.type == "text")
                        if not text.strip():
                            raise ProviderError(f"{tool} 没有返回内容")
                        return text
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # MCP ExceptionGroup may include upstream credentials; do not expose its text.
            raise ProviderError(f"{tool} 调用失败；没有使用虚构搜索结果替代") from exc

    async def search(self, query: str) -> list[dict]:
        raw = await self.call(self.settings.search_dir, "search_web", {"query": query, "max_results": 5})
        results = []
        for block in re.split(r"\n\s*---\s*\n", raw):
            match = re.search(r"URL:\s*(https?://\S+)", block)
            if match:
                title = re.sub(r"^\[\d+\]\s*", "", block.strip().splitlines()[0])
                results.append({"title": title, "url": match.group(1), "snippet": block[-2000:]})
        return results

    async def fetch(self, url: str) -> str:
        await validate_public_url(url)
        return await self.call(self.settings.fetch_dir, "fetch_web", {
            "url": url, "prompt": "提取原始操作步骤、适用型号或条件、材料用量、风险与检查条件。不要补写原文没有的步骤。",
            "format": "markdown", "max_length": 18000, "timeout": 25,
        })
