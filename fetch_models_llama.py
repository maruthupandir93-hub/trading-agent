import urllib.request
import json
import os

key = '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'
req = urllib.request.Request('https://integrate.api.nvidia.com/v1/models', headers={'Authorization': f'Bearer {key}', 'Accept': 'application/json'})
try:
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode())
        models = [m['id'] for m in data.get('data', [])]
        llama_models = [m for m in models if 'llama' in m.lower()]
        print("Llama models:", llama_models)
except Exception as e:
    print(f"Error fetching models: {e}")
