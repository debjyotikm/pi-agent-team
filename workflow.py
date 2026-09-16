"""Deterministic workflow checks; no model inference or automatic task approval."""
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

from configuration import LEAD


def initialize(store):
    store.db.executescript('''
    CREATE TABLE IF NOT EXISTS message_delivery(message_id INTEGER PRIMARY KEY, injected_at REAL, presented_at REAL);
    CREATE TABLE IF NOT EXISTS message_actions(message_id INTEGER PRIMARY KEY, run TEXT, recipient TEXT,
        task_id TEXT, expected TEXT, state TEXT DEFAULT 'open', resolution TEXT, created REAL, resolved_at REAL);
    CREATE TABLE IF NOT EXISTS workflow_alerts(run TEXT, fingerprint TEXT, attempts INTEGER,
        last_sent REAL, PRIMARY KEY(run,fingerprint));
    ''')


def transition(task):
    if not task: return None
    return {k:task.get(k) for k in ('id','state','assignment_id','dispatch_id','review_id','version')}


def track_message(s, ident, run, sender, recipient, body, created):
    kind=body.get('kind')
    if kind not in ('question','request','blocker','review','blocked','changes_requested'):return
    if sender=='user' and kind=='request':return  # Original run objective is tracked by runs.
    task_id=body.get('task_id') or body.get('task',{}).get('id')
    task=body.get('task') or (s.task(run,task_id) if task_id else None)
    s.db.execute('INSERT OR IGNORE INTO message_actions(message_id,run,recipient,task_id,expected,created) VALUES(?,?,?,?,?,?)',
        (ident,run,recipient,task_id,json.dumps({'task':transition(task),'kind':kind}),created))


def actions(s, run):
    rows=s.db.execute("SELECT * FROM message_actions WHERE run=? AND state='open' ORDER BY message_id",(run,)).fetchall()
    for row in rows:
        record=json.loads(row['expected']);expected=record.get('task') if isinstance(record,dict) and 'kind' in record else record
        kind=record.get('kind') if isinstance(record,dict) else None
        task=s.task(run,row['task_id']) if row['task_id'] else None
        actual=transition(task)
        changed=kind in ('review','blocked','changes_requested') and expected is not None and (actual is None or any(actual.get(k)!=expected.get(k) for k in ('state','assignment_id','dispatch_id','review_id')))
        if changed:
            resolution={'kind':'task_transition','before':expected,'after':transition(task)}
            s.db.execute("UPDATE message_actions SET state='resolved',resolution=?,resolved_at=? WHERE message_id=?",
                (json.dumps(resolution),time.time(),row['message_id']))
            s.event(run,'controller','message_action_resolved',{'message_id':row['message_id'],**resolution})
    return [dict(r) for r in s.db.execute('''SELECT a.*,m.sender,m.state AS delivery_state,
        d.injected_at,d.presented_at FROM message_actions a JOIN messages m ON m.id=a.message_id
        LEFT JOIN message_delivery d ON d.message_id=a.message_id WHERE a.run=? ORDER BY a.message_id''',(run,))]


def resolve(s, run, actor, args):
    row=s.db.execute('SELECT * FROM message_actions WHERE run=? AND message_id=?',(run,args['message_id'])).fetchone()
    if not row:raise ValueError('Unknown actionable message')
    if actor not in (row['recipient'],LEAD):raise PermissionError('Only the recipient or lead records its resolution')
    summary=args['summary'].strip()
    if not summary:raise ValueError('Record the concrete answer or decision, not an acknowledgement')
    evidence=args.get('evidence_ids',[])
    for eid in evidence:
        if not s.db.execute('SELECT 1 FROM events WHERE run=? AND id=?',(run,eid)).fetchone():raise ValueError('Evidence must belong to this run')
    resolution={'kind':'explicit_decision','actor':actor,'summary':summary,'evidence_ids':evidence}
    if row['state']=='resolved':return dict(row)
    with s.db:
        s.db.execute("UPDATE message_actions SET state='resolved',resolution=?,resolved_at=? WHERE message_id=?",
            (json.dumps(resolution),time.time(),row['message_id']))
        original=s.db.execute('SELECT sender FROM messages WHERE id=?',(row['message_id'],)).fetchone()[0]
        from configuration import AGENTS
        if original in AGENTS:s.message(run,actor,original,{'kind':'information','resolves_message':row['message_id'],**resolution},False)
        s.event(run,actor,'message_action_resolved',{'message_id':row['message_id'],**resolution})
    return resolution


