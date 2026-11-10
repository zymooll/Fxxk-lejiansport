"""Loopback running-workflow MVP with an authenticated LeGym adapter."""
import argparse
import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from trajectory import generate, distance, length, number, MIN_LAPS, MAX_LAPS
from geometry import evaluate
from remote import RemoteClient, RemoteError, signed_payload
from signing import build_simulation

BASE = Path(__file__).resolve().parent
DEMO_HEADERS = {'Authorization': 'Bearer local-demo-only', 'Cookie': 'JSESSIONID=LOCAL_DEMO_ONLY'}
LOCAL_COOKIE = 'RUN_MVP_LOCAL'

def canonical_credentials(headers):
    if not isinstance(headers, dict):
        raise ValueError('鉴权信息必须为 JSON 对象')
    lowered = {str(k).lower(): v for k, v in headers.items()}
    authorization = lowered.get('authorization', '')
    cookie = lowered.get('cookie', '')
    if not isinstance(authorization, str) or not isinstance(cookie, str) or len(authorization)+len(cookie)>8192:
        raise ValueError('鉴权字段格式错误')
    if not authorization.startswith('Bearer ') or not authorization[7:].strip():
        raise ValueError('缺少 Bearer 验证字段')
    parsed = SimpleCookie()
    try:
        parsed.load(cookie)
        session = parsed['JSESSIONID'].value
    except Exception:
        raise ValueError('缺少 JSESSIONID 验证字段') from None
    if not session:
        raise ValueError('JSESSIONID 不能为空')
    return json.dumps([authorization.strip(), session], separators=(',', ':')).encode()

def credential_verifier(headers):
    salt = secrets.token_hex(32)
    digest = hashlib.sha256(bytes.fromhex(salt)+canonical_credentials(headers)).hexdigest()
    return {'salt': salt, 'digest': digest}

def matches(headers, verifier):
    try:
        candidate = hashlib.sha256(bytes.fromhex(verifier['salt'])+canonical_credentials(headers)).hexdigest()
        return hmac.compare_digest(candidate, verifier['digest'])
    except (ValueError, KeyError, TypeError):
        return False

class APIError(Exception):
    def __init__(self, status, code, message, details=None):
        self.status, self.code, self.message, self.details = status, code, message, details

class Clock:
    def monotonic(self): return time.monotonic()
    def now(self): return datetime.now(timezone.utc).isoformat()

