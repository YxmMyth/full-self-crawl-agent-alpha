"""Quick script to check CodePen robots.txt and sitemap via Camoufox."""
import asyncio
import sys
sys.path.insert(0, '/app')

async def check():
    from src.tools.browser import BrowserTool
    b = BrowserTool(check_browsers=False)
    await b.start()

    print("=== robots.txt ===")
    await b.navigate('https://codepen.io/robots.txt', wait_until='load')
    html = await b.get_html()
    print(html[:3000])

    print("\n=== sitemap.xml (first 2000 chars) ===")
    result = await b.navigate('https://codepen.io/sitemap.xml', wait_until='load')
    html2 = await b.get_html()
    print(html2[:3000])

    print("\n=== sitemap_index (try) ===")
    result = await b.navigate('https://codepen.io/sitemap_index.xml', wait_until='load')
    html3 = await b.get_html()
    print(html3[:2000])

    await b.close()

asyncio.run(check())
