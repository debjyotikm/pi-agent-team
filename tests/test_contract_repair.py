import json,tempfile,time,unittest
from pathlib import Path
from server import Controller
import acceptance,obligations,workflow

class ContractRepairTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.c=Controller(Path(self.tmp.name),'http://127.0.0.1:1');self.s=self.c.store;self.r=self.s.create('Verify contracts');(self.s.root/'strict-workflow.json').write_text('{}')
  self.assign('a')
 def tearDown(self):self.c.close();self.s.db.close();self.tmp.cleanup()
 def assign(self,name,reviewer='worker-4'):
  return self.s.assign(self.r,'lead',{'task_id':name,'owner':'worker-3','reviewer':reviewer,'description':'Inspect output','acceptance':'Correct output','acceptance_checks':['File is readable','Content is correct'],'required_outputs':['out.txt'],'proposed_artifacts':['out.txt']})
 def evidence(self,actor='worker-4'):
  return self.s.event(self.r,actor,'tool_receipt',{'tool':'read','args':{},'result':{'content':'Scope and artifact inspected'}})
 def cancel(self,name='a'):
  return self.s.board_tool(self.r,'lead','team_cancel_task',{'task_id':name,'reason':'Scope transferred'})
 def test_unfilled_exact_template_and_structured_mismatch(self):
  template=self.s.board_tool(self.r,'worker-3','team_submission_contract',{'task_id':'a'});self.assertEqual([x['status'] for x in template['checks']],['pending','pending'])
  w=self.s.directory(self.r)/'agents/worker-3/workspace/out.txt';w.write_text('Correct');p=self.s.publish(self.r,'worker-3',{'task_id':'a','files':['out.txt']})
  result=self.c.tool(self.r,'worker-3','bad','team_report',{'task_id':'a','submission':'ready','version':p['version'],'publication_id':p['transaction_id'],'summary':'Done','checks':[{'criterion':'Other task','status':'pass','summary':'x','evidence_ids':[1]}]})
  self.assertEqual(result['diagnostic']['missing_criteria'],['Content is correct','File is readable']);self.assertEqual(result['diagnostic']['unexpected_criteria'],['Other task']);self.assertEqual(self.s.task(self.r,'a')['state'],'assigned')
 def test_cancellation_does_not_erase_obligation_or_allow_finish(self):
  self.cancel();self.assertEqual(len(obligations.unresolved(self.s,self.r)),1)
  with self.assertRaisesRegex(ValueError,'cancellation obligations'):self.s.board_tool(self.r,'lead','team_finish',{'version':'x','summary':'Done','validation':'yes'})
 def test_owner_and_lead_cannot_self_clear_cancellation(self):
  self.cancel()
  for actor in ('lead','worker-3'):
   with self.assertRaises(PermissionError):obligations.review(self.s,self.r,actor,{'task_id':'a','disposition':'unnecessary','summary':'No need','evidence_ids':[self.evidence(actor)]})
 def test_transfer_stays_open_until_successor_independently_done(self):
  self.assign('b');self.cancel();eid=self.evidence()
  obligations.review(self.s,self.r,'worker-4',{'task_id':'a','disposition':'superseded','replacement_task_id':'b','summary':'All criteria carried by b','evidence_ids':[eid]})
  self.assertTrue(obligations.unresolved(self.s,self.r))
  t=self.s.task(self.r,'b');t['state']='done';self.s.save_task(self.r,t);self.assertFalse(obligations.unresolved(self.s,self.r))
  t['state']='assigned';self.s.save_task(self.r,t);self.assertTrue(obligations.unresolved(self.s,self.r))
 def test_cancelled_or_self_replacement_rejected(self):
  self.assign('b');self.cancel('b');self.cancel()
  for target in ('a','b','missing'):
   with self.assertRaises(ValueError):obligations.review(self.s,self.r,'worker-4',{'task_id':'a','disposition':'superseded','replacement_task_id':target,'summary':'Transfer','evidence_ids':[self.evidence()]})
 def test_lead_review_gets_independent_cancellation_reviewer(self):
  self.assign('b',reviewer='lead');t=self.cancel('b');self.assertNotIn(t['retirement_obligation']['reviewer'],('lead','worker-3'))
 def test_blocked_decision_remains_visible_after_reminders_exhaust(self):
  t=self.s.task(self.r,'a');t['state']='blocked';self.s.save_task(self.r,t);now=time.time()+500
  issue=next(x for x in workflow.priorities(self.s,self.r,{},now) if x['kind']=='blocked_decision')
  workflow.alert(self.s,self.r,issue,now);workflow.alert(self.s,self.r,issue,now+65)
  again=next(x for x in workflow.priorities(self.s,self.r,{},now+200) if x['kind']=='blocked_decision')
  self.assertEqual(again['escalation'],'lead_decision_required');self.assertEqual(again['reminders_sent'],2);self.assertIsNone(workflow.alert(self.s,self.r,again,now+200))
 def test_reassignment_retains_prior_contract(self):
  self.s.board_tool(self.r,'lead','team_pause_task',{'task_id':'a'});self.assign('a');self.assertEqual(self.s.task(self.r,'a')['contract_history'][-1]['acceptance_checks'],['File is readable','Content is correct'])