def dependency_issues(tasks):
    by_id={t['id']:t for t in tasks};issues=[]
    def walk(ident, trail):
        if ident in trail:return [{'reason':'cycle','path':[*trail,ident]}]
        t=by_id.get(ident)
        if not t:return [{'reason':'missing','path':[*trail,ident]}]
        if t['state']=='cancelled':return [{'reason':'cancelled','path':[*trail,ident]}]
        if t['state']=='done':return []
        return [v for dep in [*t['dependencies'],*t.get('ready_dependencies',[])] for v in walk(dep,[*trail,ident])]
    for t in tasks:
        if t['state'] in ('done','cancelled'):continue
        problems=[v for dep in [*t['dependencies'],*t.get('ready_dependencies',[])] for v in walk(dep,[t['id']])]
        if problems:issues.append({'kind':'unreachable_dependency','task_id':t['id'],'owner':t['owner'],'problems':problems})
    return issues


def file_hash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def source_identity(root):
    root=Path(root).expanduser().resolve()
    if not root.is_dir():raise ValueError('Source root must exist')
    p=subprocess.run(['git','-C',str(root),'rev-parse','--show-toplevel'],capture_output=True,text=True)
    if p.returncode==0:
        gitroot=Path(p.stdout.strip()).resolve()
        commit=subprocess.check_output(['git','-C',str(gitroot),'rev-parse','HEAD'],text=True).strip()
        return str(gitroot),'git:'+commit
    from store import manifest,revision
    return str(root),'snapshot:'+revision(manifest(root))


def describe_references(s, run, actor, paths, source_root=None):
    workspace=s.directory(run)/'agents'/actor/'workspace'
    root=Path(source_root).expanduser().resolve() if source_root else workspace
    root,version=source_identity(root);root=Path(root)
    result=[]
    for name in paths:
        p=Path(name).expanduser();p=(p if p.is_absolute() else root/p).resolve()
        if not p.is_relative_to(root) or not p.is_file():raise ValueError('Reference must be an existing file inside its source root: '+str(p))
        result.append({'path':str(p),'sha256':file_hash(p),'source_root':str(root),'source_version':version})
    return result


def validate_references(refs):
    identities={}
    for ref in refs:
        if not re.fullmatch('[0-9a-f]{64}',ref.get('sha256','')):raise ValueError('Reference requires a full SHA-256')
        root=Path(ref['source_root']).expanduser().resolve();path=Path(ref['path']).expanduser()
        if not path.is_absolute():raise ValueError('Reference path must be absolute')
        path=path.resolve()
        if not path.is_relative_to(root) or not path.is_file():raise ValueError('Reference file missing or outside source root: '+str(path))
        if file_hash(path)!=ref['sha256']:raise ValueError('Reference hash changed: '+str(path))
        if str(root) not in identities:identities[str(root)]=source_identity(root)[1]
        if identities[str(root)]!=ref.get('source_version'):raise ValueError('Reference source version changed: '+str(root))
    return refs


def validate_handoff(args):
    refs=args.get('references',[]);proposed=args.get('proposed_artifacts',[])
    validate_references(refs)
    # Catch explicit file and commit claims in prose too. Proposed outputs must be labelled.
    text=args['description']+'\n'+args['acceptance']
    tokens=re.findall(r'(?<![\w/])(?:[\w.~/-]+\.(?:py|yaml|yml|json|md|csv|pt|cpp|hpp|sh))(?![\w.])',text)
    covered={str(r['path']) for r in refs}|{Path(r['path']).name for r in refs}
    future=set(proposed)|{Path(p).name for p in proposed}
    for token in tokens:
        if token not in covered and Path(token).name not in covered and token not in future and Path(token).name not in future:
            raise ValueError('Unbound artifact '+token+': use team_reference for an existing input, or list it in proposed_artifacts if it is to be created')
    for commit in re.findall(r'\bcommit\s+[`\'"]?([0-9a-f]{7,64})\b',text,re.I):
        if not any(r['source_version'].startswith('git:'+commit) for r in refs):raise ValueError('Unverified commit claim: '+commit)
    return refs


