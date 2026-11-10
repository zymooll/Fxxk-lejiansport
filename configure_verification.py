"""Save only a salted local verifier, never the supplied authentication values."""
import argparse,getpass,json
from pathlib import Path
from app import BASE,credential_verifier

if __name__=='__main__':
    p=argparse.ArgumentParser(description='配置仅用于本地比对的鉴权摘要。')
    p.add_argument('--input',type=Path,help='用户自己的验证 JSON 文件；不会复制该文件')
    args=p.parse_args()
    try:
        data=json.loads(args.input.read_text('utf-8-sig') if args.input else getpass.getpass('粘贴单行鉴权JSON（输入不回显）：'))
        verifier=credential_verifier(data)
        target=BASE/'.local';target.mkdir(exist_ok=True)
        (target/'verification.json').write_text(json.dumps(verifier),'utf-8')
        print('已保存加盐验证摘要。请重启本地服务；未验证或访问生产服务器。')
    except Exception:
        print('配置失败：请检查 JSON 格式及 Authorization / Cookie 字段。')
        raise SystemExit(1)
