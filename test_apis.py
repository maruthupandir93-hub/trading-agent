import urllib.request
import json

endpoints = [
    '/api/monitoring',
    '/api/marketdata/upstream-health',
    '/api/admin/status',
    '/api/dashboard/events',
    '/api/catalog/strategies'
]

for ep in endpoints:
    url = f'http://127.0.0.1:8000{ep}'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())
            print(f'SUCCESS: {ep} -> {str(data)[:100]}...')
    except Exception as e:
        print(f'ERROR: {ep} -> {e}')
