import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from account_auth import password_login
from app import APIError, Server
from credential_store import dpapi
from login_protocol import decrypt_bytes, encrypt_bytes, LOGIN_URL
from remote import RemoteClient, RemoteError, REFRESH_ENDPOINT, UPLOAD_ENDPOINT, save_credentials
from server_v3 import Application
from test_app import FakeClock
from trajectory import generate_secure


class PoolTransport:
    def __init__(self):
        self.calls = []
        self.uploads = []
        self.fail_upload = False
        self.fail_reads = False
        self.expire = set()
        self.rule_overrides = {}
        self.record_overrides = {}

    def __call__(self, method, url, headers, body):
        path = urlsplit(url).path
        data = json.loads(body) if body else None
        self.calls.append((path, headers.copy(), data))
        if path == REFRESH_ENDPOINT:
            self.assert_refresh(headers, body)
            token = parse_qs(urlsplit(url).query)['refreshToken'][0]
            name = token.split('-')[0]
            return 200, json.dumps({'code':0,'data':{'accessToken':name+'-new','refreshToken':name+'-refresh-new','tokenType':'bearer','accessTokenExpires':2100000000000}}).encode()
        token = headers['Authorization'][7:]
        name = token.split('-')[0]
        if token in self.expire or token.endswith('-old'):
            return 401, b'{"error":"invalid_token"}'
        if self.fail_reads and path != UPLOAD_ENDPOINT:
            return 503, b'{"code":503,"message":"unavailable"}'
        if path == UPLOAD_ENDPOINT:
            self.uploads.append((name,data))
            if self.fail_upload: raise TimeoutError()
            return 200, json.dumps({'code':0,'data':'remote-'+name}).encode()
        profile_id = ('user-'+name+'-')*5
        rows = {'/education/semester/getCurrent':{'id':'semester-current','organizationId':'school-identifier'},
                '/authorization/user/getBasicInfo':{'id':profile_id,'realName':'User '+name,'schoolName':'School','weight':60},
                '/running/app/getRunningLimit':{'hasRule':True,'limitationsGoalsSexInfoId':'goals-current','patternId':'pattern','scoringType':1,'totalDayMileage':'1.00','totalWeekMileage':'2.00','effectiveMileageStart':1,'effectiveMileageEnd':5,'semesterMileage':100,'dailyMileage':5,'weeklyMileage':35},
                '/running/app/getTotalRunning':{'totalMileage':'12.5' if name=='A' else '25.0','totalPart':3},
                '/running/app/getRunningRange':{'rangeRunningRegion':json.loads((Path(__file__).resolve().parents[1]/'fixtures/region.json').read_text('utf-8'))['polygons'],'invalidRangeRunningRegion':[]},
                '/education/course/today':[],
                '/running/app/getDetails':{'gradeDetails':[{'id':'remote-'+name,'totalMileage':'1.307','effectiveMileage':'0','uneffectiveReason':'test server reason','scoringType':1}]}}
        if path=='/running/app/getRunningLimit':rows[path].update(self.rule_overrides.get(name,{}))
        if path=='/running/app/getDetails':rows[path]['gradeDetails'][0].update(self.record_overrides.get(name,{}))
        return 200, json.dumps({'code':0,'data':rows[path]}).encode()

    @staticmethod
    def assert_refresh(headers, body):
        assert 'Authorization' not in headers and body is None


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.transport = PoolTransport()
        self.clock = FakeClock()
        self.app = Application(self.temp.name, clock=self.clock,
                               remote_factory=lambda config:RemoteClient(config,transport=self.transport))

    def tearDown(self):
        self.app.db.close(); self.temp.cleanup()

    def add(self,name='A',old=False):
        return self.app.add_account({'label':'Account '+name,'credentials':{'accessToken':name+('-old' if old else '-valid'),'refreshToken':name+'-refresh'}})['account']['id']

    def select(self,account_id):
        home,cookie = self.app.select(account_id)
        return cookie.split(';')[0].split('=',1)[1]

    def test_pool_is_encrypted_independent_and_never_returns_tokens(self):
        a,b=self.add('A'),self.add('B')
        self.assertEqual(len(self.app.accounts()),2)
        self.assertEqual(self.app.home(a)['account']['metrics']['totalMileageKm'],12.5)
        self.assertEqual(self.app.home(b)['account']['metrics']['totalMileageKm'],25)
        for account_id in (a,b):
            raw=self.app.credential_path(account_id).read_bytes()
            self.assertNotIn(b'Bearer',raw)
            self.assertIn('Authorization',json.loads(dpapi(raw,decrypt=True)))
        public=json.dumps(self.app.accounts())+'\n'.join(self.app.db.iterdump())
        self.assertNotIn('A-valid',public)
        self.assertNotIn('A-refresh',public)

    def test_same_account_updates_existing_entry(self):
        a=self.add('A')
        again=self.add('A')
        self.assertEqual(a,again)
        self.assertEqual(len(self.app.accounts()),1)

    def test_expired_account_rotates_both_encrypted_tokens(self):
        a=self.add('A',old=True)
        config=self.app.config(a)
        self.assertEqual(config['Authorization'],'Bearer A-new')
        self.assertEqual(config['refreshToken'],'A-refresh-new')
        self.assertEqual(sum(call[0]==REFRESH_ENDPOINT for call in self.transport.calls),1)
        self.assertEqual(len(list(self.app.credential_path(a).parent.glob('credential-checkpoints/*.dpapi'))),2)

    def test_use_refreshes_mileage_and_deleted_account_cannot_be_used(self):
        a=self.add('A'); token=self.select(a)
        before=len(self.transport.calls); self.app.select(a)
        self.assertGreater(len(self.transport.calls),before)
        self.app.dispatch('POST',f'/api/accounts/{a}/delete',{},token,{})
        self.assertEqual(self.app.accounts(),[])
        with self.assertRaises(APIError): self.app.session(token)
        self.app.dispatch('POST',f'/api/accounts/{a}/restore',{},'',{})
        self.assertEqual(len(self.app.accounts()),1)

    def test_default_seed_is_random_without_clock_or_local_record(self):
        a=self.add(); self.select(a)
        first=self.app.generate(a,{'laps':3})
        second=self.app.generate(a,{'laps':3})
        self.assertNotEqual(first['points'],second['points'])
        self.assertEqual(first['pointCount'],451)
        self.assertEqual(first['entropySource'],'os-csprng')
        self.assertEqual(first['config']['seed'],-1);self.assertNotIn('clockScale',first['config'])
        self.assertEqual(self.app.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0],0)
        self.assertFalse((Path(self.temp.name)/'runs.sqlite3').exists())
        with self.assertRaises(ValueError): generate_secure(self.app.reference,{'seed':-2})

    def test_explicit_seed_reproduces_track_and_random_seed_does_not(self):
        for seed in (0,42,2147483647):
            with self.subTest(seed=seed):
                first=generate_secure(self.app.reference,{'laps':4,'seed':seed})
                second=generate_secure(self.app.reference,{'laps':4,'seed':seed})
                self.assertEqual(first['points'],second['points'])
                self.assertEqual(first['config']['seed'],seed)
                self.assertEqual(first['entropySource'],'seeded-prng')
        random_first=generate_secure(self.app.reference,{'seed':-1})
        random_second=generate_secure(self.app.reference,{'seed':-1})
        self.assertNotEqual(random_first['points'],random_second['points'])

    def test_invalid_seeds_are_rejected(self):
        for seed in (-2,2147483648,True,1.5,'-1',None):
            with self.subTest(seed=seed):
                with self.assertRaises(ValueError):generate_secure(self.app.reference,{'seed':seed})

    def test_selected_generation_uploads_without_saving_and_is_idempotent(self):
        a=self.add(); self.select(a)
        plan=self.app.generate(a,{'laps':3})
        self.app.generate(a,{'laps':12})
        receipt=self.app.upload(plan['generationId'],a)
        self.assertEqual(receipt['state'],'accepted')
        self.assertEqual(receipt['serverRecord']['status'],'ineffective')
        self.assertEqual(len(self.transport.uploads),1)
        self.assertEqual(len(self.transport.uploads[0][1]['routineLine']),451)
        replay=self.app.upload(plan['generationId'],a)
        self.assertTrue(replay['idempotentReplay']);self.assertEqual(len(self.transport.uploads),1)
        self.assertNotIn('routineLine','\n'.join(self.app.db.iterdump()))

    def test_account_switch_cannot_upload_another_accounts_generation(self):
        a,b=self.add('A'),self.add('B')
        plan=self.app.generate(a,{'laps':3})
        with self.assertRaises(APIError): self.app.upload(plan['generationId'],b)
        self.assertEqual(len(self.transport.uploads),0)

    def test_unknown_upload_is_not_retried_even_after_restart(self):
        a=self.add();plan=self.app.generate(a,{'laps':3});self.transport.fail_upload=True
        with self.assertRaises(RemoteError): self.app.upload(plan['generationId'],a)
        with self.assertRaises(APIError): self.app.upload(plan['generationId'],a)
        self.assertEqual(len(self.transport.uploads),1)
        self.app.db.close()
        self.app=Application(self.temp.name,clock=self.clock,remote_factory=lambda config:RemoteClient(config,transport=self.transport))
        with self.assertRaises(APIError):self.app.upload(plan['generationId'],a)
        self.assertEqual(len(self.transport.uploads),1)

    def test_generation_expiry_and_retired_routes(self):
        a=self.add();token=self.select(a);plan=self.app.generate(a,{'laps':3});self.clock.advance(1801)
        with self.assertRaises(APIError):self.app.upload(plan['generationId'],a)
        with self.assertRaises(APIError) as error:self.app.dispatch('POST','/api/runs',{},token,{})
        self.assertEqual(error.exception.status,410)

    def test_connection_failure_keeps_new_credentials_available_for_retry(self):
        self.transport.fail_reads=True
        result=self.app.add_account({'mode':'tokens','credentials':{'accessToken':'A-valid','refreshToken':'A-refresh'}})
        self.assertFalse(result['verified'])
        self.assertEqual(result['account']['state'],'error')
        self.transport.fail_reads=False
        self.select(result['account']['id'])
        self.assertEqual(self.app.accounts()[0]['state'],'ready')

    def test_http_v3_serves_new_ui_and_checks_host(self):
        server=Server(0,self.app);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
        try:
            connection.request('GET','/');response=connection.getresponse();html=response.read().decode()
            self.assertEqual(response.status,200);self.assertIn('账号池',html)
            for old_id in ('id="clock"','id="history"','id="start"'):self.assertNotIn(old_id,html)
            self.assertIn('id="seed"',html)
            connection.request('GET','/api/accounts',headers={'Host':'evil.example'});response=connection.getresponse();response.read()
            self.assertEqual(response.status,403)
        finally:connection.close();server.shutdown();server.server_close();thread.join()


