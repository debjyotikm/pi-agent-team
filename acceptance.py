"""Evidence-backed submissions; draft work never gives up its active assignment."""
import json
import re
from pathlib import Path
from store import digest,manifest,revision

class TransitionError(ValueError):
    def __init__(self,task,message):
        t=task or {}
        self.details={'code':'invalid_task_transition','task_id':t.get('id'),'state':t.get('state'),'owner':t.get('owner'),'reviewer':t.get('reviewer'),'version':t.get('version'),'handoff_id':(t.get('review_handoff') or {}).get('handoff_id'),'instruction':message}
        super().__init__(message+'; '+json.dumps(self.details))

class TransitionPermissionError(TransitionError,PermissionError):pass

class CriteriaMismatch(ValueError):
    def __init__(self, expected, supplied):
        self.details={'code':'acceptance_criteria_mismatch','expected_criteria':expected,
            'missing_criteria':[x for x in expected if x not in supplied],
            'unexpected_criteria':[x for x in supplied if x not in expected],
            'duplicate_criteria':[x for x in dict.fromkeys(supplied) if supplied.count(x)>1],
            'instruction':'Use team_submission_contract(task_id) and copy each exact criterion once. Populate status and your own real evidence; do not rename or split criteria.'}
        super().__init__('Acceptance criteria mismatch: '+json.dumps(self.details))

def contract(s,run,actor,args):
    t=s.task(run,args['task_id'])
    if not t:raise ValueError('Unknown task')
    return {'task_id':t['id'],'state':t['state'],'owner':t['owner'],'reviewer':t['reviewer'],'version':t.get('version'),
        'publication_id':(t.get('publication_receipts') or [{}])[-1].get('transaction_id'),
        'handoff_id':(t.get('review_handoff') or {}).get('handoff_id'),
        'checks':[{'criterion':x,'status':'pending','summary':'','evidence_ids':[]} for x in t.get('acceptance_checks') or [t['acceptance']]],
        'required_outputs':t.get('required_outputs',[]),'required_test_commands':t.get('required_test_commands',[]),
        'snapshot_verification':t.get('snapshot_verification',False),'report_outputs':t.get('report_outputs',[]),'verification_outputs':t.get('verification_outputs',[]),
        'ready_dependencies':t.get('ready_dependencies',[]),'instruction':'This is an unfilled template, not evidence or approval. Ready requires every criterion passing on your own real receipts and all dependencies reviewed.'}


def partial(args):
    if args.get('submission')=='ready':return False
    return args.get('submission')=='partial' or bool(args.get('remaining')) or bool(re.search(r'\b(partial submission|still open|unfinished|incomplete submission)\b',args.get('summary',''),re.I))

class EvidenceError(ValueError):
    def __init__(self,problems):
        self.details={'code':'invalid_evidence','invalid_receipts':problems,'instruction':'Fetch team_evidence_catalog(task_id). Cite eligible own snapshot-verification or exact-version read receipts, not messages, writes, publication or model events.'}
        super().__init__('; '.join(str(p['evidence_id'])+': '+p['reason'] for p in problems))

def inspect_receipt(s,run,actor,t,eid,version,cache=None):
    cache={} if cache is None else cache
    row=s.db.execute('SELECT * FROM events WHERE run=? AND id=?',(run,eid)).fetchone()
    if not row:return None,'Evidence ID does not exist in this run'
    if row['agent']!=actor:return None,"Checks require this participant's actual tool receipts in this run; receipt belongs to "+row['agent']
    if row['kind']!='tool_receipt':return None,'Event is '+row['kind']+', not a tool receipt'
    d=json.loads(row['data']);r=d.get('result',{});op=d.get('tool');a=d.get('args',{})
    if r.get('error') or r.get('timed_out') or r.get('exit_code',0)!=0:return d,'Failed execution cannot support a passing check'
    try:
        if op=='team_verify':
            import snapshot_evidence
            snapshot_evidence.validate(s,run,t,d,version)
        elif op=='run':
            if t.get('snapshot_verification'):return d,'Workspace run is not snapshot verification; execute team_verify on the current publication'
            workspace=s.directory(run)/'agents'/actor/'workspace'
            if 'own' not in cache:cache['own']=manifest(workspace)
            if r.get('workspace_version_after')!=revision(cache['own']):return d,'Execution evidence predates current workspace changes; rerun the relevant check'
            if 'pub' not in cache:cache['pub']=manifest(s.directory(run)/'versions'/version)
            if any(cache['own'].get(n)!=h for n,h in cache['pub'].items()):return d,'Check workspace does not match the published source; sync or inspect the exact version'
        elif op=='read':
            if a.get('scope')!='version' or a.get('version')!=version:return d,'Read evidence must inspect the exact immutable submitted version'
            p=s.directory(run)/'versions'/version/a['path']
            if not p.is_file() or digest(p.read_bytes())!=r.get('sha256'):return d,'Read evidence no longer matches the submitted artifact'
        else:return d,'Use successful run or immutable-version read receipts, not messages or status as acceptance evidence; this receipt is '+str(op)
    except (ValueError,OSError,KeyError) as exc:return d,str(exc)
    return d,None

