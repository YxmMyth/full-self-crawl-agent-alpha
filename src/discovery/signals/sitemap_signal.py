"""S2: Sitemap signal — robots.txt + sitemap parsing with keyword scoring."""

import logging
import re
from urllib.parse import urlparse

logger = logging.getLogger("discovery.sitemap_signal")


async def sitemap_signal(domain: str, requirement: str, scheme: str = "https") -> dict:
    """Fetch robots.txt and parse sitemap for candidate URLs.

    Returns:
        {
            "robots_txt": str,
            "candidates": list[{url, score}],  # keyword-scored
            "sitemap_found": bool,
        }
    Non-fatal: returns empty result on any error.
    """
    base = f"{scheme}://{domain}"
    result = {"robots_txt": "", "candidates": [], "sitemap_found": False}

    # Extract keywords from requirement for scoring
    keywords = [w.lower() for w in re.split(r"\W+", requirement) if len(w) > 3]

    try:
        import httpx
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
        }
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            # 1. robots.txt
            r = await client.get(f"{base}/robots.txt", headers=headers)
            robots_txt = r.text if r.status_code == 200 else ""
            if robots_txt and robots_txt.strip().startswith("<"):
                robots_txt = ""
            result["robots_txt"] = robots_txt

            # 2. Parse sitemap URLs from robots.txt
            sitemap_urls = re.findall(
                r"^Sitemap:\s*(.+)$", robots_txt, re.MULTILINE | re.IGNORECASE
            )

            # Also try /sitemap.xml directly
            if not sitemap_urls:
                sitemap_urls = [f"{base}/sitemap.xml"]

            all_locs: list[str] = []
            for smap_url in sitemap_urls[:3]:
                smap_url = smap_url.strip()
                try:
                    sr = await client.get(smap_url, headers=headers)
                    if sr.status_code != 200:
                        continue
                    result["sitemap_found"] = True
                    locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", sr.text, re.DOTALL)
                    if "<sitemapindex" in sr.text:
                        # Nested sitemap index
                        for nested in locs[:5]:
                            try:
                                nr = await client.get(nested.strip(), headers=headers)
                                if nr.status_code == 200:
                                    all_locs.extend(
                                        re.findall(r"<loc>\s*(.*?)\s*</loc>", nr.text, re.DOTALL)
                                    )
                            except Exception:
                                pass
                    else:
                        all_locs.extend(locs)
                except Exception:
                    pass

            # 3. Score candidates by keyword match in URL
            base_netloc = domain.lstrip("www.")
            candidates = []
            for loc in all_locs[:500]:
                loc = loc.strip()
                try:
                    lp = urlparse(loc)
                    netloc = lp.netloc.lstrip("www.")
                    if base_netloc not in netloc:
                        continue
                    url_lower = loc.lower()
                    kw_hits = sum(1 for kw in keywords if kw in url_lower)
                    score = kw_hits / max(len(keywords), 1)
                    candidates.append({"url": loc, "score": score})
                except Exception:
                    pass

            # Sort by score, keep top 200
            candidates.sort(key=lambda x: x["score"], reverse=True)
            result["candidates"] = candidates[:200]

    except Exception as e:
        logger.debug(f"Sitemap signal error (non-fatal): {e}")

    logger.info(
        f"Sitemap signal: robots={'yes' if result['robots_txt'] else 'blocked'}, "
        f"sitemap_found={result['sitemap_found']}, candidates={len(result['candidates'])}"
    )
    return result
