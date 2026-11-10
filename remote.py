"""TLS client for the captured LeGym API. Credentials go only to the fixed TLS origin."""
import base64
import hashlib
import json
import math
import os
import re
import socket
import ssl
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlencode
from credential_store import checkpoint, credential_lock, record_refresh_event, dpapi

UPSTREAM = 'https://cpes.legym.cn'
READ_ENDPOINTS = {
    '/authorization/user/getBasicInfo': 'POST',
    '/education/semester/getCurrent': 'GET',
    '/running/app/getRunningLimit': 'POST',
    '/running/app/getTotalRunning': 'POST',
    '/running/app/getRunningRange': 'POST',
    '/running/app/getDetails': 'POST',
    '/education/course/today': 'GET',
}
UPLOAD_ENDPOINT = '/running/app/v3/upload'
REFRESH_ENDPOINT = '/authorization/user/refreshToken'


class RemoteError(Exception):
    def __init__(self, code, message, *, http_status=None, business_code=None, uncertain=False):
        super().__init__(message)
        self.code, self.message = code, message
        self.http_status, self.business_code, self.uncertain = http_status, business_code, uncertain

    def public(self):
        return dict(code=self.code, message=self.message, httpStatus=self.http_status,
                    businessCode=self.business_code, outcomeUncertain=self.uncertain)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        # Never forward a bearer credential through a redirect.
        return None


def credential_config(value):
    if not isinstance(value, dict):
        raise ValueError('凭据必须是 JSON 对象')
    if isinstance(value.get('headers'), dict):
        value = {**value, **value['headers']}
    lowered = {str(k).lower(): v for k, v in value.items()}
    access = lowered.get('accesstoken')
    refresh = lowered.get('refreshtoken')
    for token in (access, refresh):
        if token is not None and (not isinstance(token, str) or not token or len(token) > 8192
                                  or any(c.isspace() or c == '\x00' for c in token)):
            raise ValueError('Token 格式错误')
    auth = lowered.get('authorization', 'Bearer '+access if access else '')
    cookie = lowered.get('cookie')
    if not isinstance(auth, str) or not auth.startswith('Bearer ') or not auth[7:].strip():
        raise ValueError('缺少 Bearer 凭据')
    if len(auth) > 8192 or any(c in auth for c in '\r\n\x00'):
        raise ValueError('凭据不能包含换行或空字符')
    if access and auth.strip() != 'Bearer '+access:
        raise ValueError('Authorization 与 accessToken 不一致')
    result = {'Authorization': auth.strip()}
    if cookie is not None:
        if not isinstance(cookie, str) or len(cookie) > 8192 or any(c in cookie for c in '\r\n\x00'):
            raise ValueError('Cookie 格式错误')
        parsed = SimpleCookie()
        parsed.load(cookie)
        if 'JSESSIONID' not in parsed or not parsed['JSESSIONID'].value:
            raise ValueError('缺少 JSESSIONID Cookie')
        result['Cookie'] = 'JSESSIONID='+parsed['JSESSIONID'].coded_value
    org = lowered.get('organization')
    if org:
        if not isinstance(org, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', org):
            raise ValueError('Organization 格式错误')
        result['Organization'] = org
    if refresh is not None:
        result['refreshToken'] = refresh
    expires = lowered.get('accesstokenexpires')
    if expires is not None:
        if isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires) or expires <= 0:
            raise ValueError('Token 到期时间格式错误')
        result['accessTokenExpires'] = expires
    token_type = lowered.get('tokentype')
    if token_type is not None:
        if not isinstance(token_type, str) or token_type.lower() != 'bearer':
            raise ValueError('不支持的 Token 类型')
        result['tokenType'] = token_type
    return result


def credentials(value):
    # Only HTTP headers are returned; refreshToken is never an HTTP header.
    return {key: value for key, value in credential_config(value).items() if key in ('Authorization', 'Cookie', 'Organization')}


