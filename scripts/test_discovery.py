"""Standalone test: Phase 0 discovery against news.ycombinator.com.

Run: python -m scripts.test_discovery
Or:  python scripts/test_discovery.py
"""

import asyncio
import sys
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))


async def main():
    from src.discovery.engine import discover

    domain = "news.ycombinator.com"
    requirement = "find popular tech stories and discussions"

    print(f"\n{'='*60}")
    print(f"Phase 0 Discovery Test")
    print(f"Domain:      {domain}")
    print(f"Requirement: {requirement}")
    print(f"{'='*60}\n")

    site_intel = await discover(domain, requirement)

    print(f"Entry points ({len(site_intel.entry_points)}):")
    for ep in site_intel.entry_points[:10]:
        print(f"  [{ep.source:8s}] score={ep.score:.3f}  {ep.url}")

    print(f"\nDirect content ({len(site_intel.direct_content)}):")
    for dc in site_intel.direct_content[:5]:
        print(f"  [{dc.source:8s}] score={dc.score:.3f}  {dc.url}")

    print(f"\nLive endpoints: {site_intel.live_endpoints}")
    print(f"Sitemap sample: {site_intel.sitemap_sample[:5]}")
    print(f"Robots.txt: {'yes (' + str(len(site_intel.robots_txt)) + ' chars)' if site_intel.robots_txt else 'blocked/missing'}")


if __name__ == "__main__":
    asyncio.run(main())
