import copy,http.client,json,math,sys,tempfile,threading,unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Application,APIError,Server,credential_verifier,matches
from trajectory import generate,distance,length,INTERVAL
from geometry import evaluate,inside_polygon

class FakeClock:
    def __init__(self):self.value=0
    def monotonic(self):return self.value
    def now(self):return (datetime(2026,11,5,tzinfo=timezone.utc)+timedelta(seconds=self.value)).isoformat()
    def advance(self,value):self.value+=value

TEST_HEADERS={'Authorization':'Bearer test-credential-not-production','Cookie':'JSESSIONID=TEST_SESSION_NOT_PRODUCTION'}

class MVPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.clock=FakeClock()
        self.app=Application(self.temp.name,clock=self.clock,verifier=credential_verifier(TEST_HEADERS))
        self.token,self.owner=self.app.login({},demo=True)
    def tearDown(self):self.app.db.close();self.temp.cleanup()
    def run_ready(self):
        data=self.app.start({'laps':1,'clockScale':60},self.owner)
        self.clock.advance(data['plan']['durationSeconds']/60+.001)
        return data
    def body(self,data):return {'isSimulated':True,'referenceHash':self.app.reference_hash,'points':data['plan']['points']}
    def error(self,code,action):
        with self.assertRaises(APIError) as context:action()
        self.assertEqual(context.exception.code,code)
        return context.exception

    def test_local_auth_needs_both_fields_and_does_not_persist_secrets(self):
        token,owner=self.app.login(TEST_HEADERS)
        self.assertEqual(owner,'configured');self.assertEqual(self.app.auth(token),'configured')
        bad=dict(TEST_HEADERS,Cookie='JSESSIONID=WRONG')
        self.error('LOCAL_AUTH_MISMATCH',lambda:self.app.login(bad))
        dump='\n'.join(self.app.db.iterdump())
        self.assertNotIn(TEST_HEADERS['Authorization'],dump);self.assertNotIn('TEST_SESSION_NOT_PRODUCTION',dump)
    def test_unauthenticated_and_expired_sessions(self):
        self.error('LOCAL_SESSION_REQUIRED',lambda:self.app.auth(''))
        self.clock.advance(8*3600+1)
        self.error('LOCAL_SESSION_REQUIRED',lambda:self.app.auth(self.token))
    def test_noise_is_reproducible_bounded_and_anchor_preserved(self):
        config={'laps':3,'seed':123,'sigmaMeters':.6,'maxOffsetMeters':2}
        a=generate(self.app.reference,config);b=generate(self.app.reference,config)
        self.assertEqual(a,b);self.assertEqual(a['pointCount'],451)
        self.assertEqual(a['points'][0]['latitude'],self.app.reference[0]['latitude'])
        self.assertEqual(a['points'][0]['longitude'],self.app.reference[0]['longitude'])
        for i,p in enumerate(a['points']):self.assertLessEqual(distance(p,self.app.reference[i%150]),2.001)
        zero=generate(self.app.reference,{'laps':1,'sigmaMeters':0,'maxOffsetMeters':0})
        self.assertAlmostEqual(zero['observedDistanceMeters'],zero['referenceDistanceMeters'],places=6)
    def test_early_upload_is_rejected_by_server_clock(self):
        data=self.app.start({'clockScale':60,'laps':1},self.owner)
        self.error('TOO_EARLY',lambda:self.app.upload(data['run']['id'],self.body(data),self.owner,'key-early-test'))
        self.assertIsNone(self.app.get_run(data['run']['id'],self.owner)['result_json'])
    def test_home_totals_include_records_beyond_recent_history(self):
        for i in range(21):
            data=self.run_ready()
            self.app.upload(data['run']['id'],self.body(data),self.owner,f'key-total-{i}')
        home=self.app.home(self.owner)
        self.assertEqual(len(home['history']),20)
        self.assertEqual(home['summary']['localRuns'],21)
    def test_complete_flow_and_idempotent_upload(self):
        home=self.app.home(self.owner);self.assertEqual(home['source'],'local-fixture')
        data=self.run_ready();self.assertEqual(self.app.status(self.app.get_run(data['run']['id'],self.owner))['state'],'ready')
        first=self.app.upload(data['run']['id'],self.body(data),self.owner,'key-complete-test')
        second=self.app.upload(data['run']['id'],self.body(data),self.owner,'key-complete-test')
        self.assertTrue(first['acceptedForLocalTest']);self.assertFalse(first['productionSubmitted']);self.assertTrue(second['idempotentReplay'])
        self.assertEqual(self.app.home(self.owner)['summary']['localRuns'],1)
        self.error('ALREADY_UPLOADED',lambda:self.app.upload(data['run']['id'],self.body(data),self.owner,'key-different'))
    def test_off_reference_curve_is_accepted_inside_region(self):
        data=self.run_ready();point=copy.deepcopy(self.app.reference[0])
        # Arbitrary un-timed coordinates are accepted: there is no seed/shape/point-count matching.
        points=[dict(point),dict(point),dict(point)]
        result=self.app.upload(data['run']['id'],{'isSimulated':True,'points':points},self.owner,'key-off-curve')
        self.assertEqual(result['pointCount'],3);self.assertTrue(result['region']['passed'])
        exported,_=self.app.dispatch('GET',f'/api/runs/{data["run"]["id"]}/export',{},self.token,{})
        self.assertEqual(exported['uploaded']['points'],points)
    def test_exact_70_percent_is_accepted_and_69_percent_rejected(self):
        inside=dict(self.app.reference[0]);outside={'latitude':0.0,'longitude':0.0}
        a=self.run_ready()
        body={'isSimulated':True,'points':[inside]*69+[outside]*31}
        error=self.error('REGION_THRESHOLD_FAILED',lambda:self.app.upload(a['run']['id'],body,self.owner,'key-region-limit'))
        self.assertEqual(error.details['region']['insidePoints'],69)
        body['points']=[inside]*70+[outside]*30
        result=self.app.upload(a['run']['id'],body,self.owner,'key-region-limit')
        self.assertEqual(result['region']['ratio'],.7)
    def test_polygon_boundary_and_concavity(self):
        poly=[{'longitude':x,'latitude':y} for x,y in [(0,0),(2,0),(2,1),(1,1),(1,2),(0,2)]]
        self.assertTrue(inside_polygon({'longitude':0,'latitude':1},poly))
        self.assertTrue(inside_polygon({'longitude':.5,'latitude':1.5},poly))
        self.assertFalse(inside_polygon({'longitude':1.5,'latitude':1.5},poly))
    def test_malformed_coordinates_and_reversed_times_rejected(self):
        data=self.run_ready();body=self.body(data);body['points']=copy.deepcopy(body['points'])
        body['points'][3]['latitude']=float('nan')
        self.error('VALIDATION_FAILED',lambda:self.app.upload(data['run']['id'],body,self.owner,'key-malformed'))
        body=self.body(data);body['points']=copy.deepcopy(body['points']);body['points'][3]['elapsedMs']=0
        self.error('VALIDATION_FAILED',lambda:self.app.upload(data['run']['id'],body,self.owner,'key-bad-time'))
    def test_missing_simulation_label_rejected(self):
        data=self.run_ready();body=self.body(data);body['isSimulated']=False
        self.error('VALIDATION_FAILED',lambda:self.app.upload(data['run']['id'],body,self.owner,'key-real-label'))
    def test_only_one_active_run_per_local_identity(self):
        self.app.start({},self.owner)
        self.error('RUN_ALREADY_ACTIVE',lambda:self.app.start({},self.owner))
    def test_other_identity_cannot_access_run(self):
        data=self.app.start({},self.owner)
        self.error('RUN_NOT_FOUND',lambda:self.app.get_run(data['run']['id'],'configured'))
    def test_cancel_and_restart_do_not_allow_upload(self):
        data=self.app.start({'laps':1},self.owner)
        self.app.dispatch('POST',f'/api/runs/{data["run"]["id"]}/cancel',{},self.token,{})
        self.clock.advance(1000)
        self.error('RUN_NOT_RUNNING',lambda:self.app.upload(data['run']['id'],self.body(data),self.owner,'key-cancelled'))
        data=self.app.start({'laps':1},self.owner)
        self.app.db.close();self.app=Application(self.temp.name,clock=self.clock)
        self.assertEqual(self.app.get_run(data['run']['id'],self.owner)['state'],'interrupted')
    def test_invalid_settings(self):
        for cfg in ({'laps':True},{'sigmaMeters':20},{'clockScale':1000},{'maxOffsetMeters':-.1}):
            self.error('INVALID_SETTINGS',lambda:self.app.start(cfg,self.owner))

    def test_instant_generation_does_not_start_or_complete_a_timed_run(self):
        timed=self.app.start({'laps':1,'clockScale':1},self.owner)
        before=self.clock.monotonic()
        plan,_=self.app.dispatch('POST','/api/trajectories/generate',{'laps':12},self.token,{})
        self.assertEqual(plan['executionMode'],'generate-only')
        self.assertTrue(plan['timingSkipped']);self.assertTrue(plan['isSimulated'])
        self.assertEqual(plan['realWaitSeconds'],0);self.assertFalse(plan['productionSubmitted'])
        self.assertEqual(plan['pointCount'],1801)
        self.assertEqual(self.clock.monotonic(),before)
        self.assertEqual(self.app.db.execute('SELECT count(*) FROM runs').fetchone()[0],1)
        self.assertEqual(self.app.db.execute('SELECT count(*) FROM remote_uploads').fetchone()[0],0)
        self.error('TOO_EARLY',lambda:self.app.upload(timed['run']['id'],self.body(timed),self.owner,'still-need-real-timing'))

    def test_lap_estimates_and_maximum_point_budget(self):
        lap_meters=length(self.app.reference)
        for laps in (1,3,12,60):
            plan=self.app.generate_trajectory({'laps':laps,'sigmaMeters':0,'maxOffsetMeters':0})
            self.assertEqual(plan['pointCount'],150*laps+1)
            self.assertLessEqual(plan['pointCount'],10000)
            self.assertEqual(plan['estimatedDistanceKm'],round(lap_meters*laps/1000,3))
            self.assertAlmostEqual(plan['observedDistanceMeters'],plan['referenceDistanceMeters'],places=6)
        for laps in (0,61,3.5,True,'12'):
            self.error('INVALID_SETTINGS',lambda:self.app.generate_trajectory({'laps':laps}))
        self.assertEqual(self.app.home(self.owner)['rules']['lapsMax'],60)

