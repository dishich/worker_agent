import os, asyncio, aiohttp
from pathlib import Path

def _normalize_disk_path(remote_path: str) -> str:
    p = (remote_path or "").strip()
    if not p:
        raise RuntimeError("Empty remote_path")
    if p.startswith("disk:/"):
        return p
    if not p.startswith("/"):
        p = "/" + p
    return "disk:" + p

async def _yadisk_get_href(session: aiohttp.ClientSession, disk_path: str, token: str, timeout=30, retries=5):
    CLOUD_API = "https://cloud-api.yandex.net/v1/disk"
    url = f"{CLOUD_API}/resources/download"
    params = {"path": disk_path}
    headers = {"Authorization": f"OAuth {token}"}
    attempt = 0; backoff = 0.8
    while True:
        attempt += 1
        r = await session.get(url, headers=headers, params=params, timeout=timeout)
        try:
            if r.status == 200:
                data = await r.json()
                href = data.get("href")
                if not href:
                    raise RuntimeError("Yandex Disk /resources/download returned no href")
                return href
            if r.status in (429, 500, 502, 503, 504):
                ra = r.headers.get("Retry-After")
                try:
                    delay = float(ra) if ra and ra.strip().isdigit() else min(8.0, backoff * attempt)
                except Exception:
                    delay = min(8.0, backoff * attempt)
                if attempt < retries:
                    await asyncio.sleep(delay); continue
            body = (await r.text())[:500]
            from aiohttp import ClientResponseError
            raise ClientResponseError(r.request_info, r.history, status=r.status, message=body, headers=r.headers)
        finally:
            await r.release()

async def yadisk_download_cloud(session: aiohttp.ClientSession, remote_path: str, dst: Path, timeout=300):
    token = os.environ.get("YADISK_OAUTH_TOKEN") or os.environ.get("YANDEX_DISK_OAUTH")
    if not token:
        raise RuntimeError("YADISK_OAUTH_TOKEN is not set")
    disk_path = _normalize_disk_path(remote_path)
    href = await _yadisk_get_href(session, disk_path, token, timeout=30, retries=5)
    async with session.get(href, timeout=timeout) as r:
        r.raise_for_status()
        with dst.open("wb") as f:
            async for chunk in r.content.iter_chunked(1 << 20):
                f.write(chunk)
    return dst
