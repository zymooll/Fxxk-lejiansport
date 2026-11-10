import http.client
import json
import math
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app import APIError, Application, Server
from remote import RemoteClient, RemoteError
from signing import SHANGHAI
from trajectory import length
from test_app import FakeClock, TEST_HEADERS
from test_remote import Transport


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.clock = FakeClock(); self.transport = Transport()
        self.app = Application(self.temp.name, clock=self.clock,
                               remote_factory=lambda auth: RemoteClient(auth, transport=self.transport))
        self.token, self.owner = self.app.remote_login(TEST_HEADERS)
        self.client = self.app.remote_client(self.token)

    def tearDown(self):
        self.app.db.close(); self.temp.cleanup()

    def generated(self, laps=3, scale=30):
        plan = self.app.generate_trajectory({'laps': laps, 'clockScale': scale}, self.client)
        body = {'config': plan['config'], 'generationId': plan['generationId']}
        result = self.app.save_generated(body, self.owner, self.client)
        return result['run']['id'], plan, body

    def timed(self, scale=60, points=None):
        data = self.app.start({'laps': 1, 'clockScale': scale}, self.owner)
        self.clock.advance(data['plan']['durationSeconds']/scale+.1)
        body = {'isSimulated': True, 'points': points or data['plan']['points']}
        self.app.upload(data['run']['id'], body, self.owner, 'local-saved-'+data['run']['id'])
        return data['run']['id'], data['plan']

    def error(self, code, function):
        with self.assertRaises(APIError) as context: function()
        self.assertEqual(context.exception.code, code)

    def wire(self):
        return next(call[3] for call in reversed(self.transport.calls) if call[1] == '/running/app/v3/upload')

    def test_save_generated_is_idempotent_and_keeps_the_exact_plan(self):
        run_id, plan, body = self.generated()
        replay = self.app.save_generated(body, self.owner, self.client)
        self.assertEqual(replay['run']['id'], run_id); self.assertTrue(replay['idempotentReplay'])
        home = self.app.home(self.owner, self.client)
        self.assertEqual(home['summary']['localRuns'], 1)
        self.assertEqual(home['history'][0]['state'], 'generated')
        self.assertTrue(home['history'][0]['canUpload'])
        row = self.app.get_run(run_id, self.owner)
        self.assertEqual(json.loads(row['uploaded_json'])['points'], plan['points'])
        self.assertFalse(self.app.starts); self.assertEqual(self.transport.uploads, 0)
        changed = dict(body, config=dict(body['config'], laps=4))
        self.error('GENERATION_CONTENT_CHANGED', lambda: self.app.save_generated(changed, self.owner, self.client))

    def test_delete_restore_changes_only_the_selected_local_record_and_totals(self):
        first, _, _ = self.generated(1); self.clock.advance(1)
        second, _, _ = self.generated(2)
        self.app.delete_record(first, self.owner)
        home = self.app.home(self.owner, self.client)
        self.assertEqual([row['id'] for row in home['history']], [second])
        self.assertEqual(home['summary']['localRuns'], 1)
        self.error('RUN_NOT_FOUND', lambda: self.app.get_run(first, self.owner))
        self.assertTrue(self.app.delete_record(first, self.owner)['deleted'])
        self.app.restore_record(first, self.owner)
        self.assertEqual(self.app.home(self.owner)['summary']['localRuns'], 2)
        self.assertEqual(self.transport.uploads, 0)

    def test_delete_restore_and_individual_upload_are_owner_scoped(self):
        run_id, _, _ = self.generated()
        for action in (lambda: self.app.delete_record(run_id, 'different-owner'),
                       lambda: self.app.restore_record(run_id, 'different-owner'),
                       lambda: self.app.upload_saved_record(run_id, 'different-owner', self.client)):
            self.error('RUN_NOT_FOUND', action)
        self.assertIsNone(self.app.get_run(run_id, self.owner)['deleted_at'])
        self.assertEqual(self.transport.uploads, 0)

    def test_deleting_a_running_record_cancels_it_without_blocking_a_new_run(self):
        data = self.app.start({'laps': 1, 'clockScale': 1}, self.owner)
        run_id = data['run']['id']; self.app.delete_record(run_id, self.owner)
        self.assertNotIn(run_id, self.app.starts)
        self.app.start({'laps': 2}, self.owner)
        self.app.restore_record(run_id, self.owner)
        self.assertEqual(self.app.get_run(run_id, self.owner)['state'], 'cancelled')
        self.clock.advance(500)
        self.error('RUN_NOT_RUNNING', lambda: self.app.upload(run_id, {'isSimulated': True, 'points': data['plan']['points']}, self.owner, 'cancelled-must-stay-cancelled'))

    def test_untimed_accelerated_and_real_time_records_rebase_at_upload_time(self):
        for kind in ('generated', 'accelerated', 'real-time'):
            if kind == 'generated': run_id, plan, _ = self.generated()
            else: run_id, plan = self.timed(60 if kind == 'accelerated' else 1)
            original = dict(self.app.get_run(run_id, self.owner))
            self.clock.advance(2*86400+.375)
            now = datetime.fromisoformat(self.clock.now()).astimezone(SHANGHAI)
            before_clock = self.clock.monotonic()
            receipt = self.app.upload_saved_record(run_id, self.owner, self.client)
            body = self.wire(); duration = math.floor(plan['durationSeconds']+.5)
            self.assertEqual(body['endTimeDate'], int(now.timestamp()*1000))
            self.assertEqual(body['endTime'], now.strftime('%Y-%m-%d %H:%M:%S'))
            self.assertEqual(body['startTime'], (now-timedelta(seconds=duration)).strftime('%Y-%m-%d %H:%M:%S'))
            self.assertEqual(body['keepTime'], duration)
            self.assertEqual(body['effectiveMileage'], body['totalMileage'])
            self.assertEqual(body['effectivePart'], 1); self.assertEqual(body['uneffectiveReason'], '')
            self.assertEqual(receipt['summary']['timestampMode'], 'upload-time-backfill')
            self.assertEqual(self.clock.monotonic(), before_clock)
            after = self.app.get_run(run_id, self.owner)
            for field in ('created_at', 'plan_json', 'uploaded_json'):
                self.assertEqual(after[field], original[field])
            self.assertTrue(json.loads(after['result_json'])['productionSubmitted'])
            self.assertEqual(json.loads(after['result_json'])['serverRecord']['status'], 'effective')
            count = self.transport.uploads
            self.assertTrue(self.app.upload_saved_record(run_id, self.owner, self.client)['idempotentReplay'])
            self.assertEqual(self.transport.uploads, count)
        self.assertEqual(self.transport.uploads, 3)

    def test_single_upload_uses_saved_coordinates_and_distance(self):
        source = self.app.generate_trajectory({'laps': 1})
        selected_points = [source['points'][i] for i in (0, 50, 100)]
        run_id, _ = self.timed(60, points=selected_points)
        self.app.generate_trajectory({'laps': 12})
        self.app.upload_saved_record(run_id, self.owner, self.client)
        self.assertEqual(self.wire()['routineLine'], [{key: point[key] for key in ('latitude','longitude')} for point in selected_points])
        self.assertEqual(self.wire()['totalMileage'], round(length(selected_points)/1000, 4))
        self.assertEqual(self.app.home(self.owner)['history'][0]['pointCount'], 3)

    def test_new_single_upload_supersedes_an_old_unsent_draft(self):
        run_id, _ = self.timed(1)
        old = self.app.prepare_simulated_remote(run_id, self.owner, self.client)
        self.clock.advance(7200)
        receipt = self.app.upload_saved_record(run_id, self.owner, self.client)
        self.assertNotEqual(old['id'], receipt['id'])
        self.assertEqual(self.app.db.execute('SELECT state FROM remote_uploads WHERE id=?', (old['id'],)).fetchone()[0], 'superseded')
        self.error('REMOTE_UPLOAD_NOT_RETRYABLE', lambda: self.app.commit_remote(old['id'], self.owner, self.client))
        self.assertEqual(self.app.prepare_simulated_remote(run_id, self.owner, self.client)['state'], 'accepted')
        self.assertEqual(self.transport.uploads, 1)

    def test_unknown_outcome_survives_delete_restore_and_cannot_be_resent(self):
        run_id, _, _ = self.generated(); self.transport.mode = 'timeout'
        with self.assertRaises(RemoteError): self.app.upload_saved_record(run_id, self.owner, self.client)
        self.assertEqual(self.transport.uploads, 1)
        self.app.delete_record(run_id, self.owner); self.app.restore_record(run_id, self.owner)
        self.error('REMOTE_UPLOAD_NOT_RETRYABLE', lambda: self.app.upload_saved_record(run_id, self.owner, self.client))
        self.assertFalse(self.app.home(self.owner)['history'][0]['canUpload'])
        self.assertEqual(self.transport.uploads, 1)

    def test_deleting_a_pending_draft_blocks_writing_until_restored(self):
        run_id, _ = self.timed(1)
        draft = self.app.prepare_simulated_remote(run_id, self.owner, self.client)
        self.app.delete_record(run_id, self.owner)
        listed, _ = self.app.dispatch('GET', '/api/remote/uploads', {}, self.token, {})
        self.assertNotIn(draft['id'], [item['id'] for item in listed])
        self.error('RUN_NOT_FOUND', lambda: self.app.commit_remote(draft['id'], self.owner, self.client))
        self.assertEqual(self.transport.uploads, 0)
        self.app.restore_record(run_id, self.owner)
        self.app.commit_remote(draft['id'], self.owner, self.client)
        self.assertEqual(self.transport.uploads, 1)

    def test_accepted_receipt_survives_local_delete_and_undo(self):
        run_id, _, _ = self.generated()
        receipt = self.app.upload_saved_record(run_id, self.owner, self.client)
        self.app.delete_record(run_id, self.owner)
        self.assertEqual(self.app.home(self.owner)['summary']['localRuns'], 0)
        listed, _ = self.app.dispatch('GET', '/api/remote/uploads', {}, self.token, {})
        self.assertIn(receipt['id'], [item['id'] for item in listed])
        self.app.restore_record(run_id, self.owner)
        self.assertTrue(self.app.upload_saved_record(run_id, self.owner, self.client)['idempotentReplay'])
        self.assertEqual(self.transport.uploads, 1)

    def test_explicit_retry_after_business_rejection_rebuilds_the_timestamp(self):
        run_id, _, _ = self.generated(); self.transport.mode = 'reject'
        with self.assertRaises(RemoteError): self.app.upload_saved_record(run_id, self.owner, self.client)
        old_end = self.wire()['endTimeDate']; self.clock.advance(10)
        self.transport.mode = 'success'
        self.app.upload_saved_record(run_id, self.owner, self.client)
        self.assertGreater(self.wire()['endTimeDate'], old_end)
        self.assertEqual(self.transport.uploads, 2)

    def test_legacy_database_migration_keeps_saved_records(self):
        run_id, _ = self.timed(60)
        original = dict(self.app.get_run(run_id, self.owner))
        legacy = Path(self.temp.name)/'legacy'; legacy.mkdir()
        fields = ('id','created_at','state','plan_json','owner','result_json','uploaded_json','idempotency_key')
        with sqlite3.connect(legacy/'runs.sqlite3') as db:
            db.execute('CREATE TABLE runs(id TEXT PRIMARY KEY,created_at TEXT,state TEXT,plan_json TEXT,owner TEXT,result_json TEXT,uploaded_json TEXT,idempotency_key TEXT)')
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)', tuple(original[field] for field in fields))
        db.close()
        migrated = Application(legacy, clock=self.clock)
        try:
            row = migrated.get_run(run_id, self.owner)
            for field in fields: self.assertEqual(row[field], original[field])
            self.assertIsNone(row['deleted_at'])
        finally: migrated.db.close()

    def test_http_save_upload_delete_restore_routes(self):
        server = Server(0, self.app); thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        cookie = ''
        def request(method, path, body=None, status=200, authenticated=True):
            nonlocal cookie
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            headers = {'Content-Type':'application/json', 'Cookie':cookie if authenticated else ''}
            connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
            response = connection.getresponse(); result = json.loads(response.read())
            if response.getheader('Set-Cookie'): cookie = response.getheader('Set-Cookie').split(';')[0]
            self.assertEqual(response.status, status, result); connection.close()
            return result.get('data')
        try:
            request('POST', '/api/auth/remote', TEST_HEADERS)
            plan = request('POST', '/api/trajectories/generate', {'laps':3})
            saved = request('POST', '/api/trajectories/save', {'config':plan['config'],'generationId':plan['generationId']})
            run_id = saved['run']['id']
            receipt = request('POST', f'/api/runs/{run_id}/remote-upload', {})
            self.assertTrue(receipt['productionSubmitted'])
            request('POST', f'/api/runs/{run_id}/delete', {}, status=401, authenticated=False)
            request('POST', f'/api/runs/{run_id}/delete', {})
            request('GET', f'/api/runs/{run_id}', status=404)
            request('POST', f'/api/runs/{run_id}/restore', {})
            self.assertTrue(request('GET', f'/api/runs/{run_id}')['result']['productionSubmitted'])
            self.assertEqual(self.transport.uploads, 1)
        finally: server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__': unittest.main()
