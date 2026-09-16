"""Controller-issued review inputs and deduplicated missing-artifact blockers."""
import json
import re
from pathlib import Path
from configuration import AGENTS,LEAD
import publication
from workflow import file_hash


def initialize(s):
    s.db.execute('''CREATE TABLE IF NOT EXISTS handoff_blockers(run TEXT,task_id TEXT,actor TEXT,generation TEXT,event_id INTEGER,body TEXT,PRIMARY KEY(run,task_id,actor,generation))''')


def receipt(s,run,task,ident):
    if not ident or not re.fullmatch('[a-f0-9]{16}',ident):raise ValueError('Use the transaction_id from a verified team_publish receipt as publication_id')
    row=s.db.execute("SELECT data FROM events WHERE run=? AND kind='publication_committed' AND json_extract(data,'$.transaction_id')=?",(run,ident)).fetchone()
    if not row:raise ValueError('Unknown controller-issued publication receipt')
    record=json.loads(row['data']);path=s.directory(run)/'publications'/(ident+'.json')
    if not path.is_file() or json.loads(path.read_text())!=record or record.get('schema')!=2 or not record.get('verified'):raise ValueError('Publication receipt is missing or inconsistent')
    if record['task_id']!=task['id'] or record['version']!=task['version']:raise ValueError('Publication receipt does not belong to this task and current submitted version')
    publication.verify_tree(s.directory(run)/'versions'/record['version'],record)
    return record


def prepare(s,run,task,args):
    from store import uid
    for dep in task.get('ready_dependencies',[]):
        t=s.task(run,dep)
        if not t or t['state']!='done':raise ValueError('Required reviewed decision is still open: '+dep)
    record=receipt(s,run,task,args.get('publication_id'))
    artifacts=publication.verify_outputs(s,run,task,record['version'])
    if not artifacts:artifacts=[{**a,'version':record['version']} for a in record['artifacts']]
    root=s.directory(run)/'versions'/record['version']
    return {'handoff_id':uid(),'task_id':task['id'],'publication_id':record['transaction_id'],'version':record['version'],'snapshot_root':str(root),
        'mode':'formal_review','artifacts':[{**a,'absolute_path':str(root/a['path'])} for a in artifacts],
        'checks':args.get('checks',[]),'test_receipt_ids':sorted({eid for c in args.get('checks',[]) for eid in c.get('evidence_ids',[])}),
        'instruction':'Use these exact paths and receipts. Independently verify acceptance. Missing inputs must use team_review_input to record a blocker; do not guess alternate paths or downloads.'}


def validate(s,run,task,ident=None):
    for dep in task.get('ready_dependencies',[]):
        prerequisite=s.task(run,dep)
        if not prerequisite or prerequisite['state']!='done':raise ValueError('Required reviewed decision is still open: '+dep)
    packet=task.get('review_handoff')
    if not packet or (ident is not None and packet['handoff_id']!=ident):raise ValueError('Use the current controller-issued handoff_id from team_review_input')
    receipt(s,run,task,packet['publication_id'])
    for item in packet['artifacts']:
        p=s.directory(run)/'versions'/packet['version']/item['path']
        if not p.is_file() or p.stat().st_size!=item['size_bytes'] or file_hash(p)!=item['sha256']:raise ValueError('Review input missing or changed: '+item['path'])
    publication.verify_outputs(s,run,task,packet['version'])
    return packet


def unavailable(s,run,actor,task,reason):
    import hashlib
    identity=json.dumps({k:task.get(k) for k in ('id','assignment_id','version','review_id')},sort_keys=True)
    generation=hashlib.sha256(identity.encode()).hexdigest()
    row=s.db.execute('SELECT body FROM handoff_blockers WHERE run=? AND task_id=? AND actor=? AND generation=?',(run,task['id'],actor,generation)).fetchone()
    if row:return json.loads(row[0])
    body={'status':'unavailable','task_id':task['id'],'reason':reason,'instruction':'One blocker has been sent to the owner. Continue your other assignment or team_wait; do not search alternate paths or download claimed local artifacts.'}
    if task['state']=='review':
        from store import uid
        task.setdefault('review_history',[]).append({'actor':'controller','approved':False,'version':task['version'],'summary':reason})
        task['state']='assigned';task['dispatch_id']=uid();task['review_handoff']=None;s.save_task(run,task)
    eid=s.event(run,actor,'missing_review_input',body);body['blocker_id']=eid
    s.db.execute('INSERT INTO handoff_blockers VALUES(?,?,?,?,?,?)',(run,task['id'],actor,generation,eid,json.dumps(body)))
    for recipient in set((task['owner'],LEAD)):
        s.message(run,'controller',recipient,{'kind':'blocker','task_id':task['id'],'text':reason,'instruction':'Publish actual required files and issue a validated ready report. Preserve draft state; no review can proceed on the claimed absent inputs.','blocker_id':eid},True)
    s.db.commit();return body


def review_input(s,run,actor,args):
    task=s.task(run,args['task_id'])
    if not task:raise ValueError('Unknown task')
    if actor not in (task['reviewer'],task['owner'],LEAD):raise PermissionError('Only task participants or lead fetch formal review inputs')
    try:
        if task['state']!='review':raise ValueError('Task has no ready formal review; owner must finish/publish and submit the verified receipt')
        return validate(s,run,task)
    except ValueError as exc:return unavailable(s,run,actor,task,str(exc))


def draft_input(s,run,actor,args):
    from store import uid,safe,atomic_json
    task=s.task(run,args['task_id'])
    if not task or actor not in AGENTS:raise ValueError('Known task and participant required')
    names=task.get('required_outputs',[])
    if not names:raise ValueError('Draft owner must declare required_outputs before sharing draft inputs')
    source=s.directory(run)/'agents'/task['owner']/'workspace';root=s.directory(run)/'drafts'/uid();artifacts=[];missing=[]
    for name in publication.normalized_paths(source,names):
        p=safe(source,name)
        if not p.is_file():missing.append(name);continue
        data=p.read_bytes();dst=safe(root,name);dst.parent.mkdir(parents=True,exist_ok=True);dst.write_bytes(data)
        from store import digest
        artifacts.append({'path':name,'absolute_path':str(dst),'sha256':digest(data),'size_bytes':len(data)})
    result={'mode':'draft_feedback','formal_review':False,'task_id':task['id'],'draft_id':root.name,'artifacts':artifacts,'missing':missing,
        'instruction':'Inspect only these captured draft bytes. Send labelled draft feedback to the owner. This is not a publication, ready submission, qualification or approval.'}
    root.mkdir(parents=True,exist_ok=True);atomic_json(root/'DRAFT.json',result);s.event(run,actor,'draft_inputs_captured',result);return result


def validate_message(s,run,args):
    text=args['text']
    # Messages remain conversation, not a second route to formal review dispatch.
    if args.get('kind') in ('question','request') and re.search(r'\breview\b',text,re.I) and re.search(r'\b(published|version|review request)\b',text,re.I):
        raise ValueError('Formal review requests must use team_report(submission="ready", publication_id=...) so the controller verifies and dispatches the handoff. Use labelled draft feedback for unfinished work.')
    for version in re.findall(r'\b(?:version|snapshot)\s*[`:\s]*([a-f0-9]{24})\b',text,re.I):
        if not (s.directory(run)/'versions'/version).is_dir():raise ValueError('Message cites a nonexistent snapshot '+version+'; use team_review_input or team_draft_input, not guessed versions')
