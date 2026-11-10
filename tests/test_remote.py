import copy
import http.client
import json
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path

from app import Application, APIError, Server
from remote import RemoteClient, RemoteError, UPSTREAM, signed_payload
from signing import sign
from test_app import FakeClock, TEST_HEADERS


class Transport:
    def __init__(self):
        self.calls = []
        self.uploads = 0
        self.mode = 'success'
        self.semester = {'id': 'current-semester', 'organizationId': '0123456789abcdef0123456789abcdef', 'yearRange': '2026-2027', 'index': 1}
        self.limit = {'hasRule': True, 'limitationsGoalsSexInfoId': 'current-goals', 'patternId': 'current-pattern', 'scoringType': 1}
        self.region = json.loads((Path(__file__).resolve().parents[1]/'fixtures/region.json').read_text('utf-8'))
        self.verify_failure = False
        self.record_override = None

    def __call__(self, method, url, headers, data):
        path = url.removeprefix(UPSTREAM)
        body = json.loads(data) if data else None
        self.calls.append((method, path, copy.deepcopy(headers), body))
        if self.mode == 'http-auth': return 401, b'{"code":401,"message":"expired"}'
        if self.mode == 'business-auth': return 200, b'{"code":40101,"message":"expired"}'
        if path == '/running/app/getDetails':
            if self.verify_failure: return 503, b'{"code":503,"message":"record lookup unavailable"}'
            latest = next((call[3] for call in reversed(self.calls) if call[1] == '/running/app/v3/upload'), None)
            rows = [] if latest is None else [dict(id='remote-record-123', effectiveMileage=str(latest['effectiveMileage']),
                totalMileage=str(latest['totalMileage']), effectivePart=latest['effectivePart'], scoringType=latest['scoringType'],
                uneffectiveReason=latest['uneffectiveReason'], startTime=latest['startTime'], endTime=latest['endTime'])]
            if rows and self.record_override: rows[0].update(self.record_override)
            return 200, json.dumps({'code':0,'data':{'gradeDetails':rows}}).encode()
        responses = {
            '/education/semester/getCurrent': self.semester,
            '/authorization/user/getBasicInfo': {'id': 'abcdef0123456789abcdef0123456789', 'schoolId': None, 'realName': 'Test User',
                                                  'accessToken': 'NEVER_RETURN_THIS_ACCESS_TOKEN', 'refreshToken': 'NEVER_RETURN_THIS_REFRESH_TOKEN'},
            '/running/app/getRunningLimit': self.limit,
            '/running/app/getTotalRunning': {'totalMileage': '12.50', 'totalPart': 3},
            '/running/app/getRunningRange': {'rangeRunningRegion': self.region['polygons'], 'invalidRangeRunningRegion': []},
            '/education/course/today': [],
        }
        if path == '/running/app/v3/upload':
            self.uploads += 1
            if self.mode == 'timeout': raise urllib.error.URLError('timeout')
            if self.mode == 'reject': return 200, b'{"code":42201,"message":"invalid signature"}'
            if self.mode == 'bad-json': return 200, b'<html>gateway</html>'
            if self.mode == 'missing-id': return 200, b'{"code":0,"data":null}'
            if self.mode == 'http-500': return 500, b'{"code":500,"message":"error"}'
            return 200, b'{"code":0,"data":"remote-record-123"}'
        if path not in responses: raise AssertionError('Unexpected endpoint '+path)
        return 200, json.dumps({'code': 0, 'data': responses[path]}).encode()


