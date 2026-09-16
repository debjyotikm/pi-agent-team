"""Regressions for correction permissions, turn-boundary delivery and retained evidence."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from server import Controller
from host_executor import HostExecutor

class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.c=Controller(Path(self.tmp.name),'http://127.0.0.1:1')
        self.s=self.c.store;self.r=self.s.create('Implement and verify a component')
        self.s.delivered([m['id'] for m in self.s.pending(self.r,'lead')],'done')
    def tearDown(self):self.c.close();self.s.db.close();self.tmp.cleanup()
    def assigned(self):
        return self.s.assign(self.r,'lead',{'task_id':'a','owner':'worker-3','reviewer':'worker-4','description':'Implement','acceptance':'Verified files'})
    def submitted(self):
        self.assigned();return self.s.board_tool(self.r,'worker-3','team_report',{'task_id':'a','summary':'Ready'})
    def test_lead_reopens_corrections_and_supersedes_stale_review(self):
        t=self.submitted()
        with self.assertRaises(PermissionError):self.c.execute(self.r,'worker-3','write',{'path':'x','content':'x'})
        args={'task_id':'a','version':t['version'],'summary':'Fix incorrect numerical claim'}
        with self.assertRaises(PermissionError):self.s.board_tool(self.r,'worker-3','team_request_changes',args)
        with self.assertRaises(ValueError):self.s.board_tool(self.r,'lead','team_request_changes',{**args,'version':'wrong'})
        self.s.board_tool(self.r,'lead','team_request_changes',args)
        self.c.execute(self.r,'worker-3','write',{'path':'x','content':'corrected'})
        self.assertEqual(self.s.claim_pending(self.r,'worker-4'),[])
        messages=self.s.claim_pending(self.r,'worker-3');self.assertEqual(len(messages),1)
        self.assertEqual(messages[0]['body']['kind'],'changes_requested')
        self.assertEqual(self.s.task(self.r,'a')['review_history'][0]['summary'],args['summary'])
    def test_midturn_claims_replay_after_failure_and_settle_once(self):
        m=self.s.message(self.r,'worker-3','lead',{'kind':'blocker','text':'Fix assignment'},True)['message_id']
        self.assertEqual(self.s.claim_pending(self.r,'lead')[0]['id'],m)
        self.assertEqual(self.s.claim_pending(self.r,'lead'),[])
        self.s.settle_delivery(self.r,'lead','pending')
        self.assertEqual(self.s.claim_pending(self.r,'lead')[0]['id'],m)
        self.s.settle_delivery(self.r,'lead','done')
        self.s.recover(self.r);self.assertEqual(self.s.pending(self.r,'lead'),[])
    def test_paused_mailbox_does_not_claim(self):
        self.s.message(self.r,'worker-3','lead',{'kind':'request','text':'Review'},True)
        with self.s.db:self.s.db.execute("UPDATE runs SET state='paused' WHERE id=?",(self.r,))
        self.assertEqual(self.s.claim_pending(self.r,'lead'),[])
    def test_checkpoint_retains_failure_evidence_and_current_correction(self):
        eid=self.s.event(self.r,'worker-3','tool_receipt',{'tool':'run','args':{'command':'pytest'},'result':{'exit_code':4,'stderr':'no tests ran'}})
        self.s.message(self.r,'user','worker-3',{'kind':'user_update','text':'No verification supports the claimed completed artifacts'},True)
        cp=self.c.checkpoint(self.r,'worker-3')
        self.assertEqual(cp['own_recent_tool_evidence'][0]['id'],eid)
        self.assertEqual(cp['own_recent_tool_evidence'][0]['exit_code'],4)
        self.assertIn('completed artifacts',json.dumps(cp['recent_incoming_messages']))
        self.assertIn('does not delete',cp['evidence_rules']);self.assertLess(len(json.dumps(cp)),33000)
    def test_sync_preserves_unpublished_ignored_snapshot_files(self):
        a=self.s.directory(self.r)/'agents/worker-3';shared=a/'shared'
        (shared/'.gitignore').write_text('artifacts/\n');(shared/'artifacts').mkdir();(shared/'artifacts/checkpoint').write_text('irreplaceable')
        self.s.sync(self.r,'worker-3')
        backups=list(a.glob('shared-preserved-*'));self.assertEqual(len(backups),1)
        self.assertEqual((backups[0]/'artifacts/checkpoint').read_text(),'irreplaceable')
    def test_pipeline_failure_is_not_hidden_by_tee(self):
        host=HostExecutor(self.s.directory(self.r)/'agents/worker-3/workspace')
        try:self.assertEqual(host.execute("bash -c 'exit 7' | tee result.log")['exit_code'],7)
        finally:host.close()
    def test_trailing_echo_does_not_hide_failed_test_pipeline(self):
        host=HostExecutor(self.s.directory(self.r)/'agents/worker-3/workspace')
        try:
            result=host.execute("bash -c 'exit 4' | tee test.log; echo misleading-success")
            self.assertEqual(result['exit_code'],4)
            self.assertNotIn('misleading-success',result['stdout'])
        finally:host.close()
    def test_busy_status_exposes_delayed_messages(self):
        self.s.message(self.r,'worker-3','lead',{'kind':'blocker','text':'Stalled'},True)
        activity={(self.r,a):{'state':'waiting','updated':time.time()} for a in ('lead','worker-1','worker-2','worker-3','worker-4')}
        activity[(self.r,'lead')]['state']='working'
        status=self.c.progress.check(self.r,activity,now=time.time()+120)
        self.assertEqual(status['state'],'coordination_required');self.assertEqual(status['issues'][0]['kind'],'unresolved_message')