class Application:
    def __init__(self, data_dir, reference_path=None, clock=None, verifier=None, remote_factory=None):
        self.data_dir = Path(data_dir); self.data_dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock or Clock()
        self.reference = json.loads(Path(reference_path or ((self.data_dir/'reference.json') if (self.data_dir/'reference.json').is_file() else BASE/'fixtures/reference.json')).read_text('utf-8'))
        self.region = json.loads(((self.data_dir/'region.json') if (self.data_dir/'region.json').is_file() else BASE/'fixtures/region.json').read_text('utf-8'))
        self.reference_hash = hashlib.sha256(json.dumps(self.reference, sort_keys=True).encode()).hexdigest()
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.data_dir/'runs.sqlite3', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, state TEXT NOT NULL,
          plan_json TEXT NOT NULL, owner TEXT NOT NULL, result_json TEXT, uploaded_json TEXT, idempotency_key TEXT);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, action TEXT,
          run_id TEXT, status INTEGER, summary_json TEXT);
        CREATE TABLE IF NOT EXISTS remote_uploads(id TEXT PRIMARY KEY, owner TEXT NOT NULL, run_id TEXT,
          created_at TEXT NOT NULL, state TEXT NOT NULL, payload_json TEXT NOT NULL, body_hash TEXT NOT NULL,
          summary_json TEXT NOT NULL, record_id TEXT, error_json TEXT, UNIQUE(owner,body_hash));
        ''')
        if 'deleted_at' not in {row['name'] for row in self.db.execute('PRAGMA table_info(runs)')}:
            self.db.execute('ALTER TABLE runs ADD COLUMN deleted_at TEXT')
        self.db.execute("UPDATE remote_uploads SET state='unknown' WHERE state='sending'")
        for row in self.db.execute("SELECT id,summary_json FROM remote_uploads WHERE state='draft'").fetchall():
            if str(json.loads(row['summary_json']).get('uneffectiveReason', '')).startswith('本地MVP模拟轨迹联调'):
                self.db.execute("UPDATE remote_uploads SET state='superseded' WHERE id=?", (row['id'],))
        self.db.execute("UPDATE runs SET state='interrupted' WHERE state='running'"); self.db.commit()
        self.starts = {}; self.sessions = {}; self.login_failures = []
        self.remote_clients = {}
        self.remote_factory = remote_factory or RemoteClient
        self.demo_verifier = credential_verifier(DEMO_HEADERS)
        verifier_path = self.data_dir/'verification.json'
        self.user_verifier = verifier if verifier is not None else (json.loads(verifier_path.read_text('utf-8')) if verifier_path.exists() else None)
        control_path = self.data_dir/'control.key'
        if not control_path.exists():control_path.write_text(secrets.token_urlsafe(32),'ascii')
        self.control_key = control_path.read_text('ascii').strip()

    def audit(self, action, status=200, run_id=None, **summary):
        # Only predefined, noncredential summaries are passed here. Never log request bodies or headers.
        self.db.execute('INSERT INTO events(at,action,run_id,status,summary_json) VALUES(?,?,?,?,?)',
                        (self.clock.now(), action, run_id, status, json.dumps(summary, ensure_ascii=False)))
        self.db.commit()

    def login(self, headers, demo=False):
        now = self.clock.monotonic()
        self.login_failures = [t for t in self.login_failures if now-t < 60]
        if len(self.login_failures) >= 12:
            raise APIError(429, 'LOGIN_RATE_LIMIT', '本地验证失败次数过多，请稍后重试')
        if demo:
            owner = 'demo'
        elif self.user_verifier and matches(headers, self.user_verifier):
            owner = 'configured'
        elif matches(headers, self.demo_verifier):
            owner = 'demo'
        else:
            self.login_failures.append(now)
            self.audit('auth.rejected', 401)
            raise APIError(401, 'LOCAL_AUTH_MISMATCH', '与本地验证配置不匹配；未请求真实服务器')
        token = secrets.token_urlsafe(32)
        self.sessions[token] = (owner, now+8*3600)
        self.audit('auth.accepted', mode=owner)
        return token, owner

    def auth(self, token):
        value = self.sessions.get(token)
        if not value or value[1] <= self.clock.monotonic():
            self.sessions.pop(token, None)
            self.remote_clients.pop(token, None)
            raise APIError(401, 'LOCAL_SESSION_REQUIRED', '请先完成本地登录')
        return value[0]

    def remote_login(self, payload, credential_file=None):
        try:
            client = self.remote_factory(payload)
            if credential_file is not None:
                client.bind_credentials_file(credential_file)
            client.home(self.reference[0])
        except ValueError as error:
            raise APIError(400, 'INVALID_CREDENTIALS', str(error)) from None
        token = secrets.token_urlsafe(32)
        self.sessions[token] = (client.owner, self.clock.monotonic()+8*3600)
        self.remote_clients[token] = client
        self.audit('remote.auth.accepted')
        return token, client.owner

    def remote_client(self, token):
        self.auth(token)
        client = self.remote_clients.get(token)
        if client is None:
            raise APIError(403, 'REMOTE_SESSION_REQUIRED', '请使用真实服务器登录')
        return client

    def region_for(self, client=None):
        if client and client.snapshot:
            live = client.snapshot.get('runningRange')
            if live and live.get('rangeRunningRegion'):
                return {'polygons': live['rangeRunningRegion'], 'excludedPolygons': live.get('invalidRangeRunningRegion') or [],
                        'source': 'https://cpes.legym.cn/running/app/getRunningRange'}
        return self.region

    def trajectory_settings(self):
        return {'lapsMin': MIN_LAPS, 'lapsMax': MAX_LAPS, 'referenceLapMeters': length(self.reference),
                'intervalsPerLap': 150, 'generationOnlySupported': True, 'saveGeneratedSupported': True}

    def generate_trajectory(self, config, client=None):
        try:
            plan = generate(self.reference, config)
        except ValueError as error:
            raise APIError(400, 'INVALID_SETTINGS', str(error)) from None
        plan.update(executionMode='generate-only', timingSkipped=True, realWaitSeconds=0,
                    productionSubmitted=False, regionCheck=evaluate(plan['points'], self.region_for(client)),
                    generationId=str(uuid.uuid4()))
        return plan

    def save_generated(self, payload, owner, client=None):
        if not isinstance(payload, dict) or not isinstance(payload.get('config'), dict):
            raise APIError(400, 'INVALID_SETTINGS', '请提供生成轨迹的采样设置')
        generation_id = payload.get('generationId')
        try: generation_id = str(uuid.UUID(generation_id))
        except (ValueError, TypeError, AttributeError):
            raise APIError(400, 'GENERATION_ID_REQUIRED', '请先生成轨迹，再保存到本地记录') from None
        key = 'generation:'+generation_id
        plan = self.generate_trajectory(payload['config'], client)
        existing = self.db.execute('SELECT * FROM runs WHERE owner=? AND idempotency_key=?', (owner, key)).fetchone()
        if existing:
            if existing['deleted_at']:
                raise APIError(409, 'LOCAL_RECORD_DELETED', '该生成记录已删除，可撤销删除或重新生成')
            if json.loads(existing['plan_json'])['config'] != plan['config']:
                raise APIError(409, 'GENERATION_CONTENT_CHANGED', '本次生成标识已经用于其他采样设置')
            return {'saved': True, 'idempotentReplay': True, 'run': self.status(existing)}
        plan['generationId'] = generation_id
        plan['validationRegion'] = self.region_for(client)
        run_id = str(uuid.uuid4()); at = self.clock.now()
        result = {'localRecordId': run_id, 'isSimulated': True, 'productionSubmitted': False,
                  'generationOnly': True, 'timingSkipped': True, 'savedAt': at,
                  'acceptedForLocalTest': plan['regionCheck']['passed'], 'clockScale': plan['config']['clockScale'],
                  'durationSeconds': plan['durationSeconds'], 'distanceMeters': round(plan['observedDistanceMeters'], 3),
                  'pointCount': plan['pointCount'], 'region': plan['regionCheck'],
                  'checks': ['simulated-flag', 'finite-coordinates', 'timing-skipped'],
                  'message': '完整模拟轨迹已保存到本地记录，未运行计时。'}
        uploaded = {'isSimulated': True, 'points': plan['points']}
        self.db.execute('INSERT INTO runs(id,created_at,state,plan_json,owner,result_json,uploaded_json,idempotency_key) VALUES(?,?,?,?,?,?,?,?)',
                        (run_id, at, 'generated', json.dumps(plan, separators=(',', ':')), owner,
                         json.dumps(result, ensure_ascii=False), json.dumps(uploaded, separators=(',', ':')), key))
        self.db.commit(); self.audit('trajectory.saved', run_id=run_id, laps=plan['config']['laps'])
        return {'saved': True, 'idempotentReplay': False, 'run': self.status(self.get_run(run_id, owner))}

    def record_summary(self, row):
        plan = json.loads(row['plan_json'])
        result = json.loads(row['result_json']) if row['result_json'] else None
        remote = self.record_upload(row['id'], row['owner'])
        remote_summary = self.upload_summary(remote) if remote else None
        submitted = bool(result and result.get('productionSubmitted')) or bool(remote and remote['state'] == 'accepted')
        blocked = ('已上传到远端' if submitted else '上传结果待核对，不能重复提交' if remote and remote['state'] in ('unknown', 'sending')
                   else '请先完成并保存此记录' if row['state'] not in ('uploaded', 'generated') else None)
        return dict(id=row['id'], createdAt=row['created_at'], state=row['state'], result=result,
                    laps=plan['config']['laps'], clockScale=plan['config']['clockScale'],
                    durationSeconds=plan['durationSeconds'], pointCount=result['pointCount'] if result else plan['pointCount'],
                    distanceMeters=result['distanceMeters'] if result else plan['observedDistanceMeters'],
                    remoteUpload=remote_summary, canUpload=blocked is None, uploadBlockedReason=blocked,
                    canDelete=not (remote and remote['state'] == 'sending'))

    def record_upload(self, run_id, owner):
        return self.db.execute("SELECT * FROM remote_uploads WHERE run_id=? AND owner=? AND state<>'superseded' ORDER BY CASE state WHEN 'accepted' THEN 0 WHEN 'unknown' THEN 1 WHEN 'sending' THEN 1 ELSE 2 END,rowid DESC LIMIT 1",
                               (run_id, owner)).fetchone()

    def home(self, owner, client=None):
        rows = self.db.execute("SELECT * FROM runs WHERE owner=? AND deleted_at IS NULL ORDER BY created_at DESC,rowid DESC LIMIT 20", (owner,)).fetchall()
        history = [self.record_summary(row) for row in rows]
        completed = [json.loads(r['result_json']) for r in self.db.execute(
            "SELECT result_json FROM runs WHERE owner=? AND deleted_at IS NULL AND state IN ('uploaded','generated') AND result_json IS NOT NULL", (owner,))]
        result = {'source': 'local-fixture', 'isSimulated': True, 'user': {'name': '本地验证用户', 'authMode': owner},
                'summary': {'localRuns': len(completed), 'localDistanceKm': round(sum(r['distanceMeters'] for r in completed)/1000, 3)},
                'trajectory': self.trajectory_settings(),
                'rules': {'name': '本地仿真规则', 'openHours': '00:00–24:00（本地测试）', 'lapsMin': MIN_LAPS, 'lapsMax': MAX_LAPS,
                          'clockScales': [1,30,60], 'requiresSimulatedFlag': True, 'requiresElapsedTime': True,
                          'minimumInsideFraction': .70, 'trajectoryValidation': 'region-only',
                          'referenceLapMeters': round(length(self.reference), 3)},
                'region': self.region,
                'reference': {'pointCount': len(self.reference), 'hash': self.reference_hash, 'points': self.reference,
                              'description': '轴向平直内沿；原始起点固定；两侧直道经度恒定'},
                'announcements': ['所有主页数据和成绩均来自本机测试后端。', '支持真实计时与明确标记的加速测试，不连接乐健生产服务。'],
                'history': history}
        if client:
            remote = client.snapshot or client.home(self.reference[0])
            result.update(source='remote', isSimulated=False, remote=remote,
                          user={'name': client.profile.get('nickName') or client.profile.get('realName') or '远端账户',
                                'authMode': 'remote', 'schoolName': client.profile.get('schoolName')})
            result['region'] = self.region_for(client)
            result['announcements'] = ['真实主页、规则、区域和累计里程已从远端获取。',
                                      '上传不再强制置零或写入无效原因；接收后回查服务器记录状态。']
        return result

    def upload_summary(self, row):
        return {'id': row['id'], 'runId': row['run_id'], 'createdAt': row['created_at'], 'state': row['state'],
                'summary': json.loads(row['summary_json']), 'remoteRecordId': row['record_id'],
                'error': json.loads(row['error_json']) if row['error_json'] else None,
                'productionSubmitted': row['state'] == 'accepted'}

    def remote_draft(self, payload, owner, client, run_id=None, simulated=False):
        try:
            packed, body_hash, summary = signed_payload(payload, client.snapshot)
        except ValueError as error:
            raise APIError(422, 'REMOTE_PAYLOAD_INVALID', str(error)) from None
        summary.update(isSimulated=simulated, effectiveMileage=payload['effectiveMileage'],
                       uneffectiveReason=payload.get('uneffectiveReason', ''))
        existing = self.db.execute('SELECT * FROM remote_uploads WHERE owner=? AND body_hash=?', (owner, body_hash)).fetchone()
        if existing: return self.upload_summary(existing)
        draft_id = str(uuid.uuid4())
        self.db.execute('INSERT INTO remote_uploads(id,owner,run_id,created_at,state,payload_json,body_hash,summary_json) VALUES(?,?,?,?,?,?,?,?)',
                        (draft_id, owner, run_id, self.clock.now(), 'draft', packed, body_hash, json.dumps(summary, ensure_ascii=False)))
        self.db.commit()
        self.audit('remote.upload.prepared', run_id=run_id, pointCount=summary['pointCount'], simulated=simulated)
        return self.upload_summary(self.db.execute('SELECT * FROM remote_uploads WHERE id=?', (draft_id,)).fetchone())

    def prepare_simulated_remote(self, run_id, owner, client):
        row = self.get_run(run_id, owner)
        previous = self.record_upload(run_id, owner)
        if previous: return self.upload_summary(previous)
        plan = json.loads(row['plan_json'])
        if plan['config']['clockScale'] != 1:
            raise APIError(422, 'REMOTE_REALTIME_REQUIRED', '远端模拟联调需要1倍真实计时；加速测试仅保存到本地')
        if row['state'] not in ('running', 'uploaded'):
            raise APIError(409, 'RUN_NOT_RUNNING', '记录已取消或中断')
        if row['state'] == 'running' and self.status(row)['state'] != 'ready':
            raise APIError(409, 'TOO_EARLY', '真实计时尚未完成')
        snapshot = client.home(plan['points'][0])
        try:
            payload = build_simulation(plan, row['created_at'], snapshot, client.profile, self.clock.now())
        except ValueError as error:
            raise APIError(422, 'REMOTE_SIGNING_FAILED', str(error)) from None
        # A locally saved record is not a remote receipt.
        if row['state'] != 'uploaded':
            self.upload(run_id, {'isSimulated': True, 'points': plan['points']}, owner, 'remote-draft-'+run_id)
        return self.remote_draft(payload, owner, client, run_id, simulated=True)

    def mark_local_submitted(self, run_id, owner, receipt):
        local = self.db.execute('SELECT * FROM runs WHERE id=? AND owner=?', (run_id, owner)).fetchone()
        if not local: raise APIError(404, 'RUN_NOT_FOUND', '本地记录不存在')
        result = json.loads(local['result_json'])
        summary = receipt.get('summary')
        if summary is None:
            draft = self.db.execute('SELECT summary_json FROM remote_uploads WHERE id=? AND owner=?', (receipt['id'], owner)).fetchone()
            summary = json.loads(draft['summary_json']) if draft else {}
        result.update(productionSubmitted=True, remoteRecordId=receipt['remoteRecordId'], remoteDraftId=receipt['id'],
                      requestedRemoteEffectiveMileage=summary.get('effectiveMileage'),
                      message='远端已返回记录ID；是否有效以服务器记录回查结果为准。')
        if summary.get('serverRecord'):
            result['serverRecord'] = summary['serverRecord']
        self.db.execute('UPDATE runs SET result_json=? WHERE id=? AND owner=?',
                        (json.dumps(result, ensure_ascii=False), run_id, owner))

    def upload_saved_record(self, run_id, owner, client):
        row = self.get_run(run_id, owner)
        if row['state'] not in ('uploaded', 'generated') or not row['uploaded_json'] or not row['result_json']:
            raise APIError(409, 'LOCAL_RECORD_NOT_SAVED', '请先把完整轨迹保存到本地记录')
        previous = self.record_upload(run_id, owner)
        if previous and previous['state'] == 'accepted':
            receipt = self.upload_summary(previous); receipt['idempotentReplay'] = True
            return receipt
        if previous and previous['state'] in ('unknown', 'sending'):
            raise APIError(409, 'REMOTE_UPLOAD_NOT_RETRYABLE', '此记录的上传结果待核对，不能重复提交', self.upload_summary(previous))
        result = json.loads(row['result_json'])
        if result.get('productionSubmitted'):
            return {'id': result.get('remoteDraftId'), 'runId': run_id, 'state': 'accepted',
                    'remoteRecordId': result['remoteRecordId'], 'productionSubmitted': True, 'idempotentReplay': True}
        uploaded = json.loads(row['uploaded_json'])
        points = uploaded.get('points')
        if uploaded.get('isSimulated') is not True or not isinstance(points, list) or not 2 <= len(points) <= 10000:
            raise APIError(422, 'LOCAL_RECORD_INCOMPLETE', '本地记录中缺少完整模拟轨迹')
        for point in points:
            if not isinstance(point, dict) or any(not number(point.get(k)) for k in ('latitude', 'longitude')) or not -90 <= point['latitude'] <= 90 or not -180 <= point['longitude'] <= 180:
                raise APIError(422, 'LOCAL_RECORD_INCOMPLETE', '本地记录中存在无效坐标')
        plan = json.loads(row['plan_json'])
        # Always upload what this record actually saved, not a regenerated or current-page plan.
        plan['points'] = points
        plan['observedDistanceMeters'] = length(points)
        snapshot = client.home(points[0])
        try:
            payload = build_simulation(plan, row['created_at'], snapshot, client.profile, self.clock.now(), rebase_to_upload=True)
        except ValueError as error:
            raise APIError(422, 'REMOTE_SIGNING_FAILED', str(error)) from None
        draft = self.remote_draft(payload, owner, client, run_id, simulated=True)
        # A previously prepared, unsent request must not remain a second path to upload this run.
        self.db.execute("UPDATE remote_uploads SET state='superseded' WHERE run_id=? AND owner=? AND state='draft' AND id<>?",
                        (run_id, owner, draft['id']))
        if draft['state'] == 'draft':
            summary = dict(draft['summary'], timestampMode='upload-time-backfill', originalCreatedAt=row['created_at'],
                           originalClockScale=plan['config']['clockScale'])
            self.db.execute('UPDATE remote_uploads SET summary_json=? WHERE id=?', (json.dumps(summary, ensure_ascii=False), draft['id']))
        self.db.commit()
        receipt = self.commit_remote(draft['id'], owner, client)
        # A matching already-accepted body can be shared by two local copies without sending twice.
        self.mark_local_submitted(run_id, owner, receipt); self.db.commit()
        receipt['localRecordId'] = run_id
        return receipt

    def delete_record(self, run_id, owner):
        row = self.db.execute('SELECT * FROM runs WHERE id=? AND owner=?', (run_id, owner)).fetchone()
        if not row: raise APIError(404, 'RUN_NOT_FOUND', '本地记录不存在')
        if self.db.execute("SELECT 1 FROM remote_uploads WHERE run_id=? AND owner=? AND state='sending'", (run_id, owner)).fetchone():
            raise APIError(409, 'REMOTE_UPLOAD_IN_PROGRESS', '此记录正在上传，请等待结果后再删除')
        if not row['deleted_at']:
            self.db.execute("UPDATE runs SET deleted_at=?,state=CASE WHEN state='running' THEN 'cancelled' ELSE state END WHERE id=? AND owner=?",
                            (self.clock.now(), run_id, owner))
            self.db.commit(); self.starts.pop(run_id, None); self.audit('run.deleted', run_id=run_id)
        return {'deleted': True, 'localRecordId': run_id, 'canUndo': True, 'remoteRecordsChanged': False}

    def restore_record(self, run_id, owner):
        row = self.db.execute('SELECT * FROM runs WHERE id=? AND owner=?', (run_id, owner)).fetchone()
        if not row: raise APIError(404, 'RUN_NOT_FOUND', '本地记录不存在')
        self.db.execute('UPDATE runs SET deleted_at=NULL WHERE id=? AND owner=?', (run_id, owner)); self.db.commit()
        self.audit('run.restored', run_id=run_id)
        return {'restored': True, 'run': self.status(self.get_run(run_id, owner))}

    def commit_remote(self, draft_id, owner, client):
        row = self.db.execute('SELECT * FROM remote_uploads WHERE id=? AND owner=?', (draft_id, owner)).fetchone()
        if not row: raise APIError(404, 'REMOTE_UPLOAD_NOT_FOUND', '远端上传草稿不存在')
        if row['state'] == 'accepted':
            result = self.upload_summary(row); result['idempotentReplay'] = True
            return result
        if row['state'] != 'draft':
            raise APIError(409, 'REMOTE_UPLOAD_NOT_RETRYABLE', '此请求已处理或结果不确定；请先在官方 App 核对，不能自动重发', self.upload_summary(row))
        if row['run_id']:
            self.get_run(row['run_id'], owner)
        self.db.execute("UPDATE remote_uploads SET state='sending' WHERE id=?", (draft_id,)); self.db.commit()
        try:
            record_id = client.submit(json.loads(row['payload_json']))
        except RemoteError as error:
            state = 'unknown' if error.uncertain else 'rejected'
            self.db.execute('UPDATE remote_uploads SET state=?,error_json=? WHERE id=?',
                            (state, json.dumps(error.public(), ensure_ascii=False), draft_id)); self.db.commit()
            self.audit('remote.upload.'+state, 502, run_id=row['run_id'], errorCode=error.code)
            raise
        self.db.execute("UPDATE remote_uploads SET state='accepted',record_id=? WHERE id=?", (record_id, draft_id))
        if row['run_id']:
            self.mark_local_submitted(row['run_id'], owner, {'id': draft_id, 'remoteRecordId': record_id})
        self.db.commit()
        self.audit('remote.upload.accepted', run_id=row['run_id'])
        self.verify_remote_record(draft_id, owner, client)
        result = self.upload_summary(self.db.execute('SELECT * FROM remote_uploads WHERE id=?', (draft_id,)).fetchone())
        result['idempotentReplay'] = False
        return result

    def verify_remote_record(self, draft_id, owner, client):
        row = self.db.execute('SELECT * FROM remote_uploads WHERE id=? AND owner=?', (draft_id, owner)).fetchone()
        if not row: raise APIError(404, 'REMOTE_UPLOAD_NOT_FOUND', '远端上传记录不存在')
        if row['state'] != 'accepted' or not row['record_id']:
            raise APIError(409, 'REMOTE_RECEIPT_REQUIRED', '只有已返回记录ID的上传才能回查状态')
        # This read runs after durable acceptance and cannot trigger another submission.
        try:
            verification = client.record_status(row['record_id'])
        except RemoteError as error:
            verification = {'status': 'pending', 'recordId': row['record_id'], 'message': error.message}
        summary = json.loads(row['summary_json']); summary['serverRecord'] = verification
        self.db.execute('UPDATE remote_uploads SET summary_json=? WHERE id=?', (json.dumps(summary, ensure_ascii=False), draft_id))
        if row['run_id']:
            self.mark_local_submitted(row['run_id'], owner, {'id': draft_id, 'remoteRecordId': row['record_id'], 'summary': summary})
        self.db.commit()
        return self.upload_summary(self.db.execute('SELECT * FROM remote_uploads WHERE id=?', (draft_id,)).fetchone())

    def get_run(self, run_id, owner):
        row = self.db.execute('SELECT * FROM runs WHERE id=? AND owner=? AND deleted_at IS NULL', (run_id, owner)).fetchone()
        if not row:
            raise APIError(404, 'RUN_NOT_FOUND', '本地记录不存在')
        return row

    def status(self, row):
        plan = json.loads(row['plan_json']); state = row['state']
        elapsed = min(plan['durationSeconds'], max(0, self.clock.monotonic()-self.starts.get(row['id'], self.clock.monotonic()))*plan['config']['clockScale'])
        if state == 'running' and elapsed >= plan['durationSeconds']:
            state = 'ready'
        if state in ('uploaded', 'generated'): elapsed = plan['durationSeconds']
        return {'id': row['id'], 'state': state, 'createdAt': row['created_at'], 'isSimulated': True,
                'virtualElapsedSeconds': elapsed, 'durationSeconds': plan['durationSeconds'],
                'remainingRealSeconds': max(0, plan['durationSeconds']-elapsed)/plan['config']['clockScale'],
                'clockScale': plan['config']['clockScale'], 'pointCount': plan['pointCount'],
                'generationOnly': plan.get('executionMode') == 'generate-only',
                'result': json.loads(row['result_json']) if row['result_json'] else None}

    def start(self, config, owner, region=None):
        active = self.db.execute("SELECT id FROM runs WHERE owner=? AND state='running' AND deleted_at IS NULL", (owner,)).fetchone()
        if active: raise APIError(409, 'RUN_ALREADY_ACTIVE', '请先提交或取消当前本地测试', {'runId': active['id']})
        try: plan = generate(self.reference, config)
        except ValueError as e: raise APIError(400, 'INVALID_SETTINGS', str(e)) from None
        run_id = str(uuid.uuid4())
        plan['validationRegion'] = region or self.region
        self.db.execute('INSERT INTO runs(id,created_at,state,plan_json,owner) VALUES(?,?,?,?,?)',
                        (run_id,self.clock.now(),'running',json.dumps(plan,separators=(',',':')),owner))
        self.db.commit(); self.starts[run_id] = self.clock.monotonic()
        self.audit('run.started', run_id=run_id, laps=plan['config']['laps'], clockScale=plan['config']['clockScale'], pointCount=plan['pointCount'])
        return {'run': self.status(self.get_run(run_id,owner)), 'plan': plan}

    def upload(self, run_id, payload, owner, key):
        row = self.get_run(run_id, owner)
        if not isinstance(key, str) or not 8 <= len(key) <= 128:
            raise APIError(400, 'IDEMPOTENCY_REQUIRED', '需要8–128字符的本地幂等键')
        if not isinstance(payload, dict): raise APIError(400, 'INVALID_PAYLOAD', '上传内容必须为对象')
        if row['state'] == 'uploaded':
            if row['idempotency_key'] == key and json.loads(row['uploaded_json']) == payload:
                result = json.loads(row['result_json']); result['idempotentReplay'] = True
                self.audit('upload.idempotent', run_id=run_id)
                return result
            raise APIError(409, 'ALREADY_UPLOADED', '此记录已经提交，不能重复写入')
        if row['state'] != 'running': raise APIError(409, 'RUN_NOT_RUNNING', '记录已取消或中断')
        status = self.status(row)
        if status['state'] != 'ready':
            self.audit('upload.too_early', 409, run_id=run_id)
            raise APIError(409, 'TOO_EARLY', '服务端计时尚未完成', {'remainingRealSeconds': status['remainingRealSeconds']})
        plan = json.loads(row['plan_json']); points = payload.get('points')
        errors = []
        if payload.get('isSimulated') is not True: errors.append('必须明确标记 isSimulated=true')
        if not isinstance(points,list) or not 2<=len(points)<=10000: errors.append('轨迹应包含2–10000个坐标点')
        if not errors:
            last_time=-1
            timed=sum(isinstance(point,dict) and 'elapsedMs' in point for point in points)
            if timed not in (0,len(points)):errors.append('时间字段应全部提供或全部省略')
            for i,point in enumerate(points):
                if not isinstance(point,dict) or any(not number(point.get(k)) for k in ('latitude','longitude')):
                    errors.append(f'第{i+1}点包含无效数字'); break
                if not -90 <= point['latitude'] <= 90 or not -180 <= point['longitude'] <= 180:
                    errors.append(f'第{i+1}点坐标越界'); break
                if 'accuracyMeters' in point and (not number(point['accuracyMeters']) or point['accuracyMeters']<=0):
                    errors.append(f'第{i+1}点精度字段无效'); break
                if timed and ('elapsedMs' not in point or not number(point['elapsedMs']) or not 0<=point['elapsedMs']<=plan['durationSeconds']*1000+1000 or point['elapsedMs']<=last_time):
                    errors.append(f'第{i+1}点时间字段无效或不递增'); break
                if timed:last_time=point['elapsedMs']
        if errors:
            self.audit('upload.rejected', 422, run_id=run_id, errorCount=len(errors))
            raise APIError(422,'VALIDATION_FAILED','本地校验未通过',{'errors':errors})
        region_check = evaluate(points, plan.get('validationRegion', self.region))
        if not region_check['passed']:
            self.audit('upload.region_rejected', 422, run_id=run_id, ratio=region_check['ratio'])
            raise APIError(422,'REGION_THRESHOLD_FAILED','区域内采样点不足70%',{'region':region_check})
        calculated = length(points)
        result = {'localRecordId': run_id, 'acceptedForLocalTest': True, 'isSimulated': True,
                  'productionSubmitted': False, 'uploadedAt': self.clock.now(), 'clockScale': plan['config']['clockScale'],
                  'durationSeconds': plan['durationSeconds'], 'distanceMeters': round(calculated,3),
                  'pointCount': len(points), 'idempotentReplay': False,
                  'region': region_check, 'trajectoryValidation': 'region-only',
                  'checks': ['local-auth','elapsed-time','simulated-flag','finite-coordinates','optional-time-order','region-at-least-70-percent'],
                  'message': '本地测试记录已保存；未提交到任何外部服务'}
        self.db.execute("UPDATE runs SET state='uploaded',result_json=?,uploaded_json=?,idempotency_key=? WHERE id=?",
                        (json.dumps(result,ensure_ascii=False),json.dumps(payload,separators=(',',':')),key,run_id)); self.db.commit()
        self.audit('upload.accepted', run_id=run_id, pointCount=len(points),distanceMeters=result['distanceMeters'])
        return result

    def dispatch(self, method, path, payload, token, headers):
        with self.lock:
            if method=='GET' and path=='/api/health':
                current_session = self.sessions.get(token)
                return {'mode':'hybrid','service':'run-mvp-remote-v2',
                        'instance':hashlib.sha256(str(BASE).encode()).hexdigest()[:12],
                        'productionConnection':bool(token in self.remote_clients and current_session and current_session[1]>self.clock.monotonic()),
                        'remoteCredentialFileConfigured':(self.data_dir/'credentials.private.json').is_file(),
                        'upstream':'https://cpes.legym.cn','providedVerifierConfigured':bool(self.user_verifier),
                        'credentialRefresh':{'automatic':True,'refreshWithoutAuthorization':True,
                                             'readRetries':1,'checkpointEncryption':'Windows DPAPI'},
                        'trajectory':self.trajectory_settings(),
                        'recordActions':{'delete':True,'restore':True,'uploadSaved':True,'uploadTimestampMode':'upload-time-backfill',
                                         'uploadValidityMode':'server-record','automaticRecordCheck':True}}, None
            if method=='POST' and path in ('/api/auth/remote', '/api/auth/remote-file'):
                if path.endswith('-file'):
                    try: payload = json.loads((self.data_dir/'credentials.private.json').read_text('utf-8-sig'))
                    except (OSError, ValueError): raise APIError(400, 'CREDENTIAL_FILE_UNAVAILABLE', '请先使用 configure_remote.py 配置凭据文件') from None
                local_token, owner = self.remote_login(payload, self.data_dir/'credentials.private.json' if path.endswith('-file') else None)
                return {'authenticated':True,'mode':'remote','scope':'legym-api'}, f'{LOCAL_COOKIE}={local_token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800'
            if method=='POST' and path in ('/api/auth/login','/api/auth/demo'):
                local_token, owner = self.login(payload, demo=path.endswith('/demo'))
                return {'authenticated':True,'mode':owner,'scope':'local-verification-only'}, f'{LOCAL_COOKIE}={local_token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800'
            owner = self.auth(token)
            if method=='POST' and path=='/api/auth/logout':
                self.sessions.pop(token,None)
                self.remote_clients.pop(token, None)
                return {'loggedOut':True}, f'{LOCAL_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'
            if method=='GET' and path=='/api/home':
                self.audit('home.loaded', mode=owner)
                return self.home(owner, self.remote_clients.get(token)),None
            if path.startswith('/api/remote/'):
                client = self.remote_client(token)
                if method=='GET' and path=='/api/remote/home':
                    return client.home(self.reference[0]), None
                if method=='GET' and path=='/api/remote/uploads':
                    rows = self.db.execute("SELECT u.* FROM remote_uploads u LEFT JOIN runs r ON r.id=u.run_id AND r.owner=u.owner WHERE u.owner=? AND u.state<>'superseded' AND (u.state<>'draft' OR u.run_id IS NULL OR (r.id IS NOT NULL AND r.deleted_at IS NULL)) ORDER BY u.created_at DESC,u.rowid DESC LIMIT 30", (owner,)).fetchall()
                    return [self.upload_summary(row) for row in rows], None
                if method=='POST' and path=='/api/remote/uploads/prepare':
                    client.home(self.reference[0])
                    return self.remote_draft(payload, owner, client), None
                pieces = path.split('/')
                if len(pieces)==6 and pieces[3]=='uploads' and pieces[5]=='verify' and method=='POST':
                    return self.verify_remote_record(pieces[4], owner, client), None
                if len(pieces)==6 and pieces[3]=='uploads' and pieces[5]=='commit' and method=='POST':
                    return self.commit_remote(pieces[4], owner, client), None
            if method=='POST' and path=='/api/trajectories/generate':
                return self.generate_trajectory(payload, self.remote_clients.get(token)), None
            if method=='POST' and path=='/api/trajectories/save':
                return self.save_generated(payload, owner, self.remote_clients.get(token)), None
            if method=='POST' and path=='/api/preview':
                try:
                    plan=generate(self.reference,payload);plan['regionCheck']=evaluate(plan['points'],self.region_for(self.remote_clients.get(token)))
                    return plan,None
                except ValueError as e:raise APIError(400,'INVALID_SETTINGS',str(e)) from None
            if method=='POST' and path=='/api/runs':return self.start(payload,owner,self.region_for(self.remote_clients.get(token))),None
            if path.startswith('/api/runs/'):
                pieces=path.split('/');run_id=pieces[3]
                if len(pieces)==5 and pieces[4]=='delete' and method=='POST':
                    return self.delete_record(run_id,owner),None
                if len(pieces)==5 and pieces[4]=='restore' and method=='POST':
                    return self.restore_record(run_id,owner),None
                row=self.get_run(run_id,owner)
                if len(pieces)==4 and method=='GET':return self.status(row),None
                if len(pieces)==5 and pieces[4]=='plan' and method=='GET':return json.loads(row['plan_json']),None
                if len(pieces)==5 and pieces[4]=='upload' and method=='POST':
                    return self.upload(run_id,payload,owner,headers.get('X-Idempotency-Key')),None
                if len(pieces)==5 and pieces[4]=='remote-prepare' and method=='POST':
                    return self.prepare_simulated_remote(run_id,owner,self.remote_client(token)),None
                if len(pieces)==5 and pieces[4]=='remote-upload' and method=='POST':
                    return self.upload_saved_record(run_id,owner,self.remote_client(token)),None
                if len(pieces)==5 and pieces[4]=='cancel' and method=='POST':
                    if row['state']!='running':raise APIError(409,'NOT_CANCELLABLE','此记录不能取消')
                    self.db.execute("UPDATE runs SET state='cancelled' WHERE id=?",(run_id,));self.db.commit()
                    self.audit('run.cancelled',run_id=run_id)
                    return {'state':'cancelled'},None
                if len(pieces)==5 and pieces[4]=='export' and method=='GET':
                    return {'isSimulated':True,'productionSubmitted':bool(row['result_json'] and json.loads(row['result_json']).get('productionSubmitted')),'run':self.status(row),
                            'plan':json.loads(row['plan_json']),
                            'uploaded':json.loads(row['uploaded_json']) if row['uploaded_json'] else None,
                            'result':json.loads(row['result_json']) if row['result_json'] else None},None
            if method=='GET' and path=='/api/events':
                rows=self.db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 80').fetchall()
                return [{'at':r['at'],'action':r['action'],'runId':r['run_id'],'status':r['status'],'summary':json.loads(r['summary_json'])} for r in rows],None
            raise APIError(404,'NOT_FOUND','接口不存在')

class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, port, app):
        self.app=app
        super().__init__(('127.0.0.1',port),Handler)

class Handler(BaseHTTPRequestHandler):
    server_version='LocalRunMVP/1.0'
    def log_message(self,*args): pass
    def reply(self,status,body,content_type='application/json; charset=utf-8',cookie=None):
        data=json.dumps(body,ensure_ascii=False,allow_nan=False).encode() if isinstance(body,(dict,list)) else body
        self.send_response(status)
        for k,v in {'Content-Type':content_type,'Content-Length':str(len(data)),'Cache-Control':'no-store',
                    'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
                    'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"}.items():self.send_header(k,v)
        if cookie:self.send_header('Set-Cookie',cookie)
        self.end_headers();self.wfile.write(data)
    def handle_request(self,method):
        port=self.server.server_port;allowed={f'127.0.0.1:{port}',f'localhost:{port}'}
        if self.headers.get('Host') not in allowed:
            self.reply(403,{'code':'LOCAL_HOST_REQUIRED','message':'仅允许本机访问'});return
        origin=self.headers.get('Origin')
        if origin and origin not in {'http://'+host for host in allowed}:
            self.reply(403,{'code':'ORIGIN_BLOCKED','message':'拒绝跨站请求'});return
        path=urlsplit(self.path).path
        static={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8')}
        if method=='GET' and path in static:
            name,mime=static[path];self.reply(200,(getattr(self.server.app,'web_dir',BASE/'web')/name).read_bytes(),mime);return
        if not path.startswith('/api/'):
            self.reply(404,{'code':'NOT_FOUND','message':'不存在'});return
        payload={}
        try:
            if method=='POST':
                if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise APIError(415,'JSON_REQUIRED','仅接受JSON')
                try:size=int(self.headers.get('Content-Length','0'))
                except ValueError:raise APIError(400,'BAD_LENGTH','请求长度错误') from None
                if size<0 or size>2_000_000:raise APIError(413,'BODY_TOO_LARGE','请求体超限')
                def reject_constant(v):raise ValueError('Nonfinite JSON')
                try:payload=json.loads(self.rfile.read(size) or b'{}',parse_constant=reject_constant)
                except (ValueError,UnicodeError):raise APIError(400,'BAD_JSON','无效JSON') from None
            cookies=SimpleCookie();cookies.load(self.headers.get('Cookie',''))
            if method=='POST' and path=='/api/admin/shutdown':
                if not hmac.compare_digest(self.headers.get('X-Local-Control',''),self.server.app.control_key):
                    raise APIError(403,'LOCAL_CONTROL_REQUIRED','缺少本机控制凭据')
                self.reply(200,{'code':0,'message':'本地服务正在停止','data':None})
                threading.Thread(target=self.server.shutdown,daemon=True).start()
                return
            token=cookies[LOCAL_COOKIE].value if LOCAL_COOKIE in cookies else ''
            result,cookie=self.server.app.dispatch(method,path,payload,token,self.headers)
            self.reply(200,{'code':0,'message':'OK','data':result},cookie=cookie)
        except APIError as e:self.reply(e.status,{'code':e.code,'message':e.message,'details':e.details})
        except RemoteError as e:self.reply(401 if e.code in ('UPSTREAM_AUTH_REJECTED','LOGIN_REJECTED') else 429 if e.code=='LOGIN_RATE_LIMIT' else 502,
                                          {'code':e.code,'message':e.message,'details':e.public()})
        except Exception:
            self.reply(500,{'code':'LOCAL_INTERNAL_ERROR','message':'本地服务内部错误'})
    def do_GET(self):self.handle_request('GET')
    def do_POST(self):self.handle_request('POST')
    def do_OPTIONS(self):self.reply(403,{'code':'CORS_DISABLED','message':'不接受跨站调用'})

if __name__=='__main__':
    parser=argparse.ArgumentParser(description='本地跑步流程验证；固定监听127.0.0.1。')
    parser.add_argument('--port',type=int,default=18766)
    parser.add_argument('--data-dir',type=Path,default=BASE/'.local')
    args=parser.parse_args()
    app=Application(args.data_dir);server=Server(args.port,app)
    (args.data_dir/'runtime.json').write_text(json.dumps({'port':server.server_port}), 'utf-8')
    print(f'Local MVP: http://127.0.0.1:{server.server_port}',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close();app.db.close()
