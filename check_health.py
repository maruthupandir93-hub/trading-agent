import urllib.request
import json
try:
    req = urllib.request.Request('http://127.0.0.1:8000/api/monitoring', headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=5) as response:
        print(f"Status: {response.getcode()}")
except Exception as e:
    print(f'ERROR: {e}')