def save_credentials(path, value, *, encrypted=False):
    config = credential_config(value)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    plain = json.dumps(config, ensure_ascii=False).encode('utf-8')
    encoded = dpapi(plain) if encrypted else plain
    if encrypted and dpapi(encoded, decrypt=True) != plain:
        raise OSError('Credential encryption verification failed')
    descriptor, temporary = tempfile.mkstemp(prefix='.credentials-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class RemoteClient:
    def __init__(self, auth, transport=None):
        config = credential_config(auth)
        self._config = config
        self._headers = credentials(config)
        self._refresh_token = config.get('refreshToken')
        self._credential_file = None
        self._encrypted_credentials = False
        self._refresh_lock = threading.RLock()
        self._refresh_error = None
        self._failure_revision = None
        self._last_refresh_validation = None
        self.owner = None
        self._transport = transport
        self._opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        self.snapshot = None
        self.profile = None
        self.calls = []

    def bind_credentials_file(self, path, *, encrypted=False):
        self._credential_file = Path(path)
        self._encrypted_credentials = encrypted
        self._refresh_lock = credential_lock(path)

    def _message(self, value):
        if not isinstance(value, str):
            return '远端服务拒绝了请求'
        for secret in (self._headers['Authorization'], self._headers['Authorization'][7:],
                       self._headers.get('Cookie', '').split('=', 1)[-1], self._refresh_token):
            if secret:
                value = value.replace(secret, '[凭据已隐藏]')
        return value[:240]

    def request(self, path, payload=None, *, write=False):
        request_revision = self._config
        try:
            return self._request_once(path, payload, write=write)
        except RemoteError as error:
            if write or error.code != 'UPSTREAM_AUTH_REJECTED' or not (self._refresh_token or self._credential_file):
                raise
        # Retry reads once after refreshing. A write is never automatically resent.
        self.refresh_credentials(failed_revision=request_revision)
        if path == '/education/semester/getCurrent' and payload is None:
            return self._last_refresh_validation
        try:
            return self._request_once(path, payload)
        except RemoteError as error:
            if error.code == 'UPSTREAM_AUTH_REJECTED':
                self._refresh_error, self._failure_revision = error, self._config
            raise

    def _saved_config(self):
        if self._credential_file is None:
            return self._config
        try:
            raw = self._credential_file.read_bytes()
            if self._encrypted_credentials:
                raw = dpapi(raw, decrypt=True)
            return credential_config(json.loads(raw.decode('utf-8-sig')))
        except (OSError, ValueError):
            raise RemoteError('LOCAL_CREDENTIAL_FILE_ERROR', '无法读取本机凭据文件，请检查配置') from None

    def _adopt_saved_credentials(self, config):
        # A different account must never inherit the current account's history/session.
        candidate = RemoteClient(config, transport=self._transport)
        candidate.bind_credentials_file(self._credential_file, encrypted=self._encrypted_credentials)
        candidate.owner = self.owner
        semester, _ = candidate._identify()
        self._headers, self._config = candidate._headers, candidate._config
        self._refresh_token = candidate._refresh_token
        self.semester, self.owner = semester, candidate.owner
        self.calls = (self.calls+candidate.calls)[-30:]
        self._last_refresh_validation = semester

    def refresh_credentials(self, failed_revision=None):
        with self._refresh_lock:
            revision = self._saved_config()
            if self._refresh_error is not None and revision == self._failure_revision:
                raise self._refresh_error
            try:
                if failed_revision is not None and failed_revision != self._config and revision == self._config:
                    return {'refreshed': False, 'updatedByAnotherRequest': True}
                if revision != self._config:
                    self._adopt_saved_credentials(revision)
                    self._refresh_error = None
                    if self._credential_file:
                        record_refresh_event(self._credential_file, 'reloaded-from-file')
                    return {'refreshed': False, 'reloadedFromFile': True}
                result = self._rotate_credentials()
                self._refresh_error = None
                if self._credential_file:
                    record_refresh_event(self._credential_file, 'refreshed-and-verified')
                return result
            except RemoteError as error:
                self._refresh_error = error
                self._failure_revision = self._saved_config()
                if self._credential_file:
                    record_refresh_event(self._credential_file, 'failed', error.code, error.http_status)
                raise

    def _rotate_credentials(self):
        if not self._refresh_token:
            raise RemoteError('UPSTREAM_AUTH_REJECTED', '未配置 refreshToken，请更新凭据后重新连接')
        old_bytes = None
        if self._credential_file is not None:
            try:
                old_bytes = self._credential_file.read_bytes()
                plain = dpapi(old_bytes, decrypt=True) if self._encrypted_credentials else old_bytes
                revision = credential_config(json.loads(plain.decode('utf-8-sig')))
                if revision != self._config:
                    self._adopt_saved_credentials(revision)
                    return {'refreshed': False, 'reloadedFromFile': True}
                checkpoint(self._credential_file, plain, 'before')
            except (OSError, ValueError):
                raise RemoteError('LOCAL_CREDENTIAL_BACKUP_FAILED', '无法创建加密凭据备份，未请求刷新') from None
        data = self._request_once(REFRESH_ENDPOINT, refresh=True)
        try:
            if not isinstance(data, dict) or not data.get('accessToken') or not data.get('refreshToken'):
                raise ValueError()
            config = credential_config({**{key: value for key, value in self._headers.items() if key != 'Authorization'},
                                        **{key: data[key] for key in ('accessToken', 'refreshToken', 'tokenType', 'accessTokenExpires') if key in data}})
        except ValueError:
            raise RemoteError('UPSTREAM_BAD_RESPONSE', '远端刷新响应缺少有效的双 Token') from None
        self._headers = credentials(config)
        self._refresh_token = config['refreshToken']
        self._config = config
        if self._credential_file is not None:
            try:
                checkpoint(self._credential_file, json.dumps(config).encode(), 'after')
                if self._credential_file.read_bytes() != old_bytes:
                    raise RemoteError('LOCAL_CREDENTIAL_CHANGED', '本机凭据已被其他程序更新，新凭据保留在加密检查点中')
                if self._encrypted_credentials:
                    save_credentials(self._credential_file, config, encrypted=True)
                else:
                    save_credentials(self._credential_file, config)
            except OSError:
                raise RemoteError('LOCAL_CREDENTIAL_SAVE_FAILED', '凭据已刷新，但无法保存本机凭据文件') from None
        self._last_refresh_validation = self._request_once('/education/semester/getCurrent', exact_status=200)
        if not isinstance(self._last_refresh_validation, dict) or not self._last_refresh_validation.get('id') or not self._last_refresh_validation.get('organizationId'):
            raise RemoteError('UPSTREAM_NO_SEMESTER', '刷新后的凭据验证未返回当前学期或机构')
        return {'refreshed': True}

    def _request_once(self, path, payload=None, *, write=False, refresh=False, exact_status=None):
        if refresh:
            if path != REFRESH_ENDPOINT or write or payload is not None:
                raise ValueError('不支持的刷新接口')
            method = 'GET'
        elif write:
            if path != UPLOAD_ENDPOINT: raise ValueError('不支持的写接口')
            method = 'POST'
        else:
            method = READ_ENDPOINTS.get(path)
            if not method: raise ValueError('不支持的读取接口')
        headers = {**self._headers, 'Accept': 'application/json', 'Content-Type': 'application/json',
                   'User-Agent': 'RunMVP/2.0', 'Accept-Encoding': 'identity'}
        if refresh:
            headers = {key: value for key, value in headers.items() if key in ('Accept', 'User-Agent', 'Accept-Encoding', 'Cookie')}
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode() if payload is not None else None
        if method == 'POST' and data is None: data = b'{}'
        status = None
        try:
            url = UPSTREAM+path
            if refresh:
                url += '?'+urlencode({'refreshToken': self._refresh_token})
            if self._transport:
                status, raw = self._transport(method, url, headers, data)
            else:
                req = urllib.request.Request(url, data=data, headers=headers, method=method)
                try:
                    with self._opener.open(req, timeout=15) as response:
                        status, raw = response.status, response.read(2_000_001)
                except urllib.error.HTTPError as error:
                    status, raw = error.code, error.read(2_000_001)
            # OAuth authentication failures use {error, error_description}, not
            # the application's {code, data} envelope. Honor HTTP status first.
            if status in (401, 403):
                raise RemoteError('UPSTREAM_AUTH_REJECTED',
                                  '远端登录凭据已失效或被拒绝，请更新凭据后重新连接', http_status=status)
            if len(raw) > 2_000_000:
                raise RemoteError('UPSTREAM_RESPONSE_TOO_LARGE', '远端响应超出限制', uncertain=write)
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeError):
                raise RemoteError('UPSTREAM_BAD_RESPONSE', '远端未返回有效 JSON', http_status=status, uncertain=write) from None
            if not isinstance(body, dict) or 'code' not in body:
                raise RemoteError('UPSTREAM_BAD_RESPONSE', '远端响应缺少业务状态', http_status=status, uncertain=write)
            code = body['code']
            # Both HTTP and business status must succeed.
            if str(code) in ('401', '403', '40100', '40101'):
                raise RemoteError('UPSTREAM_AUTH_REJECTED',
                                  '远端登录凭据已失效或被拒绝，请更新凭据后重新连接',
                                  http_status=status, business_code=code)
            if not 200 <= status < 300 or (refresh and status != 200) or (exact_status is not None and status != exact_status) or code != 0 or isinstance(code, bool):
                raise RemoteError('UPSTREAM_REJECTED', self._message(body.get('message')), http_status=status,
                                  business_code=code, uncertain=write and status >= 500)
            self.calls.append(dict(path=path, httpStatus=status, businessCode=code))
            self.calls = self.calls[-30:]
            return body.get('data')
        except RemoteError:
            raise
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            raise RemoteError('UPSTREAM_NETWORK_ERROR', '连接真实服务失败或超时', http_status=status, uncertain=write) from None

    def connect(self):
        semester = self.request('/education/semester/getCurrent')
        if not isinstance(semester, dict) or not semester.get('id') or not semester.get('organizationId'):
            raise RemoteError('UPSTREAM_NO_SEMESTER', '远端未返回当前学期或机构')
        organization = str(semester['organizationId'])
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', organization):
            raise RemoteError('UPSTREAM_BAD_RESPONSE', '远端机构字段无效')
        self._headers['Organization'] = organization
        self.semester = semester
        return semester

    def _identify(self):
        # Derive current identifiers from live responses, never from the old capture.
        semester = self.connect()
        profile = self.request('/authorization/user/getBasicInfo', {})
        if not isinstance(profile, dict) or not isinstance(profile.get('id'), str) or len(profile['id']) < 12:
            raise RemoteError('UPSTREAM_BAD_PROFILE', '远端未返回当前用户标识')
        owner = 'remote:'+hashlib.sha256((profile['id']+'\0'+semester['organizationId']).encode()).hexdigest()
        if self.owner is not None and self.owner != owner:
            raise RemoteError('UPSTREAM_IDENTITY_CHANGED', '远端身份发生变化，请退出后重新连接')
        self.owner = owner
        return semester, profile

    def home(self, location):
        semester, profile = self._identify()
        # The upstream profile also contains tokens: retain only required, noncredential fields.
        self.profile = {k: profile.get(k) for k in ('id', 'schoolId', 'realName', 'nickName', 'schoolName',
                                                 'campusId', 'campusName', 'weight')}
        # getBasicInfo omits schoolId on this installation. The current semester supplies
        # its school organization; this fallback reproduces the provided capture exactly.
        self.profile['schoolId'] = profile.get('schoolId') or semester['organizationId']
        limit = self.request('/running/app/getRunningLimit', {'semesterId': semester['id']})
        if not isinstance(limit, dict):
            raise RemoteError('UPSTREAM_BAD_RESPONSE', '远端跑步规则格式错误')
        total = self.request('/running/app/getTotalRunning', {'hasRule': bool(limit.get('hasRule')), 'semesterId': semester['id']})
        region = None
        if limit.get('hasRule') and limit.get('limitationsGoalsSexInfoId'):
            region = self.request('/running/app/getRunningRange', {
                'latitude': location['latitude'], 'longitude': location['longitude'],
                'semesterId': semester['id'], 'limitationsGoalsSexInfoId': limit['limitationsGoalsSexInfoId'],
                'patternId': limit.get('patternId'), 'scoringType': limit.get('scoringType'),
            })
        courses = self.request('/education/course/today')
        if not isinstance(total, dict) or (region is not None and not isinstance(region, dict)):
            raise RemoteError('UPSTREAM_BAD_RESPONSE', '远端统计或区域格式错误')
        if region is not None:
            for polygons in (region.get('rangeRunningRegion') or [], region.get('invalidRangeRunningRegion') or []):
                if not isinstance(polygons, list): raise RemoteError('UPSTREAM_BAD_RESPONSE', '远端区域格式错误')
                for polygon in polygons:
                    if not isinstance(polygon, list) or len(polygon) < 3: raise RemoteError('UPSTREAM_BAD_RESPONSE', '远端多边形无效')
                    for point in polygon:
                        if not isinstance(point, dict) or not numeric(point.get('latitude')) or not numeric(point.get('longitude')):
                            raise RemoteError('UPSTREAM_BAD_RESPONSE', '远端坐标无效')
        self.snapshot = dict(source=UPSTREAM, fetchedAt=datetime.now(timezone.utc).isoformat(), semester=semester,
                             runningLimit=limit, totals=total, runningRange=region, courses=courses,
                             calls=list(self.calls), upload=dict(path=UPLOAD_ENDPOINT, requiresSignedPayload=True))
        return self.snapshot

    def submit(self, payload):
        record_id = self.request(UPLOAD_ENDPOINT, payload, write=True)
        if not isinstance(record_id, str) or not record_id:
            raise RemoteError('UPSTREAM_MISSING_RECEIPT', '远端未返回记录 ID，提交结果需要核对', uncertain=True)
        return record_id

    def record_status(self, record_id):
        semester = self.semester if hasattr(self, 'semester') else self.connect()
        data = self.request('/running/app/getDetails', {'page': 1, 'size': 20, 'semesterId': semester['id']})
        if not isinstance(data, dict) or not isinstance(data.get('gradeDetails'), list):
            raise RemoteError('UPSTREAM_BAD_RESPONSE', '远端记录列表格式错误')
        row = next((item for item in data['gradeDetails'] if isinstance(item, dict) and item.get('id') == record_id), None)
        if row is None:
            return {'status': 'pending', 'recordId': record_id, 'message': '已返回回执，最近记录列表中暂未找到该条'}
        try:
            effective = float(row.get('effectiveMileage') or 0)
            total = float(row.get('totalMileage') or 0)
            parts = int(row.get('effectivePart') or 0)
            scoring_type = int(row.get('scoringType') or 1)
            if not math.isfinite(effective) or not math.isfinite(total): raise ValueError()
        except (ValueError, TypeError, OverflowError):
            raise RemoteError('UPSTREAM_BAD_RESPONSE', '远端记录的里程或段数字段无效') from None
        reason = row.get('uneffectiveReason') or ''
        credited = effective > 0 if scoring_type == 1 else parts > 0
        return {'status': 'effective' if not reason and credited else 'ineffective', 'recordId': record_id,
                'effectiveMileageKm': effective, 'totalMileageKm': total, 'effectivePart': parts,
                'uneffectiveReason': str(reason), 'scoringType': scoring_type,
                'startTime': row.get('startTime'), 'endTime': row.get('endTime'),
                'checkedAt': datetime.now(timezone.utc).isoformat(), 'source': 'server-record'}


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def signed_payload(value, snapshot):
    """Check shape only; the real service, not this client, verifies native signatures."""
    if not isinstance(value, dict): raise ValueError('上传请求必须是 JSON 对象')
    # Local preview objects must never be silently converted into real records.
    if value.get('isSimulated') or 'config' in value or 'points' in value:
        raise ValueError('这里需要官方 App 的完整签名请求，不能使用本地模拟导出')
    required = ('appVersion', 'deviceType', 'systemVersion', 'startTime', 'endTime', 'endTimeDate',
                'keepTime', 'totalMileage', 'gpsMileage', 'effectiveMileage', 'effectivePart', 'totalPart',
                'avePace', 'calorie', 'paceNumber', 'paceRange', 'scoringType', 'semesterId',
                'limitationsGoalsSexInfoId', 'routineLine', 'signPoint', 'signTime', 'signDigital', 'oct', 'type')
    missing = [key for key in required if key not in value]
    if missing: raise ValueError('缺少官方请求字段：'+', '.join(missing))
    if value['semesterId'] != snapshot['semester']['id']:
        raise ValueError('上传请求不属于当前远端学期')
    if value['limitationsGoalsSexInfoId'] != snapshot['runningLimit'].get('limitationsGoalsSexInfoId'):
        raise ValueError('上传请求与当前远端跑步规则不一致')
    if not isinstance(value['signDigital'], str) or not re.fullmatch(r'[0-9a-fA-F]{40}', value['signDigital']):
        raise ValueError('signDigital 应为官方 App 生成的40位签名')
    try:
        octet = value['oct']
        if not isinstance(octet, str) or not 20 <= len(octet) <= 16384: raise ValueError()
        if not base64.b64decode(''.join(octet.split()), validate=True): raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('oct 应为官方 App 生成的完整 Base64 字段') from None
    for name in ('endTimeDate', 'keepTime', 'totalMileage', 'gpsMileage', 'effectiveMileage', 'effectivePart',
                 'totalPart', 'avePace', 'calorie', 'paceNumber', 'paceRange', 'scoringType'):
        if not numeric(value[name]) or value[name] < 0: raise ValueError(name+' 必须是非负有限数值')
    if value['keepTime'] <= 0 or value['totalMileage'] <= 0: raise ValueError('运动时长和里程必须大于0')
    try:
        start, end, sign = (datetime.strptime(value[name], '%Y-%m-%d %H:%M:%S') for name in ('startTime', 'endTime', 'signTime'))
        if end <= start or sign < start: raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('运动时间字段无效') from None
    line = value['routineLine']
    if not isinstance(line, list) or not 2 <= len(line) <= 10000: raise ValueError('routineLine 应包含2–10000个点')
    for point in line:
        if not isinstance(point, dict) or any(not numeric(point.get(k)) for k in ('latitude', 'longitude')):
            raise ValueError('routineLine 坐标无效')
        if not -90 <= point['latitude'] <= 90 or not -180 <= point['longitude'] <= 180:
            raise ValueError('routineLine 坐标越界')
        if isinstance(point.get('source'), str) and point['source'].startswith('simulated'):
            raise ValueError('不能将本地模拟点转换为官方签名请求')
    if not isinstance(value['signPoint'], list): raise ValueError('signPoint 必须为数组')
    packed = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))
    if len(packed.encode()) > 1_800_000: raise ValueError('上传请求过大')
    return packed, hashlib.sha256(packed.encode()).hexdigest(), {
        'startTime': value['startTime'], 'endTime': value['endTime'], 'durationSeconds': value['keepTime'],
        'distanceKm': value['totalMileage'], 'pointCount': len(line), 'signatureLocallyVerified': False,
        'message': '结构检查完成；签名和有效成绩以真实服务器判定为准。',
    }