def priorities(s, run, activity, now):
    import completion_health
    import obligations
    tasks=s.tasks(run);issues=dependency_issues(tasks)
    issues.extend(completion_health.issues(s,run,activity,now))
    issues.extend(obligations.unresolved(s,run))
    for t in tasks:
        if t['state'] in ('done','cancelled'):continue
        if t.get('handoff_error'):issues.append({'kind':'invalid_handoff','task_id':t['id'],'owner':t['owner'],'error':t['handoff_error']})
        if t['state']!='review':continue
        owner=activity.get((run,t['owner']),{})
        since=owner.get('updated',now)
        if owner.get('state')=='waiting' and now-since>=30:
            issues.append({'kind':'blocking_review','task_id':t['id'],'owner':t['owner'],'reviewer':t['reviewer'],
                'version':t['version'],'dispatch_id':t.get('dispatch_id'),'idle_seconds':round(now-since)})
    for action in actions(s,run):
        if action['state']=='open' and now-action['created']>=120:
            issues.append({'kind':'unresolved_message','message_id':action['message_id'],'recipient':action['recipient'],
                'task_id':action['task_id'],'age_seconds':round(now-action['created']),'presented':action['presented_at'] is not None})
    for issue in issues:
        row=s.db.execute('SELECT attempts FROM workflow_alerts WHERE run=? AND fingerprint=?',(run,alert_key(issue))).fetchone()
        issue['reminders_sent']=row[0] if row else 0
        issue['escalation']='lead_decision_required' if issue['reminders_sent']>=2 else 'pending'
    return issues

def alert_key(issue):
    identity={k:v for k,v in issue.items() if k not in ('age_seconds','idle_seconds','presented','reminders_sent','escalation')}
    return hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()


def alert(s,run,issue,now):
    key=alert_key(issue)
    row=s.db.execute('SELECT attempts,last_sent FROM workflow_alerts WHERE run=? AND fingerprint=?',(run,key)).fetchone()
    attempts,last=(row['attempts'],row['last_sent']) if row else (0,0)
    if attempts>=2 or now-last<60:return None
    recipient=(issue.get('reviewer') or issue.get('recipient') or issue.get('owner') or LEAD) if attempts==0 else LEAD
    instructions={'blocking_review':'Review this exact version now, request changes, or have the lead explicitly reassign the review. The owner is idle; do not create correction tasks dependent on approving the defective submission.',
        'blocked_decision':'Resolve this blocked contract or external prerequisite explicitly. Reopen the existing owner task with verified requirements, supply the missing input, or document the actual external action needed. Other active work does not resolve this decision. Do not cancel required scope to hide the blocker.',
        'unresolved_cancellation':'Inspect the preserved cancelled scope. Its independent reviewer must record a justified disposition with team_review_cancellation; transferred obligations require a reachable replacement and remain open until that replacement is independently complete. Cancellation grants no acceptance.',
        'unreachable_dependency':'Repair the listed dependency chain; do not mark its cancelled prerequisite done or bypass its acceptance.',
        'invalid_handoff':'Verify current inputs with team_reference and reassign with exact references. Do not score against missing or changed bindings.',
        'unresolved_message':'Read this message and record the concrete decision with team_resolve_message, or perform its required task transition. Receipt alone is not resolution.',
        'idle_assignment':'Your assigned task is unfinished while you wait. Finish its owner action and submit ready with evidence, or report blocked with the concrete dependency and next actor. Waiting for review is valid only after a formal ready handoff. A draft audit may conclude that its input is defective; it need not wait for that input to pass.',
        'publication_output_gap':'The current published snapshot lacks these required outputs. Publish them or ask the lead to reconcile the declared paths with the actual delivered scope, preserving requirements. Partial publication is allowed but is not completion.',
        'publication_without_resolution':'Publication has no subsequent ready submission or documented remaining work. Submit ready with the verified receipt and checks, report partial with concrete remaining owner actions, or report blocked. Do not invent evidence or auto-approve.'}
    sent=s.message(run,'controller',recipient,{'kind':'workflow_alert','issue':issue,'instruction':instructions[issue['kind']],'attempt':attempts+1},True)
    s.db.execute('INSERT OR REPLACE INTO workflow_alerts VALUES(?,?,?,?)',(run,key,attempts+1,now))
    s.event(run,'controller','workflow_alert',{**sent,'recipient':recipient,'issue':issue})
    return sent
