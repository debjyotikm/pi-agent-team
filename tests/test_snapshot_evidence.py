import json,subprocess,tempfile,unittest
from pathlib import Path
from server import Controller
import snapshot_evidence,acceptance
class LocalRuntime:
 def execute(self,command,timeout,cwd=None):
  p=subprocess.run(['bash','-e','-o','pipefail','-c',command],cwd=cwd,capture_output=True,text=True,timeout=timeout)
  return {'exit_code':p.returncode,'stdout':p.stdout,'stderr':p.stderr}
 def close(self):pass
class SnapshotEvidenceTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.c=Controller(Path(self.tmp.name),'http://127.0.0.1:1');self.s=self.c.store;self.r=self.s.create('Snapshot acceptance');(self.s.root/'strict-workflow.json').write_text('{}')
  self.cmd='python3 verify.py'
  self.s.assign(self.r,'lead',{'task_id':'a','owner':'worker-3','reviewer':'worker-4','description':'Verify','acceptance':'Correct','required_outputs':['verify.py','report.md','result.json'],'proposed_artifacts':['verify.py','report.md','result.json'],'required_test_commands':[self.cmd],'snapshot_verification':True,'report_outputs':['report.md'],'verification_outputs':['result.json']})
  self.w=self.s.directory(self.r)/'agents/worker-3/workspace';(self.w/'verify.py').write_text('import json\nassert 6*7==42\nopen("result.json","w").write(json.dumps({"value":42}))\n')
  self.p=self.s.publish(self.r,'worker-3',{'task_id':'a','files':['verify.py']})
  for a in ('worker-3','worker-4'):self.c.runtimes[(self.r,a)]=LocalRuntime()
 def tearDown(self):self.c.close();self.s.db.close();self.tmp.cleanup()
 def call(self,actor,op,args):return self.c.tool(self.r,actor,str(self.s.db.execute('select count(*) from events').fetchone()[0]),op,args)
 def verify(self,actor='worker-3',command=None):return self.call(actor,'team_verify',{'task_id':'a','version':self.p['version'],'command':command or self.cmd})
 def ready(self,eid,**kw):return self.call('worker-3','team_report',{'task_id':'a','version':self.p['version'],'publication_id':self.p['transaction_id'],'submission':'ready','remaining':[],'summary':'No unfinished items remain','checks':[{'criterion':'Correct','status':'pass','summary':'Executed check','evidence_ids':[eid]}],**kw})
 def finish_outputs(self,v):
  self.assertNotIn('error',v,v)
  (self.w/'result.json').write_bytes(Path(v['artifacts'][0]['absolute_path']).read_bytes());(self.w/'report.md').write_text('Verified 42.\n')
  self.p=self.s.publish(self.r,'worker-3',{'task_id':'a','files':['report.md','result.json']})
 def test_full_cycle_report_edit_reuses_source_receipt(self):
  v=self.verify();old=self.p['version'];self.finish_outputs(v);self.assertNotEqual(old,self.p['version'])
  cat=self.call('worker-3','team_evidence_catalog',{'task_id':'a'});self.assertIn(v['evidence_id'],[x['evidence_id'] for x in cat['eligible']])
  t=self.ready(v['evidence_id']);self.assertEqual(t.get('state'),'review',t)
  rv=self.verify('worker-4');self.assertNotIn('error',rv,rv)
  packet=self.call('worker-4','team_review_input',{'task_id':'a'})
  result=self.call('worker-4','team_review',{'task_id':'a','version':self.p['version'],'handoff_id':packet['handoff_id'],'approved':True,'summary':'Independent execution passed','checks':[{'criterion':'Correct','status':'pass','summary':'Independent run','evidence_ids':[rv['evidence_id']]}]})
  self.assertEqual(result.get('state'),'done',result)
 def test_source_change_rejects_old_receipt(self):
  v=self.verify();self.finish_outputs(v);(self.w/'verify.py').write_text((self.w/'verify.py').read_text()+'# changed source\n');self.p=self.s.publish(self.r,'worker-3',{'task_id':'a','files':['verify.py']})
  result=self.ready(v['evidence_id']);self.assertIn('source or declared',result['error']);self.assertEqual(self.s.task(self.r,'a')['state'],'assigned')
 def test_wrong_event_and_wrong_actor_explained(self):
  v=self.verify();self.finish_outputs(v);event=self.s.event(self.r,'worker-3','message_end',{});other=self.s.event(self.r,'worker-4','tool_receipt',{'tool':'read','args':{},'result':{}})
  result=self.ready(event);self.assertEqual(result['diagnostic']['code'],'invalid_evidence')
  cat=self.call('worker-3','team_evidence_catalog',{'task_id':'a','evidence_ids':[event,other,999999]});self.assertEqual(len(cat['ineligible']),3)
 def test_generated_output_must_match(self):
  v=self.verify();self.finish_outputs(v);(self.w/'result.json').write_text('{}');self.p=self.s.publish(self.r,'worker-3',{'task_id':'a','files':['result.json']});self.assertIn('generated output',self.ready(v['evidence_id'])['error'])
 def test_source_mutation_fails_verification(self):
  v=self.verify(command="echo changed >> verify.py");self.assertIn('modified captured source',v['error'])
 def test_latest_failure_cannot_be_hidden(self):
  v=self.verify();self.finish_outputs(v)
  # Same command and source binding, failed native execution; preserved as a real failure event.
  failure=dict(v,exit_code=1);self.s.event(self.r,'worker-3','tool_receipt',{'tool':'team_verify','args':{'task_id':'a','command':self.cmd},'result':failure})
  self.assertIn('unresolved failure',self.ready(v['evidence_id'])['error'])
 def test_ready_conflict_and_wrong_review_are_structured(self):
  result=self.ready(1,remaining=['Still work']);self.assertEqual(result['diagnostic']['code'],'invalid_task_transition')
  result=self.call('worker-4','team_review',{'task_id':'a','version':self.p['version'],'approved':True,'checks':[],'summary':'x'});self.assertEqual(result['diagnostic']['state'],'assigned');self.assertEqual(result['diagnostic']['owner'],'worker-3')
 def test_code_cannot_be_report_exclusion(self):
  with self.assertRaisesRegex(ValueError,'Markdown'):snapshot_evidence.policy({'report_outputs':['verify.py'],'required_outputs':['verify.py']})
 def test_retained_result_tamper_rejected(self):
  v=self.verify();self.finish_outputs(v);p=Path(v['record_path']);r=json.loads(p.read_text());r['environment']['platform']='tampered';p.write_text(json.dumps(r));self.assertIn('inconsistent',self.ready(v['evidence_id'])['error'])