class RemoteAuthResponseTests(unittest.TestCase):
    def test_http_auth_errors_do_not_require_a_business_envelope(self):
        cases = (
            (401, b'{"error":"invalid_token","error_description":"private token details"}'),
            (403, b'{"error":"access_denied"}'),
            (401, b'<html>Unauthorized</html>'),
            (403, b''),
        )
        for status, raw in cases:
            with self.subTest(status=status, raw=raw):
                client = RemoteClient(TEST_HEADERS, transport=lambda *args: (status, raw))
                with self.assertRaises(RemoteError) as context:
                    client.connect()
                error = context.exception
                self.assertEqual(error.code, 'UPSTREAM_AUTH_REJECTED')
                self.assertEqual(error.http_status, status)
                self.assertFalse(error.uncertain)
                self.assertNotIn('private token details', json.dumps(error.public()))
                self.assertEqual(client.calls, [])

    def test_oauth_rejection_returns_local_401_without_creating_a_session(self):
        calls = []
        def transport(method, url, headers, data):
            calls.append(url.removeprefix(UPSTREAM))
            return 401, b'{"error":"invalid_token","error_description":"private token details"}'

        with tempfile.TemporaryDirectory() as directory:
            app = Application(directory, remote_factory=lambda auth: RemoteClient(auth, transport=transport))
            server = Server(0, app)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                conn.request('POST', '/api/auth/remote', json.dumps(TEST_HEADERS),
                             {'Content-Type': 'application/json'})
                response = conn.getresponse()
                body = json.loads(response.read())
                self.assertEqual(response.status, 401)
                self.assertIsNone(response.getheader('Set-Cookie'))
                self.assertEqual(body['code'], 'UPSTREAM_AUTH_REJECTED')
                self.assertEqual(body['details']['httpStatus'], 401)
                self.assertNotIn('private token details', json.dumps(body))
                self.assertEqual(app.sessions, {})
                self.assertEqual(app.remote_clients, {})
                self.assertEqual(calls, ['/education/semester/getCurrent'])
            finally:
                conn.close()
                server.shutdown(); server.server_close(); thread.join()
                app.db.close()


class RemoteIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.transport = Transport()
        self.app = Application(self.temp.name, clock=self.clock,
                               remote_factory=lambda auth: RemoteClient(auth, transport=self.transport))
        self.token, self.owner = self.app.remote_login(TEST_HEADERS)
        self.client = self.app.remote_client(self.token)

    def tearDown(self):
        self.app.db.close()
        self.temp.cleanup()

    def ready(self, scale=1):
        data = self.app.start({'laps': 1, 'clockScale': scale}, self.owner, self.app.region_for(self.client))
        self.clock.advance(data['plan']['durationSeconds']/scale+1)
        return data['run']['id']

    def draft(self):
        run_id = self.ready()
        return self.app.prepare_simulated_remote(run_id, self.owner, self.client)

    def test_live_identifiers_region_and_profile_sanitization(self):
        home = self.app.home(self.owner, self.client)
        self.assertEqual(home['source'], 'remote')
        self.assertEqual(home['remote']['totals']['totalMileage'], '12.50')
        self.assertEqual(home['remote']['semester']['id'], 'current-semester')
        self.assertIn('getRunningRange', home['region']['source'])
        for method, path, headers, body in self.transport.calls:
            self.assertTrue(headers['Authorization'].startswith('Bearer '))
            if path.endswith('getRunningLimit'):
                self.assertEqual(body['semesterId'], 'current-semester')
            if path.endswith('getRunningRange'):
                self.assertEqual(body['limitationsGoalsSexInfoId'], 'current-goals')
        output = json.dumps(home)+str(self.client.profile)+'\n'.join(self.app.db.iterdump())
        self.assertNotIn('NEVER_RETURN_THIS', output)
        self.assertNotIn(TEST_HEADERS['Authorization'], output)

    def test_http_and_business_auth_failures_both_fail_login(self):
        for mode in ('http-auth', 'business-auth'):
            self.transport.mode = mode
            before = len(self.app.sessions)
            with self.assertRaises(RemoteError) as context:
                self.app.remote_login(TEST_HEADERS)
            self.assertEqual(context.exception.code, 'UPSTREAM_AUTH_REJECTED')
            self.assertEqual(len(self.app.sessions), before)

    def test_demo_cannot_reach_remote_or_load_saved_credentials(self):
        token, _ = self.app.login({}, demo=True)
        with self.assertRaises(APIError): self.app.remote_client(token)
        with self.assertRaises(APIError):
            self.app.dispatch('POST', '/api/auth/remote-file', {}, '', {})

    def test_early_and_accelerated_remote_preparation_rejected(self):
        data = self.app.start({'laps': 1, 'clockScale': 1}, self.owner)
        with self.assertRaises(APIError) as context:
            self.app.prepare_simulated_remote(data['run']['id'], self.owner, self.client)
        self.assertEqual(context.exception.code, 'TOO_EARLY')
        self.app.dispatch('POST', f'/api/runs/{data["run"]["id"]}/cancel', {}, self.token, {})
        run_id = self.ready(60)
        with self.assertRaises(APIError) as context:
            self.app.prepare_simulated_remote(run_id, self.owner, self.client)
        self.assertEqual(context.exception.code, 'REMOTE_REALTIME_REQUIRED')
        self.assertEqual(self.transport.uploads, 0)

    def test_prepare_only_signs_and_persists_never_sends(self):
        draft = self.draft()
        self.assertEqual(self.transport.uploads, 0)
        self.assertFalse(draft['productionSubmitted'])
        row = self.app.db.execute('SELECT * FROM remote_uploads WHERE id=?', (draft['id'],)).fetchone()
        body = json.loads(row['payload_json'])
        self.assertEqual(body['effectiveMileage'], body['totalMileage'])
        self.assertEqual(body['effectivePart'], 1)
        self.assertEqual(body['uneffectiveReason'], '')
        self.assertEqual(body['semesterId'], 'current-semester')
        again = sign(body, self.client.profile['id'], self.client.profile['schoolId'])
        for key in ('oct', 'signDigital', 'signTime'): self.assertEqual(again[key], body[key])
        self.assertEqual(self.app.prepare_simulated_remote(draft['runId'], self.owner, self.client)['id'], draft['id'])

    def test_commit_receipt_idempotency_export_and_duplicate_import(self):
        draft = self.draft()
        body = json.loads(self.app.db.execute('SELECT payload_json FROM remote_uploads WHERE id=?', (draft['id'],)).fetchone()[0])
        self.assertEqual(self.app.remote_draft(body, self.owner, self.client)['id'], draft['id'])
        first = self.app.commit_remote(draft['id'], self.owner, self.client)
        second = self.app.commit_remote(draft['id'], self.owner, self.client)
        self.assertEqual(self.transport.uploads, 1)
        self.assertTrue(first['productionSubmitted'])
        self.assertEqual(first['remoteRecordId'], 'remote-record-123')
        self.assertTrue(second['idempotentReplay'])
        exported, _ = self.app.dispatch('GET', f'/api/runs/{draft["runId"]}/export', {}, self.token, {})
        self.assertTrue(exported['productionSubmitted'])
        self.assertTrue(exported['isSimulated'])
        self.assertEqual(exported['result']['remoteRecordId'], 'remote-record-123')
        self.assertEqual(exported['result']['serverRecord']['status'], 'effective')
        self.assertGreater(exported['result']['requestedRemoteEffectiveMileage'], 0)

    def test_record_lookup_failure_does_not_lose_acceptance_or_resend(self):
        draft = self.draft(); self.transport.verify_failure = True
        receipt = self.app.commit_remote(draft['id'], self.owner, self.client)
        self.assertTrue(receipt['productionSubmitted'])
        self.assertEqual(receipt['summary']['serverRecord']['status'], 'pending')
        replay = self.app.commit_remote(draft['id'], self.owner, self.client)
        self.assertTrue(replay['idempotentReplay']); self.assertEqual(self.transport.uploads, 1)
        self.transport.verify_failure = False
        checked, _ = self.app.dispatch('POST', f'/api/remote/uploads/{draft["id"]}/verify', {}, self.token, {})
        self.assertEqual(checked['summary']['serverRecord']['status'], 'effective')
        self.assertEqual(self.transport.uploads, 1)

    def test_server_zero_credit_is_not_confused_with_successful_submission(self):
        for reason in ('非规定时间内运动', ''):
            draft = self.draft()
            self.transport.record_override = {'effectiveMileage':'0.00','effectivePart':1,'uneffectiveReason':reason}
            receipt = self.app.commit_remote(draft['id'], self.owner, self.client)
            self.assertTrue(receipt['productionSubmitted'])
            verification = receipt['summary']['serverRecord']
            self.assertEqual(verification['status'], 'ineffective')
            self.assertEqual(verification['effectiveMileageKm'], 0)
            local = json.loads(self.app.get_run(draft['runId'], self.owner)['result_json'])
            self.assertGreater(local['requestedRemoteEffectiveMileage'], 0)
            self.assertEqual(local['serverRecord']['status'], 'ineffective')

    def test_unknown_results_are_persisted_and_never_automatically_retried(self):
        for mode in ('timeout', 'bad-json', 'missing-id', 'http-500'):
            self.transport.mode = 'success'
            draft = self.draft()
            self.transport.mode = mode
            with self.assertRaises(RemoteError): self.app.commit_remote(draft['id'], self.owner, self.client)
            row = self.app.db.execute('SELECT * FROM remote_uploads WHERE id=?', (draft['id'],)).fetchone()
            self.assertEqual(row['state'], 'unknown')
            count = self.transport.uploads
            with self.assertRaises(APIError): self.app.commit_remote(draft['id'], self.owner, self.client)
            self.assertEqual(self.transport.uploads, count)

    def test_remote_business_rejection_is_not_a_success_receipt(self):
        draft = self.draft(); self.transport.mode = 'reject'
        with self.assertRaises(RemoteError): self.app.commit_remote(draft['id'], self.owner, self.client)
        row = self.app.db.execute('SELECT * FROM remote_uploads WHERE id=?', (draft['id'],)).fetchone()
        self.assertEqual(row['state'], 'rejected')
        self.assertFalse(self.app.upload_summary(row)['productionSubmitted'])

    def test_remote_drafts_are_isolated_and_logout_clears_credentials(self):
        draft = self.draft()
        with self.assertRaises(APIError): self.app.commit_remote(draft['id'], 'another-user', self.client)
        self.app.dispatch('POST', '/api/auth/logout', {}, self.token, {})
        self.assertNotIn(self.token, self.app.remote_clients)
        self.assertNotIn(self.token, self.app.sessions)

    def test_restart_makes_inflight_writes_unknown_and_drops_live_sessions(self):
        draft = self.draft()
        self.app.db.execute("UPDATE remote_uploads SET state='sending' WHERE id=?", (draft['id'],)); self.app.db.commit()
        self.app.db.close(); self.app = Application(self.temp.name, clock=self.clock)
        row = self.app.db.execute('SELECT * FROM remote_uploads WHERE id=?', (draft['id'],)).fetchone()
        self.assertEqual(row['state'], 'unknown')
        self.assertFalse(self.app.remote_clients)

    def test_import_rejects_simulated_exports_nan_and_wrong_semester(self):
        draft = self.draft()
        body = json.loads(self.app.db.execute('SELECT payload_json FROM remote_uploads WHERE id=?', (draft['id'],)).fetchone()[0])
        for patch in ({'isSimulated': True}, {'semesterId': 'old-semester'}, {'totalMileage': float('nan')}, {'oct': 'bad'}):
            with self.assertRaises(ValueError): signed_payload(dict(body, **patch), self.client.snapshot)
        self.assertEqual(self.transport.uploads, 0)

    def test_no_arbitrary_proxy_urls_or_header_injection(self):
        with self.assertRaises(ValueError): self.client.request('https://untrusted.invalid/')
        with self.assertRaises(ValueError): self.client.request('/unknown')
        with self.assertRaises(ValueError): RemoteClient(dict(TEST_HEADERS, Authorization='Bearer value\r\nInjected: yes'))

    def test_credential_rotation_preserves_verified_account_history(self):
        draft = self.draft()
        token, owner = self.app.remote_login(dict(TEST_HEADERS, Authorization='Bearer rotated-test-token'))
        self.assertEqual(owner, self.owner)
        rows, _ = self.app.dispatch('GET', '/api/remote/uploads', {}, token, {})
        self.assertEqual(rows[0]['id'], draft['id'])

    def test_http_auth_prepare_commit_and_export(self):
        server = Server(0, self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        cookie = ''
        def request(method, path, body=None):
            nonlocal cookie
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            headers = {'Content-Type': 'application/json', 'Cookie': cookie}
            conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
            response = conn.getresponse()
            if response.getheader('Set-Cookie'): cookie = response.getheader('Set-Cookie').split(';')[0]
            result = json.loads(response.read()); status = response.status; conn.close()
            self.assertEqual(status, 200, result)
            return result['data']
        try:
            request('POST', '/api/auth/remote', TEST_HEADERS)
            data = request('POST', '/api/runs', {'laps': 1, 'clockScale': 1})
            self.clock.advance(data['plan']['durationSeconds']+1)
            run_id = data['run']['id']
            draft = request('POST', f'/api/runs/{run_id}/remote-prepare', {})
            self.assertEqual(self.transport.uploads, 0)
            result = request('POST', f'/api/remote/uploads/{draft["id"]}/commit', {})
            self.assertTrue(result['productionSubmitted'])
            result = request('GET', f'/api/runs/{run_id}/export')
            self.assertEqual(result['result']['remoteRecordId'], 'remote-record-123')
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__': unittest.main()
