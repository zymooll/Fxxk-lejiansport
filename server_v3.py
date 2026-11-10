"""Account pool and transient generate/upload workflow. No timer or run history routes."""
import argparse
import hashlib
import json
import math
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from app import APIError, BASE, Clock, LOCAL_COOKIE, Server
from account_auth import password_login
from credential_store import dpapi, checkpoint
from geometry import evaluate
from remote import RemoteClient, RemoteError, credential_config, save_credentials
from signing import build_simulation
from trajectory import generate_secure, secure_settings, length, MIN_LAPS, MAX_LAPS, LAP_DURATION

SERVICE = 'run-mvp-accounts-v3'
BEIJING = timezone(timedelta(hours=8))


def mileage(value):
    if isinstance(value, bool) or value is None: return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError): return None


def km(value):
    try:
        number = float(value or 0)
        return number if math.isfinite(number) and number >= 0 else 0
    except (ValueError, TypeError): return 0


class Application:
    def __init__(self, data_dir, *, clock=None, remote_factory=None, login_factory=None):
        self.data_dir = Path(data_dir)
        self.web_dir = BASE/'web_v3'
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.account_dir = self.data_dir/'accounts'
        self.account_dir.mkdir(exist_ok=True)
        self.clock = clock or Clock()
        self.lock = threading.RLock()
        self.remote_factory = remote_factory or RemoteClient
        self.login_factory = login_factory or (lambda phone, password: password_login(
            phone, password, diagnostic_file=self.data_dir/'login-diagnostics.redacted.jsonl'))
        self.reference = json.loads(((self.data_dir/'reference.json') if (self.data_dir/'reference.json').is_file() else BASE/'fixtures/reference.json').read_text('utf-8'))
        self.region = json.loads(((self.data_dir/'region.json') if (self.data_dir/'region.json').is_file() else BASE/'fixtures/region.json').read_text('utf-8'))
        self.db = sqlite3.connect(self.data_dir/'accounts.sqlite3', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, owner TEXT UNIQUE, label TEXT NOT NULL,
            name TEXT, school TEXT, state TEXT NOT NULL, metrics_json TEXT NOT NULL DEFAULT '{}',
            error_json TEXT, created_at TEXT NOT NULL, checked_at TEXT, deleted_at TEXT);
          CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
            state TEXT NOT NULL, body_hash TEXT NOT NULL, receipt_json TEXT NOT NULL, updated_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS auto_batches(id TEXT PRIMARY KEY, state TEXT NOT NULL,
            created_at TEXT NOT NULL, settings_json TEXT NOT NULL, items_json TEXT NOT NULL);
        ''')
        if 'target_mileage_km' not in {row['name'] for row in self.db.execute('PRAGMA table_info(accounts)')}:
            self.db.execute('ALTER TABLE accounts ADD COLUMN target_mileage_km REAL')
        self.db.execute("UPDATE attempts SET state='unknown' WHERE state='sending'")
        for row in self.db.execute("SELECT * FROM auto_batches WHERE state='running'").fetchall():
            items = json.loads(row['items_json'])
            for item in items:
                if item['state'] not in ('checking','sending'): continue
                attempt = self.attempt(item.get('generationId',''), item['accountId'])
                if attempt and attempt['state'] == 'accepted':
                    item.update(state='accepted', receipt=self.receipt(attempt), reason='已保存服务器回执')
                else:
                    item.update(state='unknown' if attempt and attempt['state']=='unknown' else 'failed',
                                reason='服务重启中断，未自动重发')
            self.db.execute("UPDATE auto_batches SET state='paused',items_json=? WHERE id=?", (json.dumps(items),row['id']))
        self.db.commit()
        self.clients, self.sessions, self.generations = {}, {}, {}
        self.login_attempts = []
        key = self.data_dir/'control.key'
        if not key.exists(): key.write_text(secrets.token_urlsafe(32), 'ascii')
        self.control_key = key.read_text('ascii').strip()

    def account(self, account_id, include_deleted=False):
        row = self.db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
        if not row or (row['deleted_at'] and not include_deleted):
            raise APIError(404, 'ACCOUNT_NOT_FOUND', '账号不存在或已删除')
        return row

    def credential_path(self, account_id):
        # Only database-generated IDs can reach a filesystem path.
        self.account(account_id, include_deleted=True)
        return self.account_dir/account_id/'credentials.dpapi'

    def config(self, account_id):
        try:
            return credential_config(json.loads(dpapi(self.credential_path(account_id).read_bytes(), decrypt=True)))
        except (OSError, ValueError):
            raise APIError(400, 'ACCOUNT_CREDENTIALS_UNAVAILABLE', '无法读取此账号的加密凭据，请重新添加') from None

    def public_account(self, row):
        metrics = json.loads(row['metrics_json'])
        override = row['target_mileage_km']
        target = override if override is not None else metrics.get('semesterTargetMileageKm')
        return dict(id=row['id'], label=row['label'], name=row['name'], school=row['school'], state=row['state'],
                    metrics=metrics, targetMileageKm=target, targetSource='custom' if override is not None else 'semester',
                    remainingMileageKm=max(0,target-metrics.get('totalMileageKm',0)) if target is not None else None,
                    lastCheckedAt=row['checked_at'],
                    error=json.loads(row['error_json']) if row['error_json'] else None)

    def accounts(self):
        return [self.public_account(row) for row in self.db.execute('SELECT * FROM accounts WHERE deleted_at IS NULL ORDER BY created_at,id')]

    def client(self, account_id):
        row = self.account(account_id)
        if account_id not in self.clients:
            client = self.remote_factory(self.config(account_id))
            client.bind_credentials_file(self.credential_path(account_id), encrypted=True)
            client.owner = row['owner']
            self.clients[account_id] = client
        return self.clients[account_id]

    def refresh_account(self, account_id):
        row = self.account(account_id)
        client = self.client(account_id)
        try:
            expires = self.config(account_id).get('accessTokenExpires')
            if expires and expires <= time.time()*1000+30_000:
                client.refresh_credentials()
            snapshot = client.home(self.reference[0])
            other = self.db.execute('SELECT id FROM accounts WHERE owner=? AND id<>?', (client.owner, account_id)).fetchone()
            if other:
                canonical = other['id']
                path = self.credential_path(canonical)
                if path.exists(): checkpoint(path, dpapi(path.read_bytes(), decrypt=True), 'before-account-update')
                save_credentials(path, client._config, encrypted=True)
                self.db.execute('UPDATE accounts SET deleted_at=NULL,label=? WHERE id=?', (row['label'], canonical))
                self.db.execute('UPDATE accounts SET deleted_at=? WHERE id=?', (self.clock.now(), account_id))
                client.bind_credentials_file(path, encrypted=True)
                self.clients.pop(account_id, None)
                self.clients[canonical] = client
                account_id = canonical
            totals, limit = snapshot['totals'], snapshot['runningLimit']
            metrics = dict(totalMileageKm=km(totals.get('totalMileage')), totalPart=km(totals.get('totalPart')),
                           dayMileageKm=km(limit.get('totalDayMileage')), weekMileageKm=km(limit.get('totalWeekMileage')),
                           semesterTargetMileageKm=mileage(limit.get('semesterMileage')),
                           dailyLimitKm=mileage(limit.get('dailyMileage')), weeklyLimitKm=mileage(limit.get('weeklyMileage')),
                           semester=snapshot['semester'], accessTokenExpires=client._config.get('accessTokenExpires'))
            self.db.execute("UPDATE accounts SET owner=?,name=?,school=?,state='ready',metrics_json=?,error_json=NULL,checked_at=? WHERE id=?",
                            (client.owner, client.profile.get('realName') or client.profile.get('nickName') or '账号',
                             client.profile.get('schoolName') or '', json.dumps(metrics), self.clock.now(), account_id))
            self.db.commit()
            return account_id
        except RemoteError as error:
            self.db.execute("UPDATE accounts SET state=?,error_json=?,checked_at=? WHERE id=?",
                            ('needs_login' if error.code == 'UPSTREAM_AUTH_REJECTED' else 'error',
                             json.dumps(error.public()), self.clock.now(), account_id))
            self.db.commit()
            raise

    def add_account(self, payload, *, local=False):
        if not isinstance(payload, dict): raise APIError(400, 'INVALID_ACCOUNT', '账号参数必须是对象')
        label = payload.get('label', '').strip() if isinstance(payload.get('label', ''), str) else ''
        if len(label) > 80: raise APIError(400, 'INVALID_LABEL', '备注最多80个字符')
        mode = payload.get('mode', 'tokens')
        if local:
            try: config = credential_config(json.loads((self.data_dir/'credentials.private.json').read_text('utf-8-sig')))
            except (OSError, ValueError): raise APIError(400, 'LOCAL_CREDENTIALS_UNAVAILABLE', '没有可导入的本机凭据') from None
        elif mode == 'password':
            now = self.clock.monotonic()
            self.login_attempts = [at for at in self.login_attempts if now-at < 60]
            if len(self.login_attempts) >= 5: raise APIError(429, 'LOGIN_RATE_LIMIT', '登录请求过于频繁，请稍后重试')
            self.login_attempts.append(now)
            try: config = self.login_factory(payload.get('phone'), payload.get('password'))
            except ValueError: raise APIError(400, 'INVALID_LOGIN', '请输入有效账号和密码') from None
        elif mode == 'tokens':
            try: config = credential_config(payload.get('credentials'))
            except ValueError as error: raise APIError(400, 'INVALID_CREDENTIALS', str(error)) from None
        else: raise APIError(400, 'INVALID_ACCOUNT_MODE', '请选择密码登录或双 Token 导入')
        account_id = str(uuid.uuid4())
        self.db.execute("INSERT INTO accounts(id,label,state,created_at) VALUES(?,?,'pending',?)", (account_id, label or '新账号', self.clock.now()))
        self.db.commit()
        try:
            save_credentials(self.credential_path(account_id), config, encrypted=True)
            account_id = self.refresh_account(account_id)
            return {'account': self.public_account(self.account(account_id)), 'verified': True}
        except RemoteError as error:
            return {'account': self.public_account(self.account(account_id)), 'verified': False, 'error': error.public()}
        except (OSError, ValueError):
            self.db.execute('DELETE FROM accounts WHERE id=?', (account_id,)); self.db.commit()
            raise APIError(500, 'ACCOUNT_SAVE_FAILED', '无法加密保存账号凭据') from None

    def session(self, token):
        value = self.sessions.get(token)
        if not value or value[1] <= self.clock.monotonic():
            self.sessions.pop(token, None)
            raise APIError(401, 'LOCAL_SESSION_REQUIRED', '请选择一个账号')
        self.account(value[0])
        return value[0]

    def select(self, account_id):
        self.account(account_id)
        # An explicit selection can retry a previously failed connection once.
        if account_id in self.clients and self.clients[account_id]._refresh_error:
            self.clients.pop(account_id)
        account_id = self.refresh_account(account_id)
        token = secrets.token_urlsafe(32)
        self.sessions[token] = (account_id, self.clock.monotonic()+8*3600)
        return self.home(account_id), f'{LOCAL_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800'

    def home(self, account_id):
        client = self.client(account_id)
        live = client.snapshot.get('runningRange') if client.snapshot else None
        region = dict(polygons=live.get('rangeRunningRegion') or [], excludedPolygons=live.get('invalidRangeRunningRegion') or []) if live else self.region
        return dict(account=self.public_account(self.account(account_id)), region=region, reference=self.reference,
                    runningLimit=client.snapshot['runningLimit'] if client.snapshot else {},
                    trajectory=dict(lapsMin=MIN_LAPS,lapsMax=MAX_LAPS,referenceLapMeters=length(self.reference)))

    def generate(self, account_id, payload):
        try: plan = generate_secure(self.reference, payload)
        except ValueError as error: raise APIError(400, 'INVALID_SETTINGS', str(error)) from None
        region = self.home(account_id)['region']
        plan['regionCheck'] = evaluate(plan['points'], region)
        now = self.clock.monotonic()
        for key, item in list(self.generations.items()):
            if now-item['at'] > 1800: self.generations.pop(key)
        while len(self.generations) >= 32: self.generations.pop(next(iter(self.generations)))
        generation_id = str(uuid.uuid4())
        self.generations[generation_id] = dict(accountId=account_id, plan=plan, at=now)
        return dict(plan, generationId=generation_id, accountId=account_id)

    def attempt(self, generation_id, account_id):
        return self.db.execute('SELECT * FROM attempts WHERE id=? AND account_id=?', (generation_id, account_id)).fetchone()

    def receipt(self, row):
        return dict(json.loads(row['receipt_json']), state=row['state'], generationId=row['id'])

    def verify(self, generation_id, account_id):
        row = self.attempt(generation_id, account_id)
        if not row or row['state'] != 'accepted': raise APIError(409, 'RECEIPT_REQUIRED', '只有已收到回执的请求才能回查')
        receipt = self.receipt(row)
        try: receipt['serverRecord'] = self.client(account_id).record_status(receipt['remoteRecordId'])
        except RemoteError as error: receipt['serverRecord'] = dict(status='pending', message=error.message)
        self.db.execute('UPDATE attempts SET receipt_json=? WHERE id=?', (json.dumps(receipt), generation_id)); self.db.commit()
        return receipt

    def upload(self, generation_id, account_id, *, batch_id=None):
        previous = self.attempt(generation_id, account_id)
        if previous and previous['state'] == 'accepted': return dict(self.receipt(previous), idempotentReplay=True)
        if previous and previous['state'] in ('sending','unknown'):
            raise APIError(409, 'UPLOAD_OUTCOME_UNKNOWN', '上传结果待核对，不能再次发送')
        active = self.active_batch()
        if active and active['id'] != batch_id:
            raise APIError(409, 'AUTO_BATCH_ACTIVE', '批量上传尚未结束，请先完成或停止批次')
        generated = self.generations.get(generation_id)
        if not generated or generated['accountId'] != account_id or self.clock.monotonic()-generated['at'] > 1800:
            raise APIError(404, 'GENERATION_EXPIRED', '请为当前账号重新生成轨迹')
        plan = generated['plan']
        if not plan['regionCheck']['passed']: raise APIError(422, 'REGION_CHECK_FAILED', '区域内点数比例不足，请调整设置')
        if self.refresh_account(account_id) != account_id: raise APIError(409, 'ACCOUNT_CHANGED', '账号已合并，请重新选择')
        if batch_id:
            latest = self.automatic_plan(account_id, refresh=False)
            if latest['state'] != 'ready' or latest['laps'] != plan['config']['laps']:
                raise APIError(409, 'AUTO_PLAN_CHANGED', latest.get('reason') or '账号额度已变化，请重新计算批次')
            generated['automaticPlan'] = latest
        client = self.client(account_id)
        # Apply current coordinates to current rules; never regenerate the selected track at upload time.
        if not evaluate(plan['points'], self.home(account_id)['region'])['passed']:
            raise APIError(422, 'REGION_CHANGED', '当前可跑区域已变化，请重新生成')
        now = self.clock.now()
        try: payload = build_simulation(plan, now, client.snapshot, client.profile, now, rebase_to_upload=True)
        except ValueError as error: raise APIError(422, 'UPLOAD_NOT_READY', str(error)) from None
        body_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        receipt = dict(accountId=account_id, pointCount=plan['pointCount'], mileageKm=payload['totalMileage'],
                       startTime=payload['startTime'], endTime=payload['endTime'])
        if generated.get('automaticPlan'):
            receipt['automaticPlan'] = generated['automaticPlan']
        self.db.execute('INSERT OR REPLACE INTO attempts VALUES(?,?,?,?,?,?)',
                        (generation_id,account_id,'sending',body_hash,json.dumps(receipt),now)); self.db.commit()
        try:
            receipt['remoteRecordId'] = client.submit(payload)
        except RemoteError as error:
            state = 'unknown' if error.uncertain else 'failed'
            receipt['error'] = error.public()
            self.db.execute('UPDATE attempts SET state=?,receipt_json=? WHERE id=?', (state,json.dumps(receipt),generation_id)); self.db.commit()
            raise
        except Exception:
            self.db.execute("UPDATE attempts SET state='unknown' WHERE id=?", (generation_id,)); self.db.commit()
            raise APIError(502, 'UPLOAD_OUTCOME_UNKNOWN', '未能确认上传结果，请到官方 App 核对') from None
        self.db.execute("UPDATE attempts SET state='accepted',receipt_json=? WHERE id=?", (json.dumps(receipt),generation_id)); self.db.commit()
        result = self.verify(generation_id, account_id)
        try: self.refresh_account(account_id)
        except RemoteError: result['statisticsPending'] = True
        return result

    def set_target(self, account_id, payload):
        self.account(account_id)
        if 'targetMileageKm' not in payload:
            raise APIError(400, 'TARGET_REQUIRED', '请提供目标里程；null表示跟随学期目标')
        value = payload['targetMileageKm']
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value) or not 0 <= value <= 1_000_000):
            raise APIError(400, 'INVALID_TARGET', '目标应为0–1000000公里，或恢复学期目标')
        self.db.execute('UPDATE accounts SET target_mileage_km=? WHERE id=?', (value,account_id)); self.db.commit()
        return self.public_account(self.account(account_id))

    def auto_settings(self, payload):
        raw = payload.get('settings', {})
        if not isinstance(raw, dict) or 'laps' in raw:
            raise APIError(400, 'INVALID_AUTO_SETTINGS', '批量圈数由额度计算，请只提供偏移和种子设置')
        try:
            config = secure_settings({'laps':1, **raw})
        except ValueError as error:
            raise APIError(400, 'INVALID_SETTINGS', str(error)) from None
        config.pop('laps')
        return config

    def automatic_plan(self, account_id, *, refresh=True):
        row = self.account(account_id)
        if refresh and account_id in self.clients and self.clients[account_id]._refresh_error:
            self.clients.pop(account_id)
        if refresh and self.refresh_account(account_id) != account_id:
            raise APIError(409, 'ACCOUNT_CHANGED', '账号已合并，请重新计算')
        account = self.public_account(self.account(account_id))
        snapshot = self.client(account_id).snapshot
        limit = snapshot['runningLimit']; totals = snapshot['totals']
        total, day_used, week_used = mileage(totals.get('totalMileage')), mileage(limit.get('totalDayMileage')), mileage(limit.get('totalWeekMileage'))
        target, daily = account['targetMileageKm'], mileage(limit.get('dailyMileage'))
        today = datetime.fromisoformat(self.clock.now()).astimezone(BEIJING).date().isoformat()
        result = dict(accountId=account_id,label=row['label'],name=account['name'],state='ready',reason='待上传',
                      totalMileageKm=total,targetMileageKm=target,dailyLimitKm=daily,dayMileageKm=day_used,day=today)
        def skip(code, reason):
            return dict(result, state='skipped', code=code, reason=reason)
        if target is None: return skip('TARGET_UNAVAILABLE', '学期目标暂不可用，请读取账号或自行设置目标')
        if total is None: return skip('TOTAL_UNAVAILABLE', '当前累计里程无法确认')
        if total+1e-8 >= target: return skip('TARGET_REACHED', '已达到目标里程')
        if not limit.get('hasRule') or daily is None or day_used is None:
            return skip('QUOTA_UNAVAILABLE', '当前日额度无法确认')
        # A pending/unknown earlier write cannot be made safe by allocating a new generation ID.
        confirmed_today = 0
        for attempt in self.db.execute('SELECT * FROM attempts WHERE account_id=?', (account_id,)).fetchall():
            try: attempt_day = datetime.fromisoformat(attempt['updated_at']).astimezone(BEIJING).date().isoformat()
            except ValueError: attempt_day = today
            if attempt_day != today: continue
            if attempt['state'] in ('sending','unknown'):
                return skip('PREVIOUS_OUTCOME_UNKNOWN', '今日已有上传结果待核对，未再次提交')
            receipt = self.receipt(attempt)
            reservation = receipt.get('automaticPlan')
            if attempt['state'] != 'accepted': continue
            server = receipt.get('serverRecord', {})
            if server.get('status') not in ('effective','ineffective'):
                receipt = self.verify(attempt['id'],account_id)
                server = receipt.get('serverRecord', {})
            if server.get('status') not in ('effective','ineffective'):
                return skip('PREVIOUS_RECEIPT_PENDING', '今日已有回执待判定，未再次提交')
            if server.get('status') == 'effective':
                credited = mileage(server.get('effectiveMileageKm')) or 0
                confirmed_today += credited
                if reservation: day_used = max(day_used, reservation['dayMileageKm']+credited)
                if reservation and week_used is not None:
                    week_used = max(week_used, reservation.get('weekMileageKm',week_used)+credited)
        day_used = max(day_used,confirmed_today)
        result['dayMileageKm'] = day_used
        available = max(0, daily-day_used)
        if available <= 1e-8: return skip('DAILY_LIMIT_REACHED', '当日额度已用满')
        weekly = mileage(limit.get('weeklyMileage'))
        if weekly is not None and weekly > 0:
            if week_used is None: return skip('WEEKLY_QUOTA_UNAVAILABLE', '当前周额度无法确认')
            available = min(available, max(0,weekly-week_used))
            if available <= 1e-8: return skip('WEEKLY_LIMIT_REACHED', '本周额度已用满')
        maximum = mileage(limit.get('effectiveMileageEnd'))
        if maximum and maximum > 0: available = min(available,maximum)
        lap = length(self.reference)
        laps = math.ceil(available*1000/lap-1e-12)
        if not MIN_LAPS <= laps <= MAX_LAPS:
            return skip('LAP_LIMIT', '本次额度所需圈数超出生成范围')
        estimated = lap*laps/1000
        minimum = mileage(limit.get('effectiveMileageStart'))
        if minimum and estimated+1e-8 < minimum:
            return skip('BELOW_SINGLE_MINIMUM', '剩余额度对应的轨迹低于最低单次里程')
        end = datetime.fromisoformat(self.clock.now()).astimezone(BEIJING)
        start = end-timedelta(seconds=math.floor(laps*LAP_DURATION+.5))
        windows = (limit.get('runningTimeLimit') or {}).get('openTimes') or []
        if windows:
            start_ms = ((start.hour*60+start.minute)*60+start.second)*1000
            end_ms = ((end.hour*60+end.minute)*60+end.second)*1000
            def within(window):
                low, high = mileage(window.get('dayStartTime')), mileage(window.get('dayEndTime'))
                return low is not None and high is not None and start.date()==end.date() and low <= start_ms <= end_ms <= high
            if not any(isinstance(window,dict) and within(window) for window in windows):
                return skip('OUTSIDE_RUNNING_TIME', '当前开放时段不能覆盖本次计划时长')
        return dict(result,plannedMileageKm=round(available,6),laps=laps,estimatedMileageKm=round(estimated,6),
                    referenceLapMeters=lap,weekMileageKm=week_used or 0)

    def preview_auto(self, payload):
        config = self.auto_settings(payload)
        items = []
        for account in self.accounts():
            try: items.append(self.automatic_plan(account['id']))
            except (RemoteError,APIError) as error:
                items.append(dict(accountId=account['id'],label=account['label'],state='failed',reason=error.message))
        return dict(settings=config,items=items,preview=True)

    def active_batch(self):
        return self.db.execute("SELECT * FROM auto_batches WHERE state IN ('running','paused') ORDER BY created_at DESC LIMIT 1").fetchone()

    def batch(self, batch_id):
        row = self.db.execute('SELECT * FROM auto_batches WHERE id=?', (batch_id,)).fetchone()
        if not row: raise APIError(404,'BATCH_NOT_FOUND','批次不存在')
        return dict(id=row['id'],state=row['state'],createdAt=row['created_at'],settings=json.loads(row['settings_json']),items=json.loads(row['items_json']))

    def save_batch(self, batch):
        self.db.execute('UPDATE auto_batches SET state=?,items_json=? WHERE id=?', (batch['state'],json.dumps(batch['items']),batch['id'])); self.db.commit()

    def start_batch(self, payload):
        active = self.active_batch()
        if active: raise APIError(409,'AUTO_BATCH_ACTIVE','已有未结束批次，请继续或停止该批次')
        config = self.auto_settings(payload)
        items = [dict(accountId=account['id'],label=account['label'],state='pending',reason='等待检查') for account in self.accounts()]
        batch_id = str(uuid.uuid4())
        self.db.execute('INSERT INTO auto_batches VALUES(?,?,?,?,?)', (batch_id,'running' if items else 'completed',self.clock.now(),json.dumps(config),json.dumps(items))); self.db.commit()
        return self.batch(batch_id)

    def next_batch(self, batch_id):
        batch = self.batch(batch_id)
        if batch['state'] not in ('running','paused'): return batch
        item = next((item for item in batch['items'] if item['state']=='pending'),None)
        if item is None:
            batch['state']='completed'; self.save_batch(batch); return batch
        batch['state']='running';item.update(state='checking',reason='正在检查目标与额度');self.save_batch(batch)
        account_id=item['accountId']
        try:
            plan = self.automatic_plan(account_id)
            item.update(plan)
            if plan['state']=='ready':
                generated = self.generate(account_id,dict(batch['settings'],laps=plan['laps']))
                generation_id=generated['generationId']
                self.generations[generation_id]['automaticPlan']=plan
                item.update(generationId=generation_id,state='sending',reason='正在上传新轨迹');self.save_batch(batch)
                receipt = self.upload(generation_id,account_id,batch_id=batch_id)
                item.update(state='accepted',reason='服务器已接收，请查看有效性判定',receipt=receipt)
        except (RemoteError,APIError) as error:
            attempt=self.attempt(item.get('generationId',''),account_id)
            state='unknown' if (attempt and attempt['state']=='unknown') else 'skipped' if error.code in ('AUTO_PLAN_CHANGED','ACCOUNT_CHANGED','ACCOUNT_NOT_FOUND') else 'failed'
            item.update(state=state,reason=error.message)
            if attempt and attempt['state']=='accepted': item.update(state='accepted',receipt=self.receipt(attempt),reason='已保存服务器回执')
        except Exception:
            attempt=self.attempt(item.get('generationId',''),account_id)
            if attempt and attempt['state']=='accepted':
                item.update(state='accepted',receipt=self.receipt(attempt),reason='已保存服务器回执')
            else:item.update(state='unknown' if attempt else 'failed',reason='处理未能确认，请检查后再继续')
        if not any(item['state']=='pending' for item in batch['items']): batch['state']='completed'
        self.save_batch(batch)
        return batch

    def cancel_batch(self, batch_id):
        batch=self.batch(batch_id)
        if batch['state'] in ('running','paused'):
            for item in batch['items']:
                if item['state']=='pending':item.update(state='skipped',reason='已停止后续处理')
            batch['state']='cancelled';self.save_batch(batch)
        return batch

    def dispatch(self, method, path, payload, token, headers):
        with self.lock:
            if not isinstance(payload, dict): raise APIError(400, 'JSON_OBJECT_REQUIRED', '请求参数必须是对象')
            if method == 'GET' and path == '/api/health':
                return dict(service=SERVICE, instance=hashlib.sha256(str(BASE).encode()).hexdigest()[:12],
                            accountPool=True, localHistory=False, timer=False, entropySource='os-csprng',
                            legacyCredentialFileConfigured=(self.data_dir/'credentials.private.json').is_file()), None
            if method == 'GET' and path == '/api/accounts': return self.accounts(), None
            if path == '/api/auto-upload/current' and method == 'GET':
                row=self.active_batch()
                if not row:row=self.db.execute('SELECT * FROM auto_batches ORDER BY created_at DESC LIMIT 1').fetchone()
                return self.batch(row['id']) if row else None, None
            if path == '/api/auto-upload/preview' and method == 'POST':return self.preview_auto(payload),None
            if path == '/api/auto-upload' and method == 'POST':return self.start_batch(payload),None
            if method == 'POST' and path in ('/api/accounts','/api/accounts/import-local'):
                return self.add_account(payload, local=path.endswith('import-local')), None
            parts = path.strip('/').split('/')
            if len(parts)>=3 and parts[:2]==['api','auto-upload']:
                if len(parts)==3 and method=='GET':return self.batch(parts[2]),None
                if len(parts)==4 and method=='POST':
                    if parts[3]=='next':return self.next_batch(parts[2]),None
                    if parts[3]=='cancel':return self.cancel_batch(parts[2]),None
            if len(parts) == 4 and parts[:2] == ['api','accounts'] and method == 'POST':
                account_id, action = parts[2:]
                self.account(account_id, include_deleted=action=='restore')
                if action == 'use': return self.select(account_id)
                if action == 'target':return self.set_target(account_id,payload),None
                if action in ('delete','restore'):
                    self.db.execute('UPDATE accounts SET deleted_at=? WHERE id=?', (self.clock.now() if action=='delete' else None,account_id)); self.db.commit()
                    self.clients.pop(account_id, None)
                    if action == 'delete':
                        self.sessions = {key: value for key,value in self.sessions.items() if value[0] != account_id}
                        self.generations = {key:value for key,value in self.generations.items() if value['accountId'] != account_id}
                    return dict(id=account_id, deleted=action=='delete'), None
            if method == 'POST' and path == '/api/auth/logout':
                self.sessions.pop(token, None)
                return {}, f'{LOCAL_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'
            if method == 'GET' and path == '/api/session': return self.home(self.session(token)), None
            account_id = self.session(token)
            if method == 'POST' and path == '/api/trajectories/generate': return self.generate(account_id, payload), None
            if len(parts) == 4 and parts[:2] == ['api','trajectories'] and method == 'POST':
                generation_id, action = parts[2:]
                if action == 'upload': return self.upload(generation_id,account_id), None
                if action == 'verify': return self.verify(generation_id,account_id), None
            raise APIError(410, 'WORKFLOW_RETIRED', '计时与本地记录接口已移除，请使用直接生成流程')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18766)
    parser.add_argument('--data-dir', type=Path, default=BASE/'.local')
    args = parser.parse_args()
    app = Application(args.data_dir)
    server = Server(args.port, app)
    (args.data_dir/'runtime.json').write_text(json.dumps({'port':server.server_port}), 'utf-8')
    print(f'Account workbench: http://127.0.0.1:{server.server_port}', flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close(); app.db.close()
