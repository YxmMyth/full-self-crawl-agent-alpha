"""S3: Probe signal — HTTP HEAD checks against common discovery endpoints."""

import asyncio
import logging

logger = logging.getLogger("discovery.probe_signal")

# Common paths that indicate navigable content sections
COMMON_PATHS = [
    "/search",
    "/topics",
    "/topic",
    "/tags",
    "/tag",
    "/categories",
    "/category",
    "/explore",
    "/browse",
    "/trending",
    "/popular",
    "/new",
    "/top",
    "/latest",
    "/feed",
    "/api",
    "/sitemap",
    "/directory",
    "/index",
    "/collections",
    "/library",
]


async def probe_signal(domain: str, scheme: str = "https") -> list[str]:
    """HTTP HEAD probe against common endpoint paths.

    Returns list of confirmed-live paths (status 200/301/302).
    Non-fatal: returns [] on any error.
    Fast: parallel HEAD requests, ~200ms total.
    """
    import httpx

    base = f"{scheme}://{domain}"
    live_paths: list[str] = []

    async def _check(client: httpx.AsyncClient, path: str) -> str | None:
        try:
            r = await client.head(f"{base}{path}", timeout=5)
            if r.status_code in (200, 301, 302, 307, 308):
                return path
        except Exception:
            pass
        return None

    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0 (compatible; SiteProbe/1.0)"},
        ) as client:
            tasks = [_check(client, p) for p in COMMON_PATHS]
            results = await asyncio.gather(*tasks)
            live_paths = [r for r in results if r is not None]
    except Exception as e:
        logger.debug(f"Probe signal error (non-fatal): {e}")

    logger.info(f"Probe signal: {len(live_paths)} live endpoints on {domain}")
    return live_paths
