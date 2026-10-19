import httpx
import pytest
from along.device import CaptureSpool


async def test_capture_spool_retries_same_identity(tmp_path):
    spool = CaptureSpool(tmp_path)
    path = spool.save(b"jpeg", "task_test")
    ids = []

    def fail(request):
        raise httpx.ConnectError("offline")

    async with httpx.AsyncClient(base_url="http://localhost", transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(httpx.ConnectError):
            await spool.flush(client)
    assert path.exists()

    def success(request):
        ids.append(request.content)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(
        base_url="http://localhost", transport=httpx.MockTransport(success)
    ) as client:
        await spool.flush(client)
        await spool.flush(client)
    assert not path.exists() and len(ids) == 1


def test_spool_does_not_discard_when_full(tmp_path):
    spool = CaptureSpool(tmp_path, max_bytes=10)
    with pytest.raises(RuntimeError):
        spool.save(b"x" * 20, "task_test")
