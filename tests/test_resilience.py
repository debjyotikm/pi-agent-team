import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from server import Controller
import resilience
from store import digest,manifest,revision


class ResilienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.c=Controller(Path(self.tmp.name),'http://127.0.0.1:1');self.s=self.c.store;self.r=self.s.create('Verify recovery')
    def tearDown(self):self.c.close();self.s.db.close();self.tmp.cleanup()
    def assign(self,owner='worker-3',reviewer='lead',**kw):
        return self.s.assign(self.r,'lead',{'task_id':'a','owner':owner,'reviewer':reviewer,'description':'Inspect deliverable','acceptance':'Check contents',**kw})
    def sample(self,agent='lead',value='same',command='sha256sum file && date',version='a'):
        result={'stdout':value,'exit_code':0,'workspace_version_before':version,'workspace_version_after':version}
        eid=self.s.event(self.r,agent,'tool_receipt',{'tool':'run','args':{'command':command},'result':result})
        return resilience.observe(self.s,self.r,agent,'run',{'command':command},result,eid)
    def test_dates_do_not_hide_repetition_and_recovery_is_bounded_durable(self):
        for i in range(3):self.assertIsNone(self.sample(value=f'abc file\nMon Jan 01 12:3{i}:00 UTC 2024'))
        r=self.sample(value='abc file\nMon Jan 01 12:34:00 UTC 2024');self.assertEqual(r['recovery'],'pending')
        for _ in range(3):self.assertIsNone(self.sample())
        self.assertEqual(self.sample()['recovery'],'stalled')
        self.assertTrue(resilience.stalled(self.s,self.r,'lead'));self.assertFalse(resilience.stalled(self.s,self.r,'worker-3'))
        resilience.reset(self.s,self.r,'lead');self.assertIsNone(resilience.state(self.s,self.r,'lead'))
    def test_repeated_list_result_yields_without_corrupting_call_receipt(self):
        self.c.execute=lambda *args:[{'path':'same','sha256':'same'}]
        for i in range(4):result=self.c.tool(self.r,'lead',str(i),'team_reference',{'paths':['same']})
        self.assertTrue(result['yield']);self.assertEqual(result['data'][0]['path'],'same')
        self.assertEqual(self.s.db.execute("SELECT state FROM calls WHERE id='3'").fetchone()[0],'done')
    def test_real_output_and_source_changes_are_not_loops(self):
        for i in range(10):self.assertIsNone(self.sample(value=str(i)))
        for i in range(10):self.assertIsNone(self.sample(version=str(i)))
    def test_equivalent_command_date_suffix(self):
        self.assertEqual(resilience.normalize_command('sha256sum a && date'),resilience.normalize_command('sha256sum a; date'))
        self.assertNotEqual(resilience.normalize_command('echo "date"'),resilience.normalize_command('date'))
    def test_real_process_poll_lease_and_exit(self):
        p=subprocess.Popen(['sleep','10'])
        try:
            args={'pid':p.pid,'command':'ps -p '+str(p.pid),'expires_seconds':60}
            lease=resilience.register_poll(self.s,self.r,'worker-3',args)
            request={'command':args['command'],'poll_job_id':lease['job_id']};resilience.poll_valid(self.s,self.r,'worker-3',request)
            for i in range(8):self.assertIsNone(resilience.observe(self.s,self.r,'worker-3','run',request,{'stdout':'running'},i))
            with self.assertRaises(ValueError):resilience.poll_valid(self.s,self.r,'worker-3',{**request,'command':'other'})
            p.terminate();p.wait()
            with self.assertRaises(ValueError):resilience.poll_valid(self.s,self.r,'worker-3',request)
        finally:
            if p.poll() is None:p.terminate();p.wait()
    def stall(self,actor):
        self.s.db.execute('INSERT OR REPLACE INTO participant_recovery VALUES(?,?,?,?,?,?)',(self.r,actor,2,'stalled','{}',time.time()));self.s.db.commit()
    def test_exact_review_delegation_rejects_self_and_stale_reviewer(self):
        self.assign();t=self.s.board_tool(self.r,'worker-3','team_report',{'task_id':'a','summary':'Ready'})
        self.stall('lead')
        with self.assertRaises(PermissionError):resilience.claim_review(self.s,self.r,'worker-3',{'task_id':'a','version':t['version'],'reason':'self'})
        new=resilience.claim_review(self.s,self.r,'worker-4',{'task_id':'a','version':t['version'],'reason':'unblock'})
        self.assertEqual(t['version'],new['version']);self.assertFalse(resilience.review_eligible(self.s,self.r,new))
        with self.assertRaises(PermissionError):self.s.board_tool(self.r,'lead','team_review',{'task_id':'a','version':t['version'],'approved':True,'summary':'old'})
    def test_legacy_review_uses_durable_reminder_age_and_does_not_ping_pong(self):
        self.assign();t=self.s.board_tool(self.r,'worker-3','team_report',{'task_id':'a','summary':'Ready'})
        t.pop('state_changed_at',None)
        self.s.db.execute('UPDATE tasks SET data=? WHERE run=? AND id=?',(json.dumps(t),self.r,'a'));self.s.db.commit()
        for _ in range(2):
            eid=self.s.event(self.r,'controller','workflow_alert',{'issue':{'kind':'blocking_review','task_id':'a','reviewer':'lead','version':t['version']}})
            self.s.db.execute('UPDATE events SET created=? WHERE id=?',(time.time()-180,eid));self.s.db.commit()
        self.assertTrue(resilience.review_eligible(self.s,self.r,t))
        new=resilience.claim_review(self.s,self.r,'worker-4',{'task_id':'a','version':t['version'],'reason':'overdue'})
        self.assertFalse(resilience.review_eligible(self.s,self.r,new))
    def test_claim_existing_task_preserves_scope_and_blocks_active_execution(self):
        self.assign(owner='lead',reviewer='worker-3');self.stall('lead')
        self.s.db.execute('INSERT INTO calls VALUES(?,?,?,?,?,?)',(self.r,'lead','inflight','{}',None,'executing'));self.s.db.commit()
        with self.assertRaises(ValueError):resilience.claim_task(self.s,self.r,'worker-4',{'task_id':'a','reason':'help'})
        self.s.db.execute("UPDATE calls SET state='done'");self.s.db.commit()
        t=resilience.claim_task(self.s,self.r,'worker-4',{'task_id':'a','reason':'help'})
        self.assertEqual(t['acceptance'],'Check contents');self.assertEqual(t['owner'],'worker-4');self.assertIn('lead/workspace',t['previous_owner_workspace'])
    def strict_task(self):
        (self.s.root/'strict-workflow.json').write_text('{}');self.assign()
        p=self.s.directory(self.r)/'agents/worker-3/workspace/deliverable.txt';p.write_text('correct')
        pub=self.s.publish(self.r,'worker-3',{'task_id':'a','files':['deliverable.txt']});self.publication_id=pub['transaction_id'];return pub['version']
    def evidence(self,actor,version,fail=False):
        data={'tool':'read','args':{'scope':'version','version':version,'path':'deliverable.txt'},'result':{'sha256':digest(b'correct'),'content':'correct'}}
        if fail:data['result']['error']='failure'
        return self.s.event(self.r,actor,'tool_receipt',data)
    def checks(self,eid):return [{'criterion':'Check contents','status':'pass','summary':'Inspected correct contents','evidence_ids':[eid]}]
    def test_partial_submission_retains_work_and_does_not_create_review(self):
        v=self.strict_task();t=self.s.board_tool(self.r,'worker-3','team_report',{'task_id':'a','summary':'partial submission','submission':'partial','remaining':['fix calculation'],'checks':[]})
        self.assertEqual(t['state'],'assigned');self.assertEqual(t['version'],v);self.assertEqual(t['remaining'],['fix calculation'])
    def test_explicit_blocker_takes_precedence_over_partial_status(self):
        self.strict_task();t=self.s.board_tool(self.r,'worker-3','team_report',{'task_id':'a','summary':'Still open: unavailable input','submission':'partial','blocked':True,'remaining':['obtain input'],'checks':[]})
        self.assertEqual(t['state'],'blocked')
    def test_ready_requires_exact_evidence_and_independent_approval(self):
        v=self.strict_task();eid=self.evidence('worker-3',v)
        args={'task_id':'a','summary':'Ready','submission':'ready','publication_id':self.publication_id,'remaining':[],'version':v,'checks':self.checks(eid)}
        with self.assertRaises(ValueError):self.s.board_tool(self.r,'worker-3','team_report',{**args,'checks':[]})
        t=self.s.board_tool(self.r,'worker-3','team_report',args);self.assertEqual(t['state'],'review')
        review={'task_id':'a','version':v,'approved':True,'handoff_id':t['review_handoff']['handoff_id'],'summary':'Independent verification','checks':self.checks(eid)}
        with self.assertRaises(ValueError):self.s.board_tool(self.r,'lead','team_review',review)
        review['checks']=self.checks(self.evidence('lead',v));self.assertEqual(self.s.board_tool(self.r,'lead','team_review',review)['state'],'done')
    def test_failed_receipt_cannot_be_pass(self):
        v=self.strict_task();args={'task_id':'a','summary':'Ready','submission':'ready','publication_id':self.publication_id,'version':v,'checks':self.checks(self.evidence('worker-3',v,True))}
        with self.assertRaises(ValueError):self.s.board_tool(self.r,'worker-3','team_report',args)
    def test_required_failed_test_is_not_hidden_by_passing_read(self):
        v=self.strict_task();t=self.s.task(self.r,'a');t['required_test_commands']=['python -m pytest'];self.s.save_task(self.r,t)
        self.s.event(self.r,'worker-3','tool_receipt',{'tool':'run','args':{'command':'python -m pytest'},'result':{'exit_code':1,'stdout':'9 failed, 12 passed'}})
        args={'task_id':'a','summary':'Ready','submission':'ready','publication_id':self.publication_id,'version':v,'checks':self.checks(self.evidence('worker-3',v))}
        with self.assertRaisesRegex(ValueError,'required test command'):self.s.board_tool(self.r,'worker-3','team_report',args)
    def test_actor_recovery_closes_only_affected_runtime_and_stops_after_second_loop(self):
        c=self.c;s=self.s;r=self.r;closed=[];calls=[]
        class Fake:
            def __init__(self,a):self.agent=a;self.rpc=self
            def close(self):closed.append(self.agent)
            def prompt(self,text):
                calls.append(self.agent)
                for i in range(4):
                    result=c.tool(r,self.agent,f'{len(calls)}-{i}','run',{'command':'sha256sum file && date'})
                    if result.get('yield'):break
        c.ensure_runtime=lambda run,agent:c.runtimes.setdefault((run,agent),Fake(agent))
        c.execute=lambda *args:{'stdout':'abc file','exit_code':0,'workspace_version_before':'same','workspace_version_after':'same'}
        c.runtimes[(r,'worker-4')]=Fake('worker-4')
        th=threading.Thread(target=c.actor,args=(r,'lead'));th.start()
        try:
            deadline=time.time()+5
            while time.time()<deadline and c.activity.get((r,'lead'),{}).get('state')!='stalled':time.sleep(.02)
            self.assertEqual(c.activity[(r,'lead')]['state'],'stalled');self.assertEqual(len(calls),2)
            self.assertEqual(closed,['lead']);n=len(calls);time.sleep(.1);self.assertEqual(len(calls),n)
        finally:c.closing.set();th.join(3)
