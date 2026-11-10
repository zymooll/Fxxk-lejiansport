"""Single-attempt login, App-compatible headers, and credential-free diagnostics."""
import gzip
import io
import json
import re
import ssl
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from login_protocol import LOGIN_URL, build_login_request, extract_token_pair
from remote import NoRedirect, RemoteError, credential_config

# Both successful 2026-11-06 and 2026-11-08 captures use this header profile.
LOGIN_HEADERS = {'Content-Type': 'application/json', 'Charset': 'UTF-8',
                 'User-Agent': 'okhttp/4.8.1', 'Accept-Encoding': 'gzip'}
MAX_RESPONSE = 2_000_000


class LoginError(RemoteError):
    def __init__(self, code, message, *, phase, diagnostic_id, http_status=None, business_code=None):
        super().__init__(code, message+'（诊断编号：'+diagnostic_id+'）',
                         http_status=http_status, business_code=business_code)
        self.phase, self.diagnostic_id = phase, diagnostic_id

    def public(self):
        return dict(super().public(), phase=self.phase, diagnosticId=self.diagnostic_id)


def safe_message(value, phone, password):
    if not isinstance(value, str) or not value.strip():
        return '服务器未提供具体原因'
    # Do not turn a server echo into a password, phone, or token disclosure.
    for secret in sorted({phone, phone.strip(), password}, key=len, reverse=True):
        if secret: value = value.replace(secret, '[已隐藏]')
    value = re.sub(r'(?i)Bearer\s+[^\s,;"<>]+', 'Bearer [已隐藏]', value)
    value = re.sub(r'(?i)(accessToken|refreshToken|password|JSESSIONID)\s*[:=]\s*[^\s,;<>]+', r'\1=[已隐藏]', value)
    value = re.sub(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b', '[已隐藏]', value)
    value = re.sub(r'\b1\d{10}\b', '[已隐藏]', value)
    value = re.sub(r'\beyJ[\w-]+\.[\w-]+\.[\w-]+\b', '[已隐藏]', value)
    return value[:240]


def password_login(phone, password, transport=None, *, diagnostic_file=None):
    if not isinstance(phone, str) or not phone.strip() or len(phone) > 128 or not isinstance(password, str) or not password or len(password) > 1024:
        raise ValueError('请输入有效账号与密码')
    phone = phone.strip()
    envelope = build_login_request(phone, password)
    body = json.dumps(envelope, separators=(',', ':')).encode()
    diagnostic_id = uuid.uuid4().hex[:12]
    started = time.monotonic()
    status = None
    report = dict(at=datetime.now(timezone.utc).isoformat(), diagnosticId=diagnostic_id,
                  endpoint='/authorization/user/v2/manage/login', requestTimestamp=envelope['t'],
                  headerProfile='okhttp-4.8.1-capture', authorizationSent=False, cookieSent=False)

    def failure(code, message, phase, business_code=None):
        return LoginError(code, message, phase=phase, diagnostic_id=diagnostic_id,
                          http_status=status, business_code=business_code)

    try:
        if transport:
            status, raw = transport('POST', LOGIN_URL, LOGIN_HEADERS.copy(), body)
        else:
            opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
            request = urllib.request.Request(LOGIN_URL, data=body, headers=LOGIN_HEADERS, method='POST')
            try:
                with opener.open(request, timeout=15) as response:
                    status, raw = response.status, response.read(MAX_RESPONSE+1)
            except urllib.error.HTTPError as error:
                status, raw = error.code, error.read(MAX_RESPONSE+1)
        report['httpStatus'] = status
        if len(raw) > MAX_RESPONSE:
            raise failure('LOGIN_BAD_RESPONSE', '登录响应超出限制', 'format')
        if raw.startswith(b'\x1f\x8b'):
            report['responseGzip'] = True
            try:
                with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
                    raw = compressed.read(MAX_RESPONSE+1)
            except (OSError, EOFError):
                raise failure('LOGIN_BAD_RESPONSE', '登录响应的 gzip 数据损坏', 'format') from None
            if len(raw) > MAX_RESPONSE:
                raise failure('LOGIN_BAD_RESPONSE', '登录响应解压后超出限制', 'format')
        if 300 <= status < 400:
            raise failure('LOGIN_REDIRECT_BLOCKED', '登录服务返回重定向，已停止转发凭据', 'http')
        if status == 429:
            raise failure('LOGIN_RATE_LIMIT', '登录请求被服务器限流，请稍后手动重试', 'http')
        if status >= 500:
            raise failure('LOGIN_HTTP_ERROR', f'登录服务暂时异常（HTTP {status}），请稍后重试', 'http')
        try: response = json.loads(raw)
        except (ValueError, UnicodeError):
            raise failure('LOGIN_BAD_RESPONSE', f'登录接口未返回有效 JSON（HTTP {status}）', 'format') from None
        if not isinstance(response, dict):
            raise failure('LOGIN_BAD_RESPONSE', f'登录响应格式异常（HTTP {status}）', 'format')
        report['responseFields'] = [key for key in ('code','message','data','error','error_description') if key in response]
        code = response.get('code')
        safe_code = code if type(code) is int and abs(code) < 1_000_000_000 and str(code) not in (phone,password) else None
        if status != 200:
            message = safe_message(response.get('message') or response.get('error_description') or response.get('error'), phone, password)
            raise failure('LOGIN_REJECTED', f'登录失败（HTTP {status}）：{message}', 'http', safe_code)
        if type(code) is not int:
            raise failure('LOGIN_BAD_RESPONSE', '登录响应缺少正确格式的业务码，暂不能判断账号密码是否正确', 'format')
        report['businessCode'] = safe_code
        if code != 0:
            message = safe_message(response.get('message') or response.get('error_description'), phone, password)
            raise failure('LOGIN_REJECTED', f'登录失败（业务码 {safe_code if safe_code is not None else "未识别"}）：{message}', 'business', safe_code)
        try: config = credential_config(extract_token_pair(response))
        except (ValueError, KeyError, TypeError, UnicodeError, AttributeError):
            raise failure('LOGIN_BAD_RESPONSE', '服务器返回成功状态，但登录数据解包失败，请检查协议兼容性', 'decode', 0) from None
        report.update(outcome='accepted', phase='decode', tokenPairRecovered=True)
        return config
    except LoginError as error:
        report.update(outcome='rejected', error=error.public())
        raise
    except (urllib.error.URLError, TimeoutError, OSError, EOFError):
        error = failure('LOGIN_NETWORK_ERROR', '登录连接或响应读取失败，请稍后手动重试', 'network')
        report.update(outcome='failed', error=error.public())
        raise error from None
    finally:
        report['durationMs'] = round((time.monotonic()-started)*1000)
        if diagnostic_file is not None:
            try:
                path = Path(diagnostic_file)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('a', encoding='utf-8') as output:
                    output.write(json.dumps(report, ensure_ascii=False)+'\n')
            except OSError: pass
