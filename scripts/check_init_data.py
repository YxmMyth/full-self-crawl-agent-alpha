"""Check if #init-data exists on a CodePen pen page and if the extraction skill works."""
import asyncio
import sys
sys.path.insert(0, '/app')
from src.tools.browser import BrowserTool


async def main():
    b = BrowserTool(check_browsers=False)
    await b.start()
    r = await b.navigate('https://codepen.io/Yakudoo/pen/YXxmYR')
    print('navigate strategy:', r.get('strategy'), 'elems:', r.get('element_count'))

    has_init = await b.page.evaluate("document.getElementById('init-data') !== null")
    print('has #init-data:', has_init)

    skill_result = await b.page.evaluate(
        "() => { try {"
        "  const raw = document.getElementById('init-data')?.value;"
        "  if (!raw) return {error: 'no init-data element'};"
        "  const data = JSON.parse(raw);"
        "  const item = JSON.parse(data.__item || '{}');"
        "  return { title: item.title || 'MISSING', js_len: (item.js||'').length, has_js: !!item.js };"
        "} catch(e) { return {error: e.message}; } }"
    )
    print('skill result:', skill_result)

    # Also try window.__INIT_DATA__
    win_data = await b.page.evaluate(
        "() => { try { const d = window.__INIT_DATA__; return d ? {exists: true, keys: Object.keys(d)} : {exists: false}; } catch(e) { return {error: e.message}; } }"
    )
    print('window.__INIT_DATA__:', win_data)

    await b.close()


asyncio.run(main())