class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.app=Application(self.temp.name)
        self.server=Server(0,self.app);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.app.db.close();self.temp.cleanup()
    def request(self,method,path,body=None,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        c.request(method,path,body=body,headers=headers or {});r=c.getresponse();data=r.read();result=(r.status,dict(r.getheaders()),data);c.close();return result
    def test_http_static_auth_and_loopback_only(self):
        self.assertEqual(self.server.server_address[0],'127.0.0.1')
        status,headers,body=self.request('GET','/');self.assertEqual(status,200);self.assertIn(b'LOCAL MVP',body)
        self.assertIn("connect-src 'self'",headers['Content-Security-Policy'])
        self.assertEqual(self.request('GET','/api/home')[0],401)
        status,headers,_=self.request('POST','/api/auth/demo',b'{}',{'Content-Type':'application/json'})
        self.assertEqual(status,200);self.assertIn('HttpOnly',headers['Set-Cookie'])
        self.assertEqual(self.request('GET','/api/home',headers={'Cookie':headers['Set-Cookie'].split(';')[0]})[0],200)
    def test_cross_origin_host_bad_json_and_body_limit(self):
        self.assertEqual(self.request('POST','/api/auth/demo',b'{}',{'Content-Type':'application/json','Origin':'https://untrusted.invalid'})[0],403)
        self.assertEqual(self.request('GET','/api/health',headers={'Host':'untrusted.invalid'})[0],403)
        self.assertEqual(self.request('POST','/api/auth/demo',b'{"x":NaN}',{'Content-Type':'application/json'})[0],400)
        self.assertEqual(self.request('POST','/api/auth/demo',b'{}',{'Content-Type':'text/plain'})[0],415)
        self.assertEqual(self.request('POST','/api/auth/demo',b'',{'Content-Type':'application/json','Content-Length':'2000001'})[0],413)
        self.assertEqual(self.request('GET','/.local/verification.json')[0],404)

    def test_instant_generation_http_contract_and_auth(self):
        status,_,body=self.request('GET','/api/health')
        settings=json.loads(body)['data']['trajectory']
        self.assertEqual(status,200);self.assertEqual(settings['lapsMax'],60)
        self.assertGreater(settings['referenceLapMeters'],400)
        self.assertEqual(self.request('POST','/api/trajectories/generate',b'{}',{'Content-Type':'application/json'})[0],401)
        _,headers,_=self.request('POST','/api/auth/demo',b'{}',{'Content-Type':'application/json'})
        headers={'Content-Type':'application/json','Cookie':headers['Set-Cookie'].split(';')[0]}
        status,_,body=self.request('POST','/api/trajectories/generate',b'{"laps":12}',headers)
        plan=json.loads(body)['data']
        self.assertEqual(status,200);self.assertEqual(plan['pointCount'],1801)
        self.assertEqual(plan['realWaitSeconds'],0)
        self.assertNotIn('signDigital',plan);self.assertNotIn('oct',plan)
        self.assertEqual(self.app.db.execute('SELECT count(*) FROM runs').fetchone()[0],0)

if __name__=='__main__':unittest.main(verbosity=2)
