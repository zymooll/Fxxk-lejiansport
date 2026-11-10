import json
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import APIError
from remote import RemoteClient
from server_v3 import Application
from test_app import FakeClock
from test_v3 import PoolTransport


class AutomaticTargetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.transport=PoolTransport();self.clock=FakeClock()
        self.app=Application(self.temp.name,clock=self.clock,remote_factory=lambda config:RemoteClient(config,transport=self.transport))
        self.a=self.add('A');self.clock.advance(1);self.b=self.add('B')

    def add(self,name):
        return self.app.add_account({'label':name,'credentials':{'accessToken':name+'-valid','refreshToken':name+'-refresh'}})['account']['id']

    def tearDown(self):self.app.db.close();self.temp.cleanup()

    def drain(self,job):
        while job['state'] in ('running','paused'):job=self.app.next_batch(job['id'])
        return job

    def test_goal_defaults_to_semester_and_overrides_are_independent_and_durable(self):
        self.assertEqual(self.app.accounts()[0]['targetMileageKm'],100)
        self.app.set_target(self.a,{'targetMileageKm':80.25})
        self.app.set_target(self.b,{'targetMileageKm':120})
        self.transport.rule_overrides['A']={'semesterMileage':90}
        self.app.refresh_account(self.a)
        self.assertEqual(self.app.public_account(self.app.account(self.a))['targetMileageKm'],80.25)
        self.assertEqual(self.app.public_account(self.app.account(self.b))['targetMileageKm'],120)
        self.app.db.close()
        self.app=Application(self.temp.name,clock=self.clock,remote_factory=lambda config:RemoteClient(config,transport=self.transport))
        self.assertEqual(self.app.public_account(self.app.account(self.a))['targetMileageKm'],80.25)
        followed=self.app.set_target(self.a,{'targetMileageKm':None})
        self.assertEqual(followed['targetMileageKm'],90);self.assertEqual(followed['targetSource'],'semester')

    def test_goal_validation_accepts_zero_and_rejects_invalid_numbers(self):
        self.assertEqual(self.app.set_target(self.a,{'targetMileageKm':0})['targetMileageKm'],0)
        for value in (-1,True,'100',float('nan'),float('inf'),1000001):
            with self.subTest(value=value):
                with self.assertRaises(APIError):self.app.set_target(self.a,{'targetMileageKm':value})

    def test_preview_refreshes_but_does_not_generate_or_upload(self):
        self.app.set_target(self.a,{'targetMileageKm':10})
        preview=self.app.preview_auto({'settings':{'seed':42}})
        self.assertEqual(preview['items'][0]['code'],'TARGET_REACHED')
        plan=preview['items'][1];self.assertEqual(plan['plannedMileageKm'],4)
        self.assertEqual(plan['laps'],math.ceil(4000/plan['referenceLapMeters']))
        self.assertEqual(self.transport.uploads,[]);self.assertEqual(self.app.generations,{})

    def test_five_kilometers_use_ceiling_actual_lap_length_and_example_400_m(self):
        self.transport.rule_overrides['A']={'totalDayMileage':0}
        actual=self.app.automatic_plan(self.a)
        self.assertEqual(actual['plannedMileageKm'],5);self.assertEqual(actual['laps'],12)
        with patch('server_v3.length',return_value=400):
            self.assertEqual(self.app.automatic_plan(self.a)['laps'],13)

    def test_target_is_stop_condition_not_a_reduction_of_daily_record(self):
        self.app.set_target(self.a,{'targetMileageKm':13})
        plan=self.app.automatic_plan(self.a)
        self.assertEqual(plan['plannedMileageKm'],4)

    def test_skip_day_full_week_full_and_unavailable_goal(self):
        for override,code in (({'totalDayMileage':5},'DAILY_LIMIT_REACHED'),
                              ({'totalWeekMileage':35},'WEEKLY_LIMIT_REACHED'),
                              ({'semesterMileage':None},'TARGET_UNAVAILABLE'),
                              ({'totalDayMileage':None},'QUOTA_UNAVAILABLE')):
            self.transport.rule_overrides['A']=override
            with self.subTest(code=code):self.assertEqual(self.app.automatic_plan(self.a)['code'],code)

    def test_weekly_remaining_and_opening_time_are_respected(self):
        self.transport.rule_overrides['A']={'totalWeekMileage':33,'weeklyMileage':35}
        self.assertEqual(self.app.automatic_plan(self.a)['plannedMileageKm'],2)
        self.transport.rule_overrides['A']={'runningTimeLimit':{'openTimes':[{'dayStartTime':12*3600*1000,'dayEndTime':23*3600*1000}]}}
        self.assertEqual(self.app.automatic_plan(self.a)['code'],'OUTSIDE_RUNNING_TIME')

    def test_batch_submits_at_most_one_record_per_eligible_account_and_continues(self):
        self.app.set_target(self.a,{'targetMileageKm':10})
        job=self.drain(self.app.start_batch({'settings':{'seed':42}}))
        self.assertEqual([item['state'] for item in job['items']],['skipped','accepted'])
        self.assertEqual(len(self.transport.uploads),1);self.assertEqual(self.transport.uploads[0][0],'B')
        self.assertEqual(len(self.transport.uploads[0][1]['routineLine']),1501)
        self.assertEqual(job['items'][1]['receipt']['serverRecord']['status'],'ineffective')
        self.app.next_batch(job['id']);self.assertEqual(len(self.transport.uploads),1)

    def test_active_batch_blocks_another_batch_and_manual_write(self):
        generated=self.app.generate(self.a,{'laps':3});self.app.start_batch({})
        with self.assertRaises(APIError):self.app.start_batch({})
        with self.assertRaises(APIError):self.app.upload(generated['generationId'],self.a)
        self.assertEqual(self.transport.uploads,[])

    def test_unknown_outcome_is_not_retried_by_continuation_or_new_batch(self):
        job=self.app.start_batch({});self.transport.fail_upload=True
        job=self.app.next_batch(job['id']);self.assertEqual(job['items'][0]['state'],'unknown')
        self.transport.fail_upload=False;job=self.drain(job)
        self.assertEqual(len(self.transport.uploads),2)
        plan=self.app.automatic_plan(self.a)
        self.assertEqual(plan['code'],'PREVIOUS_OUTCOME_UNKNOWN')
        self.app.next_batch(job['id']);self.assertEqual(len(self.transport.uploads),2)

    def test_confirmed_credit_reserves_day_quota_while_statistics_lag(self):
        self.app.set_target(self.b,{'targetMileageKm':0})
        self.transport.record_overrides['A']={'effectiveMileage':'4','uneffectiveReason':''}
        job=self.drain(self.app.start_batch({}))
        self.assertEqual(job['items'][0]['receipt']['serverRecord']['status'],'effective')
        self.assertEqual(self.app.automatic_plan(self.a)['code'],'DAILY_LIMIT_REACHED')

    def test_stop_and_restart_continue_only_unprocessed_accounts(self):
        job=self.app.start_batch({});job=self.app.next_batch(job['id'])
        self.assertEqual(len(self.transport.uploads),1)
        self.app.db.close()
        self.app=Application(self.temp.name,clock=self.clock,remote_factory=lambda config:RemoteClient(config,transport=self.transport))
        resumed=self.app.batch(job['id']);self.assertEqual(resumed['state'],'paused')
        resumed=self.drain(resumed);self.assertEqual(len(self.transport.uploads),2)
        another=self.app.start_batch({});cancelled=self.app.cancel_batch(another['id'])
        self.assertEqual(cancelled['state'],'cancelled');self.app.next_batch(another['id'])
        self.assertEqual(len(self.transport.uploads),2)

    def test_failure_of_one_account_does_not_stop_the_other(self):
        original=self.app.refresh_account
        def failed(account_id):
            if account_id==self.a:raise APIError(502,'TEST_FAILURE','account unavailable')
            return original(account_id)
        with patch.object(self.app,'refresh_account',side_effect=failed):
            job=self.drain(self.app.start_batch({}))
        self.assertEqual([item['state'] for item in job['items']],['failed','accepted'])

    def test_deleted_account_is_skipped_and_auto_settings_reject_manual_laps(self):
        job=self.app.start_batch({})
        self.app.dispatch('POST',f'/api/accounts/{self.a}/delete',{},'',{})
        finished=self.drain(job);self.assertEqual(finished['items'][0]['state'],'skipped')
        with self.assertRaises(APIError):self.app.auto_settings({'settings':{'laps':3}})

    def test_restart_of_inflight_item_does_not_submit_it_again(self):
        job=self.app.start_batch({});item=job['items'][0]
        item.update(state='sending',generationId='inflight-test-id');self.app.save_batch(job)
        self.app.db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?)',('inflight-test-id',self.a,'sending','hash','{}',self.clock.now()));self.app.db.commit()
        self.app.db.close()
        self.app=Application(self.temp.name,clock=self.clock,remote_factory=lambda config:RemoteClient(config,transport=self.transport))
        recovered=self.app.batch(job['id']);self.assertEqual(recovered['items'][0]['state'],'unknown')
        self.drain(recovered);self.assertEqual(len(self.transport.uploads),1);self.assertEqual(self.transport.uploads[0][0],'B')

    def test_database_migration_keeps_old_account_and_defaults_to_cached_semester(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'accounts.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE accounts(id TEXT PRIMARY KEY,owner TEXT UNIQUE,label TEXT NOT NULL,name TEXT,school TEXT,state TEXT NOT NULL,metrics_json TEXT NOT NULL,error_json TEXT,created_at TEXT NOT NULL,checked_at TEXT,deleted_at TEXT)')
                db.execute('INSERT INTO accounts VALUES(?,?,?,?,?,?,?,?,?,?,?)',('old-account','old-owner','Original','User','School','ready',json.dumps({'semesterTargetMileageKm':120,'totalMileageKm':5}),None,self.clock.now(),None,None))
            db.close()
            migrated=Application(directory)
            try:
                account=migrated.accounts()[0];self.assertEqual(account['label'],'Original');self.assertEqual(account['targetMileageKm'],120)
                self.assertEqual(account['targetSource'],'semester')
            finally:migrated.db.close()


if __name__=='__main__':unittest.main()
