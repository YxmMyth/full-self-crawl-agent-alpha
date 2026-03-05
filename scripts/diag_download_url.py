"""Diagnostic: test download_url on a known Three.js CodePen pen."""
import asyncio, sys, os
sys.path.insert(0, '/app')
from src.tools.browser import BrowserTool

async def main():
    b = BrowserTool(check_browsers=False)
    await b.start()
    r = await b.navigate('https://codepen.io/ste-vg/pen/GRooLza', wait_until='load')
    print('Navigated:', r.get('title'), r.get('url'))
    slug = r['url'].rstrip('/').split('/')[-1]
    print('Slug:', slug)
    export_url = f'https://codepen.io/cpe/pen/export/{slug}'
    print('Downloading:', export_url)
    result = await b.download_url(export_url)
    print('Result:', result)
    await b.stop()

asyncio.run(main())
