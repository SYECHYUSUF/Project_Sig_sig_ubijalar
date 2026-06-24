import urllib.request
import json

try:
    r = urllib.request.urlopen('http://127.0.0.1:8000/recommend', timeout=120)
    data = json.loads(r.read())
    feats = data.get('features', [])
    print(f"Status: OK")
    print(f"Features count: {len(feats)}")
    if feats:
        print(f"Sample: {json.dumps(feats[0]['properties'], indent=2, ensure_ascii=False)}")
    else:
        print("NO features returned - empty result")
except Exception as e:
    print(f"ERROR: {e}")
