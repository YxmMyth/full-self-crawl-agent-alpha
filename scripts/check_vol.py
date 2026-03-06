import json
recs = [json.loads(l) for l in open('/workspace/artifacts/data/records.jsonl')]
js_ok = sum(1 for r in recs if (r.get('js_code') or '') not in ('', 'N/A', 'null'))
print(f'total={len(recs)} with_js={js_ok}')
for i, r in enumerate(recs):
    js = r.get('js_code') or ''
    ok = js not in ('', 'N/A', 'null')
    title = (r.get('title') or 'N/A')[:40]
    print(f'  {i+1}. {title!r} js_len={len(js)} ok={ok}')
