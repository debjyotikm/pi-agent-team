"""Recovery after an idle turn fails to complete its required workflow transition."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from progress import ProgressReconciler
from server import Controller
from store import AGENTS


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = Controller(Path(self.tmp.name), 'http://127.0.0.1:1')
        self.s = self.c.store
        self.r = self.s.create('Prepare and qualify; do not launch the campaign.')
        self.s.delivered([m['id'] for m in self.s.pending(self.r, 'lead')], 'done')
        self.activity = {(self.r, a): {'state': 'waiting', 'updated': 0} for a in AGENTS}
        self.now = time.time() + 100
        self.p = ProgressReconciler(self.s, idle_grace=0, retry_delay=0)

    def tearDown(self):
        self.c.close()
        for thread in self.c.threads.values():
            thread.join(timeout=3)
        self.s.db.close()
        self.tmp.cleanup()

    def delivery_review(self):
        self.s.assign(self.r, 'lead', {'task_id': 'delivery', 'owner': 'lead', 'reviewer': 'worker-1',
                                     'description': 'Read back delivered files', 'acceptance': 'Hashes match'})
        self.s.board_tool(self.r, 'lead', 'team_report', {'task_id': 'delivery', 'summary': 'Delivered; verify hashes'})
        for a in AGENTS:
            self.s.delivered([m['id'] for m in self.s.pending(self.r, a)], 'done')
        return self.s.task(self.r, 'delivery')

    def check(self):
        return self.p.check(self.r, self.activity, now=self.now, repair=True)

    def consume(self):
        for a in AGENTS:
            self.s.delivered([m['id'] for m in self.s.pending(self.r, a)], 'done')

    def test_mistaken_owner_tool_gets_actionable_review_error_then_targeted_reminder(self):
        task = self.delivery_review()
        with self.assertRaisesRegex(PermissionError, 'You are the reviewer. Use team_review'):
            self.s.board_tool(self.r, 'worker-1', 'team_report', {'task_id': 'delivery', 'summary': 'Verified'})
        self.s.board_tool(self.r, 'worker-1', 'team_message', {'recipient': 'lead', 'kind': 'information', 'text': 'Checks complete'})
        result = self.check()
        self.assertEqual(result['next_actor'], 'worker-1')
        msg = self.s.pending(self.r, 'worker-1')[0]
        self.assertEqual(msg['body']['kind'], 'progress_repair')
        self.assertIn('team_review', msg['body']['instruction'])
        self.assertEqual(self.s.task(self.r, 'delivery')['state'], 'review')
        self.assertEqual(self.s.task(self.r, 'delivery')['version'], task['version'])

    def test_information_only_readback_wakes_lead_for_finalization_without_auto_completion(self):
        task = self.delivery_review()
        self.s.board_tool(self.r, 'worker-1', 'team_review', {'task_id': 'delivery', 'version': task['version'], 'approved': True, 'summary': 'Verified'})
        self.consume()
        message = self.s.message(self.r, 'worker-4', 'lead', {'kind': 'information', 'text': 'Final delivered hashes match'}, False)
        result = self.check()
        self.assertEqual(result['next_actor'], 'lead')
        self.assertIn(message['message_id'], result['recent_information_ids'])
        self.assertIn('team_finish', self.s.pending(self.r, 'lead')[0]['body']['instruction'])
        self.assertEqual(self.s.run(self.r)['state'], 'running')

    def test_bounded_escalation_survives_reconciler_recreation(self):
        self.delivery_review()
        self.assertEqual(self.check()['next_actor'], 'worker-1')
        self.consume()
        self.p = ProgressReconciler(self.s, idle_grace=0, retry_delay=0)
        self.assertEqual(self.check()['next_actor'], 'lead')
        self.consume()
        result = self.check()
        self.assertEqual(result['state'], 'stalled')
        self.assertEqual(result['repair_attempts'], 2)
        self.assertFalse(any(self.s.pending(self.r, a) for a in AGENTS))
        self.assertEqual(self.check()['repair_attempts'], 2)

    def test_live_activity_pending_messages_and_unresolved_backend_calls_are_not_nudged(self):
        self.delivery_review()
        self.activity[(self.r, 'lead')]['state'] = 'working'
        self.assertEqual(self.check()['state'], 'active')
        self.activity[(self.r, 'lead')]['state'] = 'waiting'
        self.s.message(self.r, 'user', 'lead', {'kind': 'user_update', 'text': 'Update'}, True)
        self.assertEqual(self.check()['state'], 'dispatch_pending')
        self.consume()
        with self.s.db:
            self.s.db.execute('INSERT INTO calls VALUES(?,?,?,?,?,?)', (self.r, 'lead', 'live-call', '{}', None, 'executing'))
        self.assertEqual(self.check()['state'], 'executing')
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM progress_repairs').fetchone()[0], 0)

    def test_blocker_is_escalated_without_mutating_authority_or_task_state(self):
        self.s.assign(self.r, 'lead', {'task_id': 'resource', 'owner': 'lead', 'reviewer': 'worker-2', 'description': 'Qualify', 'acceptance': 'Required checks'})
        self.s.board_tool(self.r, 'lead', 'team_report', {'task_id': 'resource', 'summary': 'Resource lock held', 'blocked': True})
        self.consume()
        self.assertEqual(self.check()['next_actor'], 'lead')
        self.assertEqual(self.s.task(self.r, 'resource')['state'], 'blocked')
        self.assertIn('Do not assume expanded authority', self.s.pending(self.r, 'lead')[0]['body']['instruction'])

    def test_completed_and_paused_runs_never_wake(self):
        for state in ('complete', 'paused'):
            with self.s.db:
                self.s.db.execute('UPDATE runs SET state=? WHERE id=?', (state, self.r))
            self.assertEqual(self.check()['state'], state)
            self.assertFalse(any(self.s.pending(self.r, a) for a in AGENTS))

    def test_idle_grace_and_retry_delay(self):
        self.delivery_review()
        self.p = ProgressReconciler(self.s, idle_grace=30, retry_delay=60)
        self.activity[(self.r, 'lead')]['updated'] = self.now - 10
        self.assertEqual(self.check()['state'], 'awaiting_review')
        self.now += 31
        self.assertEqual(self.check()['state'], 'repair_queued')
        self.consume()
        self.now += 30
        self.assertEqual(self.check()['state'], 'awaiting_review')
        self.now += 31
        self.assertEqual(self.check()['state'], 'repair_queued')

    def test_actual_actor_recovers_review_and_finalizes_without_manual_message(self):
        self.delivery_review()
        self.c.progress = self.p
        calls = []
        class FakeRPC:
            def __init__(inner, agent):
                inner.agent = agent
            def prompt(inner, text):
                calls.append(inner.agent)
                task = self.s.task(self.r, 'delivery')
                if inner.agent == 'worker-1':
                    self.s.board_tool(self.r, inner.agent, 'team_review', {'task_id': 'delivery', 'version': task['version'], 'approved': True, 'summary': 'Read-back verified'})
                elif inner.agent == 'lead':
                    self.assertEqual(task['state'], 'done')
                    self.s.board_tool(self.r, 'lead', 'team_finish', {'version': self.s.status(self.r)['version'], 'summary': 'Qualified worker delivered; no deployment', 'validation': 'Existing receipts and independent review'})
                else:
                    raise AssertionError('No unrelated actor work needed')
        class FakeRuntime:
            def __init__(inner, agent):
                inner.rpc = FakeRPC(agent)
            def close(inner):
                pass
        self.c.ensure_runtime = lambda run, agent: FakeRuntime(agent)
        self.c.start(self.r)
        deadline = time.monotonic() + 5
        while self.s.run(self.r)['state'] == 'running' and time.monotonic() < deadline:
            self.p.check(self.r, dict(self.c.activity), repair=True)
            time.sleep(.05)
        self.assertEqual(self.s.run(self.r)['state'], 'complete')
        self.assertEqual(calls, ['worker-1', 'lead'])

if __name__ == '__main__':
    unittest.main()
