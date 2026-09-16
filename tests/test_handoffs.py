import json
import tempfile
import unittest
from pathlib import Path
from server import Controller
import handoffs

class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.c=Controller(Path(self.tmp.name),'http://127.0.0.1:1');self.s=self.c.store;self.r=self.s.create('Verify handoffs');(self.s.root/'strict-workflow.json').write_text('{}')
        self.s.assign(self.r,'lead',{'task_id':'a','owner':'worker-3','reviewer':'worker-4','description':'Write deliverable','acceptance':'Content checked','required_outputs':['output.py']})
        self.w=self.s.directory(self.r)/'agents/worker-3/workspace'
    def tearDown(self):self.c.close();self.s.db.close();self.tmp.cleanup()
    def pub(self):
        (self.w/'output.py').write_text('VALUE=1\n');return self.s.publish(self.r,'worker-3',{'task_id':'a','files':['output.py']})
    def report(self,pub,**kw):
        read=self.c.tool(self.r,'worker-3','read-'+pub['transaction_id'],'read',{'scope':'version','version':pub['version'],'path':'output.py'})
        return self.s.board_tool(self.r,'worker-3','team_report',{'task_id':'a','submission':'ready','version':pub['version'],'publication_id':pub['transaction_id'],'summary':'Ready','checks':[{'criterion':'Content checked','status':'pass','summary':'Read content','evidence_ids':[read['evidence_id']]}],**kw})
    def test_receipt_bound_packet_uses_real_paths_and_check_receipts(self):
        p=self.pub();t=self.report(p);packet=self.s.board_tool(self.r,'worker-4','team_review_input',{'task_id':'a'})
        self.assertEqual(packet['publication_id'],p['transaction_id']);self.assertTrue(Path(packet['artifacts'][0]['absolute_path']).is_file());self.assertTrue(packet['test_receipt_ids'])
        with self.assertRaises(ValueError):self.s.board_tool(self.r,'worker-4','team_review',{'task_id':'a','version':p['version'],'approved':False,'summary':'fix','handoff_id':'invented'})
        self.s.board_tool(self.r,'worker-4','team_review',{'task_id':'a','version':p['version'],'approved':False,'summary':'fix','handoff_id':packet['handoff_id']})
    def test_unknown_and_wrong_task_receipts_do_not_dispatch_review(self):
        p=self.pub()
        with self.assertRaises(ValueError):self.report(p,publication_id='0'*16)
        self.assertEqual(self.s.task(self.r,'a')['state'],'assigned')
        t=self.s.task(self.r,'a');t['id']='other'
        with self.assertRaises(ValueError):handoffs.receipt(self.s,self.r,t,p['transaction_id'])
    def test_repeated_unavailable_input_sends_one_blocker_and_keeps_owner_active(self):
        first=self.s.board_tool(self.r,'worker-4','team_review_input',{'task_id':'a'})
        for _ in range(5):self.assertEqual(self.s.board_tool(self.r,'worker-4','team_review_input',{'task_id':'a'}),first)
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM events WHERE kind='missing_review_input'").fetchone()[0],1)
        self.assertEqual(self.s.task(self.r,'a')['state'],'assigned')
    def test_disappeared_review_file_reopens_owner_and_deduplicates(self):
        p=self.pub();self.report(p);(self.s.directory(self.r)/'versions'/p['version']/'output.py').unlink()
        first=self.s.board_tool(self.r,'worker-4','team_review_input',{'task_id':'a'});second=self.s.board_tool(self.r,'worker-4','team_review_input',{'task_id':'a'})
        self.assertEqual(first,second);self.assertEqual(self.s.task(self.r,'a')['state'],'assigned')
    def test_draft_captures_bytes_missing_files_and_no_review_authority(self):
        (self.w/'output.py').write_text('DRAFT');t=self.s.task(self.r,'a');t['required_outputs'].append('test_output.py');self.s.save_task(self.r,t)
        draft=self.s.board_tool(self.r,'worker-4','team_draft_input',{'task_id':'a'});(self.w/'output.py').write_text('CHANGED')
        self.assertEqual(Path(draft['artifacts'][0]['absolute_path']).read_text(),'DRAFT');self.assertEqual(draft['missing'],['test_output.py']);self.assertFalse(draft['formal_review']);self.assertEqual(self.s.task(self.r,'a')['state'],'assigned')
    def test_messages_cannot_dispatch_review_or_cite_nonexistent_versions(self):
        for text in ['Review request: published version '+('a'*24),'The published version '+('b'*24)+' is ready']:
            with self.assertRaises(ValueError):self.s.board_tool(self.r,'worker-3','team_message',{'kind':'request','recipient':'worker-4','text':text})
        self.s.board_tool(self.r,'worker-3','team_message',{'kind':'information','recipient':'worker-4','text':'Draft feedback: check the initialization logic.'})
    def test_ready_waits_for_independently_completed_design_decision(self):
        self.s.assign(self.r,'lead',{'task_id':'decision','owner':'lead','reviewer':'worker-4','description':'Resolve requirements','acceptance':'Reviewed'})
        t=self.s.task(self.r,'a');t['ready_dependencies']=['decision'];self.s.save_task(self.r,t);p=self.pub()
        with self.assertRaisesRegex(ValueError,'decision is still open'):self.report(p)
        t=self.s.task(self.r,'decision');t['state']='done';self.s.save_task(self.r,t);self.assertEqual(self.report(p)['state'],'review')
        t['state']='assigned';self.s.save_task(self.r,t)
        with self.assertRaisesRegex(ValueError,'decision is still open'):handoffs.validate(self.s,self.r,self.s.task(self.r,'a'))
    def test_version_read_missing_routes_to_one_blocker(self):
        args={'scope':'version','version':'0'*24,'path':'output.py','task_id':'a'}
        first=self.c.execute(self.r,'worker-4','read',args);second=self.c.execute(self.r,'worker-4','read',args)
        self.assertEqual(first['review_input']['blocker_id'],second['review_input']['blocker_id'])
    def test_ready_dependency_cycle_rejected(self):
        t=self.s.task(self.r,'a');t['state']='paused';self.s.save_task(self.r,t)
        with self.assertRaises(ValueError):self.s.assign(self.r,'lead',{'task_id':'a','owner':'worker-3','reviewer':'worker-4','description':'Build','acceptance':'Check','ready_dependencies':['a']})
    def test_numerical_probe_command_is_valid_required_execution(self):
        import subprocess
        from types import SimpleNamespace
        def execute(command,timeout,cwd=None):
            result=subprocess.run(command,shell=True,cwd=cwd or self.w,capture_output=True,text=True,timeout=timeout)
            return {'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr}
        self.c.runtimes[(self.r,'worker-3')]=SimpleNamespace(execute=execute,close=lambda:None)
        p=self.pub();t=self.s.task(self.r,'a');t['required_test_commands']=['python3 output.py'];self.s.save_task(self.r,t)
        executed=self.c.tool(self.r,'worker-3','probe','run',{'command':'python3 output.py'})
        self.assertNotIn('error',executed)
        checks=[{'criterion':'Content checked','status':'pass','summary':'Executed numerical probe','evidence_ids':[executed['evidence_id']]}]
        self.assertEqual(self.report(p,checks=checks)['state'],'review')
