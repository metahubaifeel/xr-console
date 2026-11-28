"""Live read-only smoke check. No model calls and no robot motion commands."""
import concurrent.futures
import json
import urllib.request
BASE='http://127.0.0.1:8910'
op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def get(path):
    with op.open(BASE+path,timeout=12) as r:return r.status,r.read()
assert json.loads(get('/api/config')[1])['mode']=='live'
assert b'id="testStart"' in get('/')[1]
assert b'id="judgment"' in get('/')[1]
def camera(name):
    code,raw=get('/api/camera/'+name)
    assert code==200 and raw.startswith(b'\xff\xd8') and raw.endswith(b'\xff\xd9')
    return name,len(raw)
with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
    for result in pool.map(camera,['head','left','right','bev1','bev2']):print(result)
status=json.loads(get('/api/runtime')[1]);print('robot client running:',status['running'])
print('PASS: LIVE frontend, five real JPEGs, real client status; no model calls or motion commands')
