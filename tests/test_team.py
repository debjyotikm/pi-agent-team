import concurrent.futures
import json
import tempfile
import threading
import unittest
from pathlib import Path
from store import Store, AGENTS, manifest, revision, safe
from server import Controller


class TeamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = Controller(Path(self.tmp.name), 'http://127.0.0.1:1')
        self.s = self.c.store; self.r = self.s.create('Implement the user task and check it.')
        self.d = self.s.directory(self.r)

    def tearDown(self):
        self.s.db.close(); self.tmp.cleanup()

    def task(self, ident='a', owner='worker-1', reviewer='worker-3', deps=None):
        return self.s.board_tool(self.r, 'lead', 'team_assign', {'task_id':ident,'owner':owner,'reviewer':reviewer,'description':'Make a file','acceptance':'The file has correct content','dependencies':deps or []})

    def put(self, agent, name, value):
        p=self.d/'agents'/agent/'workspace'/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(value)

    def publish(self, agent, ident, files):
        return self.s.board_tool(self.r,agent,'team_publish',{'task_id':ident,'files':files})

    def test_exact_roster_and_leader(self):
        self.assertEqual(len(AGENTS),5); self.assertEqual(AGENTS['lead']['thinking'],'off')
        self.assertEqual(sum(v['provider']=='local' for v in AGENTS.values()),5)
        with self.assertRaises(PermissionError): self.s.assign(self.r,'worker-1',{})

    def test_dependency_dispatch_waits_for_review(self):
        self.task(); self.task('b','worker-2','worker-4',['a'])
        self.assertEqual(self.s.task(self.r,'b')['state'],'queued')
        self.s.board_tool(self.r,'worker-1','team_report',{'task_id':'a','summary':'Analyzed'})
        self.assertEqual(self.s.task(self.r,'b')['state'],'queued')
        a=self.s.task(self.r,'a')
        self.s.board_tool(self.r,'worker-3','team_review',{'task_id':'a','version':a['version'],'approved':True,'summary':'Verified'})
        self.assertEqual(self.s.task(self.r,'b')['state'],'assigned')

    def test_ack_does_not_wake(self):
        self.s.board_tool(self.r,'worker-1','team_message',{'recipient':'worker-3','text':'Thanks','kind':'ack'})
        self.assertEqual(self.s.pending(self.r,'worker-3'),[])
        inbox=self.s.board_tool(self.r,'worker-3','team_inbox',{})
        self.assertEqual(inbox[0]['body']['text'],'Thanks')

    def test_question_wakes_only_recipient(self):
        self.s.board_tool(self.r,'worker-1','team_message',{'recipient':'worker-3','text':'Can you review this?','kind':'question'})
        self.assertEqual(len(self.s.pending(self.r,'worker-3')),1)
        self.assertFalse(self.s.pending(self.r,'worker-4'))

    def test_cross_agent_file_isolation(self):
        self.put('worker-1','a.txt','private')
        self.assertFalse((self.d/'agents/worker-3/workspace/a.txt').exists())
        self.assertFalse((self.d/'project/a.txt').exists())

    def test_publication_ownership(self):
        self.task();self.put('worker-2','a.txt','wrong owner')
        with self.assertRaises(PermissionError):self.publish('worker-2','a',['a.txt'])

    def test_conflicting_publication_is_atomic(self):
        self.task();self.task('b','worker-2','worker-4')
        self.put('worker-1','a.txt','first');self.publish('worker-1','a',['a.txt'])
        self.put('worker-2','a.txt','second');self.put('worker-2','b.txt','must not leak')
        with self.assertRaises(ValueError):self.publish('worker-2','b',['b.txt','a.txt'])
        self.assertEqual((self.d/'project/a.txt').read_text(),'first')
        self.assertFalse((self.d/'project/b.txt').exists())

    def test_disjoint_changes_merge(self):
        self.task();self.task('b','worker-2','worker-4')
        self.put('worker-1','a.txt','A');self.put('worker-2','b.txt','B')
        self.publish('worker-1','a',['a.txt']);self.publish('worker-2','b',['b.txt'])
        self.assertEqual(set(manifest(self.d/'project')),{'a.txt','b.txt'})

    def test_sync_preserves_dirty_work(self):
        self.task();self.put('worker-1','a.txt','published');self.publish('worker-1','a',['a.txt'])
        self.put('worker-3','a.txt','unpublished')
        with self.assertRaises(ValueError):self.s.sync(self.r,'worker-3')
        self.assertEqual((self.d/'agents/worker-3/workspace/a.txt').read_text(),'unpublished')

    def test_submitted_version_is_immutable(self):
        self.task();self.put('worker-1','a.txt','v1');v=self.publish('worker-1','a',['a.txt'])['version']
        self.put('worker-1','a.txt','v2');self.publish('worker-1','a',['a.txt'])
        self.assertEqual((self.d/'versions'/v/'a.txt').read_text(),'v1')

    def test_wrong_review_identity_or_version_rejected(self):
        self.task();self.s.board_tool(self.r,'worker-1','team_report',{'task_id':'a','summary':'done'})
        with self.assertRaises(PermissionError):self.s.board_tool(self.r,'worker-2','team_review',{'task_id':'a'})
        with self.assertRaises(ValueError):self.s.board_tool(self.r,'worker-3','team_review',{'task_id':'a','version':'wrong'})

    def test_failed_review_returns_to_owner(self):
        self.task();self.s.board_tool(self.r,'worker-1','team_report',{'task_id':'a','summary':'done'})
        t=self.s.task(self.r,'a');self.s.board_tool(self.r,'worker-3','team_review',{'task_id':'a','version':t['version'],'approved':False,'summary':'missing required output'})
        self.assertEqual(self.s.task(self.r,'a')['state'],'assigned')

    def test_duplicate_mutation_is_not_repeated(self):
        a={'path':'a.txt','content':'first'}
        r=self.c.tool(self.r,'worker-1','call-1','write',a)
        self.put('worker-1','a.txt','subsequent change')
        self.assertEqual(self.c.tool(self.r,'worker-1','call-1','write',a),r)
        self.assertEqual((self.d/'agents/worker-1/workspace/a.txt').read_text(),'subsequent change')

    def test_interrupted_mutation_is_not_replayed(self):
        with self.s.db:self.s.db.execute('INSERT INTO calls VALUES(?,?,?,?,?,?)',(self.r,'worker-1','id',json.dumps({'tool':'write','args':{}},sort_keys=True),None,'executing'))
        with self.assertRaises(RuntimeError):self.c.tool(self.r,'worker-1','id','write',{})

    def test_failed_tools_are_durable_evidence(self):
        result=self.c.tool(self.r,'worker-1','id','read',{'path':'missing'})
        self.assertIn('error',result)
        e=self.s.board_tool(self.r,'lead','team_evidence',{'event_id':result['evidence_id']})
        self.assertIn('error',e['data']['result'])

    def test_restart_requeues_only_inflight_messages(self):
        m=self.s.pending(self.r,'lead')[0]['id'];self.s.delivered([m],'delivering')
        self.s.recover(self.r);self.assertEqual(self.s.pending(self.r,'lead')[0]['id'],m)
        self.s.delivered([m],'done');self.s.recover(self.r);self.assertFalse(self.s.pending(self.r,'lead'))

    def test_checkpoint_is_bounded_and_keeps_evidence_references(self):
        e=self.s.event(self.r,'worker-3','tool_receipt',{'stdout':'x'*1000000})
        checkpoint=self.c.checkpoint(self.r,'worker-3')
        self.assertLess(len(json.dumps(checkpoint)),33000)
        self.assertTrue(any(x['id']==e for x in checkpoint['recent_evidence']))
        self.assertEqual(checkpoint['identity']['model'],AGENTS['worker-3']['model'])

    def test_database_concurrent_reads_and_writes(self):
        def work(n):
            for i in range(50):
                self.s.event(self.r,'lead','test',{'n':n,'i':i})
                self.assertEqual(self.s.run(self.r)['id'],self.r)
                self.s.status(self.r)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:list(pool.map(work,range(5)))

    def test_path_escape_and_symlink_rejected(self):
        for p in ('../outside','/etc/passwd','.git/config'):
            with self.assertRaises(ValueError):safe(self.d/'project',p)
        (self.d/'project/link').symlink_to('/etc/passwd')
        with self.assertRaises(ValueError):safe(self.d/'project','link')

    def test_unfinished_task_blocks_final_delivery(self):
        self.task()
        with self.assertRaises(ValueError):self.s.board_tool(self.r,'lead','team_finish',{'version':self.s.status(self.r)['version'],'summary':'done','validation':'checked'})

    def test_only_lead_can_finish(self):
        with self.assertRaises(PermissionError):self.s.board_tool(self.r,'worker-3','team_finish',{})

    def test_worker_failure_does_not_change_other_tasks(self):
        self.task();self.task('b','worker-2','worker-4')
        self.s.board_tool(self.r,'worker-1','team_report',{'task_id':'a','summary':'provider failed','blocked':True})
        self.assertEqual(self.s.task(self.r,'b')['state'],'assigned')
        self.assertEqual(self.s.run(self.r)['state'],'running')

if __name__=='__main__':unittest.main()
