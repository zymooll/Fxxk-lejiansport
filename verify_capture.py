"""Offline replay of signing only. This script never makes a network request."""
import argparse
import json
from pathlib import Path
from signing import sign

parser = argparse.ArgumentParser(description='离线复算抓包签名，不向服务器发送任何内容')
parser.add_argument('events', type=Path, help='完整跑步会话的 http-events.redacted.jsonl')
args = parser.parse_args()
rows = [json.loads(line) for line in args.events.read_text('utf-8-sig').splitlines() if line.strip()]
body = next(row['requestBody'] for row in rows if '/running/app/v3/upload' in row.get('url', ''))
user_id = next(row['requestBody']['userId'] for row in rows if 'userId' in row.get('requestBody', {}))
school_id = next(row['responseBody']['data']['organizationId'] for row in rows if '/semester/getCurrent' in row.get('url', ''))
generated = sign(body, user_id, school_id)
result = {field+'Exact': generated[field] == body[field] for field in ('signDigital', 'signTime', 'oct')}
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(0 if all(result.values()) else 1)
