import json
import tempfile
import time
import subprocess
import unittest
from pathlib import Path
from server import Controller
import workflow

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.c=Controller(Path(self.tmp.name),'http://127.0.0.1:1');self.s=self.c.store;self.r=self.s.create('Check workflow')
        self.s.delivered([m['id'] for m in self.s.pending(self.r,'lead')],'done')
    def tearDown(self):self.c.close();self.s.db.close();self.tmp.cleanup()
    def assign(self,id='a',owner='worker-3',deps=None,**kw):
        return self.s.assign(self.r,'lead',{'task_id':id,'owner':owner,'reviewer':'worker-4','description':'Implement','acceptance':'Checked','dependencies':deps or [],**kw})
    def test_cancelled_dependency_rejected(self):
        self.assign();self.s.board_tool(self.r,'lead','team_cancel_task',{'task_id':'a','reason':'Superseded'})
        with self.assertRaisesRegex(ValueError,'cancelled'):self.assign('b',deps=['a'])
    def test_transitive_unreachable_dependency_rejected_and_existing_flagged(self):
        self.assign();self.assign('b',deps=['a'])
        self.s.board_tool(self.r,'lead','team_cancel_task',{'task_id':'a','reason':'Superseded'})
        with self.assertRaisesRegex(ValueError,'unreachable'):self.assign('c',deps=['b'])
        issues=self.s.status(self.r)['workflow_issues'];self.assertEqual(issues[0]['task_id'],'b')
        self.assertEqual(issues[0]['problems'][0]['path'],['b','a'])
    def test_file_claim_requires_binding_or_proposed_output(self):
        with self.assertRaisesRegex(ValueError,'Unbound artifact'):self.assign(description='Read missing.py')
        self.assign(description='Create new.py',proposed_artifacts=['new.py'])
    def reference(self):
        p=self.s.directory(self.r)/'agents/lead/workspace/source.py';p.write_text('VALUE=1\n')
        return p,workflow.describe_references(self.s,self.r,'lead',['source.py'])
    def test_full_hash_and_source_version_are_verified(self):
        p,refs=self.reference()
        self.assertEqual(len(refs[0]['sha256']),64)
        for bad in ({**refs[0],'sha256':'123'}, {**refs[0],'source_version':'git:'+'0'*40}):
            with self.assertRaises(ValueError):self.assign(references=[bad])
        self.assign(description='Inspect source.py',references=refs)
    def test_mutated_input_blocks_dispatch(self):
        self.assign('parent');p,refs=self.reference();self.assign('b',deps=['parent'],references=refs)
        p.write_text('VALUE=2\n');t=self.s.task(self.r,'parent');t['state']='done';self.s.save_task(self.r,t);self.s.dispatch_ready(self.r)
        child=self.s.task(self.r,'b');self.assertEqual(child['state'],'queued');self.assertIn('hash changed',child['handoff_error'])
    def test_mutated_input_blocks_claim_for_already_assigned_task(self):
        p,refs=self.reference();self.assign(references=refs);p.write_text('changed')
        self.assertEqual(self.s.claim_pending(self.r,'worker-3'),[])
        self.assertEqual(self.s.task(self.r,'a')['state'],'blocked')
    def test_full_git_source_identity_and_changed_head(self):
        root=Path(self.tmp.name)/'source';root.mkdir();(root/'input.py').write_text('VALUE=1')
        def git(*args):return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.DEVNULL,text=True).strip()
        git('init','-q');git('add','input.py');git('-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','first')
        refs=workflow.describe_references(self.s,self.r,'lead',['input.py'],str(root))
        self.assertEqual(refs[0]['source_version'],'git:'+git('rev-parse','HEAD'))
        self.assertEqual(len(refs[0]['source_version']),44)
        (root/'other.py').write_text('NEW=1');git('add','other.py');git('-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','second')
        with self.assertRaisesRegex(ValueError,'source version changed'):workflow.validate_references(refs)
    def test_invalid_handoff_cannot_be_approved(self):
        self.assign();t=self.s.board_tool(self.r,'worker-3','team_report',{'task_id':'a','summary':'Draft'})
        t['handoff_error']='Missing source';self.s.save_task(self.r,t)
        args={'task_id':'a','version':t['version'],'summary':'Reviewed','approved':True}
        with self.assertRaisesRegex(ValueError,'invalid handoff'):self.s.board_tool(self.r,'worker-4','team_review',args)
        self.s.board_tool(self.r,'worker-4','team_review',{**args,'approved':False})
    def test_short_unverified_commit_claim_rejected(self):
        with self.assertRaisesRegex(ValueError,'Unverified commit'):self.assign(description='Use commit c23835b')
    def test_receipt_does_not_resolve_message(self):
        mid=self.s.message(self.r,'worker-3','lead',{'kind':'question','text':'Which approach?'},True)['message_id']
        self.s.claim_pending(self.r,'lead');self.s.settle_delivery(self.r,'lead','done')
        self.assertEqual(workflow.actions(self.s,self.r)[0]['state'],'open')
        with self.assertRaises(PermissionError):workflow.resolve(self.s,self.r,'worker-4',{'message_id':mid,'summary':'Answer'})
        workflow.resolve(self.s,self.r,'lead',{'message_id':mid,'summary':'Use the identity-preserving variant; review before scoring'})
        self.assertEqual(workflow.actions(self.s,self.r)[0]['state'],'resolved')
    def test_design_question_requires_decision_despite_task_transition(self):
        self.assign()
        self.s.message(self.r,'worker-3','lead',{'kind':'question','task_id':'a','text':'Which parameter budget should we use?'},True)
        self.s.board_tool(self.r,'worker-3','team_report',{'task_id':'a','summary':'Draft'})
        rows=workflow.actions(self.s,self.r)
        self.assertEqual(rows[0]['state'],'open')
    def test_task_transition_resolves_review_without_auto_approval(self):
        self.assign();t=self.s.board_tool(self.r,'worker-3','team_report',{'task_id':'a','summary':'Ready'})
        self.assertEqual(workflow.actions(self.s,self.r)[0]['state'],'open')
        self.s.board_tool(self.r,'worker-4','team_review',{'task_id':'a','version':t['version'],'approved':False,'summary':'Fix'})
        acts=workflow.actions(self.s,self.r)
        self.assertEqual(acts[0]['state'],'resolved');self.assertEqual(self.s.task(self.r,'a')['state'],'assigned')
    def test_review_prioritized_despite_busy_peer_and_reminders_are_bounded(self):
        self.assign();self.s.board_tool(self.r,'worker-3','team_report',{'task_id':'a','summary':'Ready'})
        now=time.time()+40;activity={(self.r,a):{'state':'working','updated':now} for a in ('lead','worker-3','worker-4','worker-1','worker-2')}
        activity[(self.r,'worker-3')]={'state':'waiting','updated':now-40}
        first=self.c.progress.check(self.r,activity,now=now,repair=True)
        self.assertEqual(first['state'],'coordination_required');self.assertEqual(first['issues'][0]['kind'],'blocking_review')
        self.c.progress.check(self.r,activity,now=now+65,repair=True)
        self.c.progress.check(self.r,activity,now=now+70,repair=True)
        rows=self.s.db.execute('SELECT attempts FROM workflow_alerts').fetchall()
        self.assertEqual(max(r[0] for r in rows),2)
        self.assertEqual(self.s.task(self.r,'a')['state'],'review')
    def test_correction_cannot_self_depend_and_cycles_rejected(self):
        self.assign();self.assign('b',deps=['a']);self.s.board_tool(self.r,'lead','team_pause_task',{'task_id':'a'})
        with self.assertRaises(ValueError):self.assign(deps=['b'])
