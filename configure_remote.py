"""Import the user's own credential JSON without printing its contents."""
import argparse
import getpass
import json
from pathlib import Path
from remote import credential_config, save_credentials

parser = argparse.ArgumentParser(description='配置乐健远端凭据；内容仅保存到本机 .local 目录')
parser.add_argument('--input', type=Path, help='已有凭据 JSON 文件')
args = parser.parse_args()
try:
    raw = args.input.read_text('utf-8-sig') if args.input else getpass.getpass('粘贴单行凭据 JSON（不回显）：')
    auth = credential_config(json.loads(raw))
except (ValueError, OSError):
    raise SystemExit('无法读取有效凭据 JSON；支持 accessToken/refreshToken 或 Authorization: Bearer …，Cookie 可选') from None
local = Path(__file__).resolve().parent/'.local'
local.mkdir(exist_ok=True)
save_credentials(local/'credentials.private.json', auth)
print('已配置本机凭据文件。网页中点击“使用本机凭据文件”连接。')
