import urllib.request
import json
try:
    req = urllib.request.Request('http://127.0.0.1:8000/api/ai/reason', headers={'User-Agent': 'Mozilla/5.0'}, method='POST')
    with urllib.request.urlopen(req, data=b'{}', timeout=5) as response:
        print(response.read().decode())
except urllib.error.HTTPError as e:
    print(f'HTTP ERROR: {e.code} - {e.read().decode()}')
except Exception as e:
    print(f'ERROR: {e}')