class PasswordProtocolTests(unittest.TestCase):
    def test_password_login_uses_fresh_encrypted_envelope_without_auth_or_cookie(self):
        seen=[]
        def transport(method,url,headers,body):
            seen.append(url);self.assertEqual(url,LOGIN_URL);self.assertEqual(method,'POST')
            self.assertNotIn('Authorization',headers);self.assertNotIn('Cookie',headers)
            decoded=json.loads(decrypt_bytes(json.loads(body)))
            self.assertEqual(decoded['phone'],'test-user');self.assertEqual(decoded['password'],'test-secret<&>')
            self.assertEqual(len(decoded['signDigital']),40)
            t=1794300000001
            tokens={'accessToken':'A-valid','refreshToken':'A-refresh','accessTokenExpires':2100000000000,'tokenType':'bearer','privateProfile':'discard'}
            return 200,json.dumps({'code':0,'data':{'t':t,'pyd':encrypt_bytes(json.dumps(tokens).encode(),t)}}).encode()
        config=password_login('test-user','test-secret<&>',transport)
        self.assertEqual(config['Authorization'],'Bearer A-valid');self.assertNotIn('privateProfile',config)
        self.assertEqual(len(seen),1)

    def test_login_rejection_is_single_attempt_and_does_not_echo_password(self):
        calls=[]
        def transport(*args):calls.append(1);return 200,b'{"code":1,"message":"test-secret"}'
        with self.assertRaises(RemoteError) as error:password_login('test-user','test-secret',transport)
        self.assertEqual(len(calls),1);self.assertNotIn('test-secret',json.dumps(error.exception.public()))


if __name__=='__main__':unittest.main()
