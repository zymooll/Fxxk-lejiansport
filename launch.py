import argparse,hashlib,json,subprocess,sys,time,urllib.request,webbrowser
from pathlib import Path

root=Path(__file__).resolve().parent
parser=argparse.ArgumentParser();parser.add_argument('--no-browser',action='store_true');args=parser.parse_args()
url='http://127.0.0.1:18766'
def healthy():
    try:
        with urllib.request.urlopen(url+'/api/health',timeout=.5) as r:
            data=json.load(r)['data']
            return data.get('service')=='run-mvp-accounts-v3' and data.get('instance')==hashlib.sha256(str(root).encode()).hexdigest()[:12]
    except Exception:return False
if not healthy():
    local=root/'.local';local.mkdir(exist_ok=True)
    with (local/'server.log').open('ab',buffering=0) as log:
        process=subprocess.Popen([sys.executable,str(root/'server_v3.py')],cwd=root,stdin=subprocess.DEVNULL,
          stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    for _ in range(30):
        if healthy():break
        if process.poll() is not None:raise SystemExit('服务未启动；请查看 .local/server.log 或检查18766端口。')
        time.sleep(.2)
    else:raise SystemExit('服务启动超时。')
if not args.no_browser:webbrowser.open(url)
print('账号池与轨迹工作台已启动：'+url)
