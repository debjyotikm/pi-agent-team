import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from server import Controller
from store import manifest,revision


class RecoveryEdges(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.c=Controller(Path(self.tmp.name),'http://127.0.0.1:1');self.s=self.c.store
        self.r=self.s.create('Implement the task');self.d=self.s.directory(self.r)
        for i in (1,2):
            self.s.assign(self.r,'lead',{'task_id':str(i),'owner':f'worker-{i}','reviewer':f'worker-{i+2}','description':'file','acceptance':'correct'})
    def tearDown(self):self.s.db.close();self.tmp.cleanup()
    def put(self,a,name,text):
        p=self.d/'agents'/a/'workspace'/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
    def pub(self,a,t,files):return self.s.publish(self.r,a,{'task_id':t,'files':files})

    def test_path_alias_cannot_bypass_conflict(self):
        self.put('worker-1','foo.txt','first');self.pub('worker-1','1',['foo.txt'])
        self.put('worker-2','foo.txt','second')
        with self.assertRaises(ValueError):self.pub('worker-2','2',['./foo.txt'])
        self.assertEqual((self.d/'project/foo.txt').read_text(),'first')

    def test_alias_duplicates_are_rejected(self):
        self.put('worker-1','foo.txt','value')
        with self.assertRaises(ValueError):self.pub('worker-1','1',['foo.txt','./foo.txt'])

    def test_journal_survives_failure_before_version_seal(self):
        self.put('worker-1','foo.txt','value')
        with patch.object(self.s,'seal',side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):self.pub('worker-1','1',['foo.txt'])
        self.assertTrue((self.d/'publication.json').exists())
        self.s.repair_publication(self.r)
        self.assertFalse((self.d/'publication.json').exists())
        ver=revision(manifest(self.d/'project'))
        self.assertTrue((self.d/'versions'/ver/'foo.txt').exists())
        self.assertEqual(self.s.task(self.r,'1')['version'],ver)
        base=json.loads((self.d/'agents/worker-1/base.json').read_text())
        self.assertEqual(base,manifest(self.d/'project'))
        self.put('worker-1','foo.txt','next');self.pub('worker-1','1',['foo.txt'])

    def test_recovery_completes_task_metadata_after_seal(self):
        self.put('worker-1','foo.txt','value')
        with patch.object(self.s,'save_task',side_effect=OSError('interrupted after base')):
            with self.assertRaises(OSError):self.pub('worker-1','1',['foo.txt'])
        self.s.repair_publication(self.r)
        self.assertEqual(self.s.task(self.r,'1')['version'],revision(manifest(self.d/'project')))

    def test_second_review_cycle_notification_is_recovered(self):
        self.s.board_tool(self.r,'worker-1','team_report',{'task_id':'1','summary':'first'})
        t=self.s.task(self.r,'1');old=t['review_id']
        self.s.board_tool(self.r,'worker-3','team_review',{'task_id':'1','version':t['version'],'approved':False,'summary':'fix'})
        # Interrupted report: current review state persisted, its notification did not.
        t=self.s.task(self.r,'1');t.update(state='review',review_id='new-cycle');self.s.save_task(self.r,t)
        self.s.recover(self.r)
        rows=self.s.pending(self.r,'worker-3')
        self.assertTrue(any(x['body'].get('task',{}).get('review_id')=='new-cycle' for x in rows))

    def test_rejected_review_owner_notification_is_recovered(self):
        self.s.board_tool(self.r,'worker-1','team_report',{'task_id':'1','summary':'first'})
        t=self.s.task(self.r,'1')
        self.s.board_tool(self.r,'worker-3','team_review',{'task_id':'1','version':t['version'],'approved':False,'summary':'fix'})
        with self.s.lock,self.s.db:
            self.s.db.execute("DELETE FROM messages WHERE run=? AND json_extract(body,'$.kind')='changes_requested'",(self.r,))
        t=self.s.task(self.r,'1')
        self.s.recover(self.r)
        self.assertTrue(any(x['body'].get('task',{}).get('dispatch_id')==t['dispatch_id'] for x in self.s.pending(self.r,'worker-1')))

    def test_paused_task_blocks_mutations_and_completion(self):
        self.s.board_tool(self.r,'lead','team_pause_task',{'task_id':'1'})
        with self.assertRaises(PermissionError):self.c.execute(self.r,'worker-1','write',{'path':'x','content':'x'})
        with self.assertRaises(ValueError):self.s.board_tool(self.r,'lead','team_finish',{'version':self.s.status(self.r)['version'],'summary':'done','validation':'reviewed'})
        self.assertFalse(any(x['body'].get('kind')=='pause_task' for x in self.s.pending(self.r,'worker-1')))

    def test_pausing_active_owner_calls_runtime_interrupt(self):
        class R:
            interrupted=False
            def interrupt(self):self.interrupted=True
        rt=R();self.c.runtimes[(self.r,'worker-1')]=rt
        self.c.execute(self.r,'lead','team_pause_task',{'task_id':'1'})
        self.assertTrue(rt.interrupted)

if __name__=='__main__':unittest.main()