def catalog(s,run,actor,args):
    t=s.task(run,args['task_id'])
    if not t:raise ValueError('Unknown task')
    version=args.get('version') or t.get('version');ids=args.get('evidence_ids')
    if ids is None:
        rows=s.db.execute("SELECT id,data FROM events WHERE run=? AND agent=? AND kind='tool_receipt' AND ((json_extract(data,'$.tool')='team_verify' AND json_extract(data,'$.args.task_id')=?) OR (json_extract(data,'$.tool')='read' AND json_extract(data,'$.args.scope')='version' AND json_extract(data,'$.args.version')=?) OR (json_extract(data,'$.tool')='run' AND json_extract(data,'$.args.command') IN (SELECT value FROM json_each(?)))) ORDER BY id DESC LIMIT 150",(run,actor,t['id'],version,json.dumps(t.get('required_test_commands',[])))).fetchall()
        ids=[]
        for row in rows:
            d=json.loads(row['data']);op=d.get('tool');a=d.get('args',{})
            if (op=='team_verify' and a.get('task_id')==t['id']) or (op=='read' and a.get('scope')=='version' and a.get('version')==version) or (op=='run' and a.get('command') in t.get('required_test_commands',[])):ids.append(row['id'])
    if len(ids)>150:raise ValueError('Request at most 150 evidence IDs')
    eligible=[];ineligible=[];cache={}
    for eid in ids:
        d,reason=inspect_receipt(s,run,actor,t,eid,version,cache)
        entry={'evidence_id':eid,'tool':(d or {}).get('tool'),'command':(d or {}).get('args',{}).get('command'),'path':(d or {}).get('args',{}).get('path')}
        if reason:ineligible.append({**entry,'reason':reason})
        else:eligible.append(entry)
    return {'task_id':t['id'],'actor':actor,'version':version,'eligible':eligible,'ineligible':ineligible,'instruction':'Eligibility means provenance is usable, not that it proves a criterion. Inspect outputs and cite only relevant evidence. Use team_verify for execution, read(scope=version) for current report inspection.'}


def validate(s,run,actor,t,args,review=False):
    import publication
    publication.verify_outputs(s,run,t,args.get('version') or t.get('version') or '')
    if not t.get('evidence_required'):return
    version=args.get('version')
    if not version or version!=t.get('version'):raise ValueError('Ready submission/approval requires the exact published version')
    checks=args.get('checks',[]);expected=set(t.get('acceptance_checks') or [t['acceptance']])
    if len(checks)!=len(expected) or {c.get('criterion') for c in checks}!=expected:raise CriteriaMismatch(sorted(expected),[c.get('criterion') for c in checks])
    executed=set();cache={};generated=set();problems=[]
    for check in checks:
        if check.get('status')!='pass' or not check.get('summary','').strip() or not check.get('evidence_ids'):raise ValueError('Unfinished/failed criteria cannot enter ready review or approval')
        for eid in check['evidence_ids']:
            d,reason=inspect_receipt(s,run,actor,t,eid,version,cache)
            if reason:problems.append({'evidence_id':eid,'criterion':check['criterion'],'reason':reason});continue
            if d['tool'] in ('run','team_verify'):executed.add(d['args'].get('command',''))
            if d['tool']=='team_verify':generated.update(a['path'] for a in d['result'].get('artifacts',[]))
    if problems:raise EvidenceError(problems)
    if not review and set(t.get('verification_outputs',[]))-generated:raise ValueError('Cite snapshot verification receipts generating every declared machine output')
    if set(t.get('required_test_commands',[]))-executed:raise ValueError('Cite your successful execution of every required test command, including for independent approval')
    # Explicit unresolved test failures are not erased by selecting a different passing receipt.
    tests=t.get('required_test_commands',[])
    if not tests:return
    latest={}
    if t.get('snapshot_verification'):
        import snapshot_evidence
        current=snapshot_evidence.binding(s,run,t,version)
    for row in s.db.execute("SELECT id,data FROM events WHERE run=? AND agent=? AND kind='tool_receipt' AND json_extract(data,'$.tool') IN ('run','team_verify') ORDER BY id DESC",(run,t['owner'])):
        d=json.loads(row['data']);cmd=d.get('args',{}).get('command','')
        if t.get('snapshot_verification'):
            if d.get('tool')!='team_verify' or d.get('args',{}).get('task_id')!=t['id']:continue
            if any(d.get('result',{}).get(k)!=current[k] for k in ('source_hash','policy_hash')):continue
        elif d.get('tool')!='run':continue
        # Scope failure tracking to source named in the task's test requirements if present.
        tests=t.get('required_test_commands',[])
        if cmd not in tests:continue
        latest.setdefault(cmd,d.get('result',{}))
        if set(t.get('required_test_commands',[]))<=set(latest):break
    if any(r.get('error') or r.get('exit_code',0)!=0 for r in latest.values()):raise ValueError('A required test command still has an unresolved failure')
    missing=set(t.get('required_test_commands',[]))-set(latest)
    if missing:raise ValueError('Required test commands have not run')
