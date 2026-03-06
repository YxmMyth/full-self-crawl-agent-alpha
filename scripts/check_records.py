"""Check extracted records quality."""
import json

records = [json.loads(l) for l in open('/workspace/artifacts/data/records.jsonl')]
print(f'Total records: {len(records)}')
for i, r in enumerate(records):
    js = r.get('js_code', '') or ''
    title = (r.get('title') or 'N/A')[:35]
    js_ok = js not in ('', 'N/A', 'null', None)
    js_len = len(js) if js_ok else 0
    print(f'  {i+1}. title={title!r} js_len={js_len} ok={js_ok}')
