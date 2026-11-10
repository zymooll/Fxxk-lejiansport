import json,urllib.request
from pathlib import Path

root=Path(__file__).resolve().parent/'.local'
try:
    port=json.loads((root/'runtime.json').read_text('utf-8'))['port']
    request=urllib.request.Request(f'http://127.0.0.1:{int(port)}/api/admin/shutdown',data=b'{}',
        headers={'Content-Type':'application/json','X-Local-Control':(root/'control.key').read_text('ascii')},method='POST')
    with urllib.request.urlopen(request,timeout=3) as r:json.load(r)
    print('本地后端已停止。')
except Exception:
    print('服务未运行，或此目录没有对应的运行信息。')
