import urllib.request
import json
try:
    req = urllib.request.Request('http://127.0.0.1:8000/api/monitoring', headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=5) as response:
        data = json.loads(response.read().decode())
        print(json.dumps(data.get('llm_provider', {}), indent=2))
except Exception as e:
    print(f'ERROR: {e}')
