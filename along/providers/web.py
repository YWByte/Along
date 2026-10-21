import asyncio
import ipaddress
import socket
from typing import Protocol
from urllib.parse import urlparse

import httpx

from along.config import Settings
from along.providers.vlm import ProviderError


class WebProvider(Protocol):
    async def search(self, query: str) -> list[dict]: ...
    async def fetch(self, url: str) -> str: ...
    async def close(self) -> None: ...


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


class DeepSeekWebProvider:
    def __init__(self, settings: Settings):
        self.settings = settings
        base = settings.deepseek_base_url.rstrip("/")
        self.tools_url = f"{base}/tools" if base.endswith("/v1") else f"{base}/v1/tools"
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout, connect=10))

    def headers(self) -> dict[str, str]:
        if self.settings.deepseek_api_key is None:
            raise ProviderError("DeepSeek 搜索未配置 ALONG_DEEPSEEK_API_KEY")
        key = self.settings.deepseek_api_key.get_secret_value().strip()
        if not key:
            raise ProviderError("DeepSeek 搜索未配置 ALONG_DEEPSEEK_API_KEY")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    async def request(self, endpoint: str, payload: dict) -> dict:
        for attempt in range(2):
            try:
                response = await self.client.post(
                    f"{self.tools_url}/{endpoint}", headers=self.headers(), json=payload
                )
                if response.status_code in (408, 429, 500, 502, 503, 504) and attempt == 0:
                    await asyncio.sleep(1)
                    continue
                if response.status_code == 401:
                    raise ProviderError("DeepSeek 搜索鉴权失败；请检查 API Key")
                if response.status_code == 402:
                    raise ProviderError("DeepSeek 搜索账户余额不足")
                if response.is_error:
                    raise ProviderError(
                        f"DeepSeek {endpoint} HTTP {response.status_code}；未使用虚构资料替代"
                    )
                body = response.json()
                if not isinstance(body, dict):
                    raise ProviderError(f"DeepSeek {endpoint} 返回格式无效")
                return body
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == 0:
                    await asyncio.sleep(1)
                    continue
                raise ProviderError(f"DeepSeek {endpoint} 连接失败或超时") from exc
            except ValueError as exc:
                raise ProviderError(f"DeepSeek {endpoint} 返回的 JSON 无效") from exc
        raise ProviderError(f"DeepSeek {endpoint} 调用失败")

    async def search(self, query: str) -> list[dict]:
        query = query.strip()
        if not 1 <= len(query) <= 128:
            raise ValueError("搜索词长度必须为 1 到 128 个字符")
        body = await self.request("search", {"query": query, "count": 5, "need_content": True})
        raw_results = body.get("results")
        if not isinstance(raw_results, list):
            raise ProviderError("DeepSeek search 响应缺少 results")
        results = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            title, url = item.get("title"), item.get("url")
            if not isinstance(title, str) or not isinstance(url, str):
                continue
            results.append(
                {
                    "title": title,
                    "url": url,
                    "snippet": item.get("snippet") or item.get("summary") or "",
                    "content": item.get("content") if isinstance(item.get("content"), str) else "",
                    "authority": item.get("authority"),
                    "rerank_score": item.get("rerank_score"),
                }
            )
        return results

    async def fetch(self, url: str) -> str:
        await validate_public_url(url)
        body = await self.request("fetch", {"url": url})
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("DeepSeek fetch 没有返回可用正文")
        return content

    async def close(self) -> None:
        await self.client.aclose()
