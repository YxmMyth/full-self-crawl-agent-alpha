"""
Camoufox remote server — exposes a Playwright-compatible WebSocket endpoint.

The agent container connects via:
    BROWSER_WS_URL=ws://camoufox:1234/ws

Camoufox uses a custom Firefox build with C++-level fingerprint injection,
making automation undetectable to Cloudflare and other anti-bot systems.
"""
import base64
import os
import subprocess
from pathlib import Path

import orjson
from camoufox.server import LAUNCH_SCRIPT, get_nodejs, launch_options, to_camel_case_dict

proxy_url = os.environ.get("PROXY_URL", "")
proxy = {"server": proxy_url} if proxy_url else None

port = int(os.environ.get("CAMOUFOX_PORT", "1234"))
ws_path = os.environ.get("CAMOUFOX_WS_PATH", "ws")

print(f"Starting Camoufox server on ws://0.0.0.0:{port}/{ws_path}")
if proxy:
    print(f"Using proxy: {proxy_url.split('@')[-1]}")  # log host only, not credentials
else:
    print("No proxy configured (direct connection)")

kwargs = dict(
    headless=True,        # True = pure headless mode in Docker
    disable_coop=True,    # allow clicking CF Turnstile checkbox in cross-origin iframes
    i_know_what_im_doing=True,  # suppress LeakWarning for disable_coop
    humanize=True,        # human-like mouse movement
    port=port,
    ws_path=ws_path,
)
if proxy:
    kwargs["proxy"] = proxy

config = launch_options(**kwargs)
# Filter out None values — camoufox always includes proxy:None which Firefox rejects
config = {k: v for k, v in config.items() if v is not None}

nodejs = get_nodejs()
data = orjson.dumps(to_camel_case_dict(config))
process = subprocess.Popen(  # nosec
    [nodejs, str(LAUNCH_SCRIPT)],
    cwd=Path(nodejs).parent / "package",
    stdin=subprocess.PIPE,
    text=True,
)
if process.stdin:
    process.stdin.write(base64.b64encode(data).decode())
    process.stdin.close()
process.wait()
raise RuntimeError("Camoufox server process terminated unexpectedly")
