"""
Camoufox remote server — exposes a Playwright-compatible WebSocket endpoint.

The agent container connects via:
    BROWSER_WS_URL=ws://camoufox:1234/ws

Camoufox uses a custom Firefox build with C++-level fingerprint injection,
making automation undetectable to Cloudflare and other anti-bot systems.
"""
import os
from camoufox.server import launch_server

proxy_url = os.environ.get("PROXY_URL", "")
proxy = {"server": proxy_url} if proxy_url else None

port = int(os.environ.get("CAMOUFOX_PORT", "1234"))
ws_path = os.environ.get("CAMOUFOX_WS_PATH", "ws")

print(f"Starting Camoufox server on ws://0.0.0.0:{port}/{ws_path}")
if proxy:
    print(f"Using proxy: {proxy_url.split('@')[-1]}")  # log host only, not credentials
else:
    print("No proxy configured (direct connection)")

launch_server(
    headless="virtual",   # Xvfb virtual display — not flagged as headless
    geoip=bool(proxy),    # match timezone/locale to proxy IP when proxy is set
    proxy=proxy,
    disable_coop=True,    # allow clicking CF Turnstile checkbox in cross-origin iframes
    humanize=True,        # human-like mouse movement
    port=port,
    ws_path=ws_path,
)
