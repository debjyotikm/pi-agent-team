import json
import tempfile
import time
import unittest
from pathlib import Path
from server import Controller
import completion_health,workflow,resilience

class CompletionHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.c=Controller(Path(self.tmp.name),'http://127.0.0.1:1');self.s=self.c.store
        self.r=self.s.create('Test completion');(self.s.root/'strict-workflow.json').write_text('{}')
        self.s.assign(self.r,'lead',{'task_id':'a','owner':'worker-3','reviewer':'worker-4','description':'Create an audit','acceptance':'Audit is reproducible','required_outputs':['audit.md'],'proposed_artifacts':['audit.md']})
        self.now=time.time()+1000;self.activity={(self.r,a):{'state':'working','updated':self.now} for a in ('lead','worker-3','worker-4','worker-1','worker-2')}
    def tearDown(self):self.c.close();self.s.db.close();self.tmp.cleanup()
    def publish(self,name='audit.md'):
        w=self.s.directory(self.r)/'agents/worker-3/workspace';(w/name).write_text('Draft audit')
        return self.s.publish(self.r,'worker-3',{'task_id':'a','files':[name]})
    def issues(self):return completion_health.issues(self.s,self.r,self.activity,self.now)
    def test_idle_owner_detected_despite_busy_lead_and_bounded_escalation(self):
        self.activity[(self.r,'worker-3')]={'state':'waiting','updated':self.now-180}
        result=self.c.progress.check(self.r,self.activity,now=self.now,repair=True)
        self.assertEqual(result['state'],'coordination_required');self.assertEqual(result['issues'][0]['kind'],'idle_assignment')
        self.c.progress.check(self.r,self.activity,now=self.now+65,repair=True)
        self.c.progress.check(self.r,self.activity,now=self.now+150,repair=True)
        rows=self.s.db.execute("SELECT recipient,body FROM messages WHERE sender='controller'").fetchall()
        alerts=[(r['recipient'],json.loads(r['body'])) for r in rows if json.loads(r['body']).get('kind')=='workflow_alert']
        self.assertEqual([a for a,b in alerts],['worker-3','lead']);self.assertEqual(self.s.task(self.r,'a')['state'],'assigned')
    def test_executing_call_is_not_idle(self):
        self.activity[(self.r,'worker-3')]={'state':'waiting','updated':self.now-180}
        self.s.db.execute('INSERT INTO calls VALUES(?,?,?,?,?,?)',(self.r,'worker-3','x','{}',None,'executing'))
        self.assertEqual(self.issues(),[])
    def test_published_missing_output_visible_until_actual_path_reconciled(self):
        self.publish('corrected.md');self.assertIn('publication_output_gap',[x['kind'] for x in self.issues()])
        t=self.s.task(self.r,'a');t['required_outputs']=['corrected.md'];self.s.save_task(self.r,t)
        self.assertNotIn('publication_output_gap',[x['kind'] for x in self.issues()])
    def test_partial_remaining_prevents_publication_alert_but_not_idle_action(self):
        self.publish();self.assertIn('publication_without_resolution',[x['kind'] for x in self.issues()])
        self.s.board_tool(self.r,'worker-3','team_report',{'task_id':'a','submission':'partial','summary':'Draft','remaining':['Finish reproducibility check']})
        self.assertEqual(self.issues(),[])
        self.activity[(self.r,'worker-3')]={'state':'waiting','updated':self.now-180}
        self.assertEqual([x['kind'] for x in self.issues()],['idle_assignment'])
    def test_new_publication_requires_new_disposition(self):
        self.publish();self.s.board_tool(self.r,'worker-3','team_report',{'task_id':'a','submission':'partial','summary':'Draft','remaining':['Check it']})
        self.publish();self.assertIn('publication_without_resolution',[x['kind'] for x in self.issues()])
    def test_blocked_and_done_do_not_receive_owner_idle_alerts(self):
        self.activity[(self.r,'worker-3')]={'state':'waiting','updated':self.now-180}
        for state in ('blocked','done','paused'):
            t=self.s.task(self.r,'a');t['state']=state;self.s.save_task(self.r,t);self.assertNotIn('idle_assignment',[i['kind'] for i in self.issues()])
            if state!='blocked':self.assertEqual(self.issues(),[])
    def test_changed_search_commands_trigger_bounded_recovery(self):
        for i in range(4):
            result=resilience.observe(self.s,self.r,'worker-3','run',{'command':f'find /tmp/search{i} -name "absent.py"'},
                {'exit_code':0,'stdout':f'Searched root {i}\n','stderr':'','workspace_version_before':'same','workspace_version_after':'same'},i)
        self.assertEqual(result['recovery'],'pending')
    def test_successful_or_ambiguous_searches_do_not_count(self):
        args={'command':'find /tmp -name "present.py"'}
        for output in ({'stdout':'/tmp/present.py','exit_code':0},{'stdout':'','stderr':'Permission denied','exit_code':1}):self.assertIsNone(resilience.unsuccessful_search(args,output))
        self.assertIsNone(resilience.unsuccessful_search({'command':'grep -n missing.py source.md'},{'stdout':'','exit_code':1}))
        self.assertIsNone(resilience.unsuccessful_search({'command':'find /tmp -name "*.py"'},{'stdout':''}))
    def test_workspace_change_clears_search_history(self):
        for i in range(3):resilience.observe(self.s,self.r,'worker-3','run',{'command':f'find /tmp/{i} -name absent.py'},{'stdout':'','exit_code':0},i)
        resilience.observe(self.s,self.r,'worker-3','write',{}, {'changed':True},4)
        self.assertIsNone(resilience.observe(self.s,self.r,'worker-3','run',{'command':'find /tmp/x -name absent.py'},{'stdout':'','exit_code':0},5))
    def test_retired_task_handoff_errors_are_not_active_blockers(self):
        t=self.s.task(self.r,'a');t['state']='cancelled';t['handoff_error']='historical missing source';self.s.save_task(self.r,t)
        self.assertEqual(workflow.priorities(self.s,self.r,self.activity,self.now),[])
