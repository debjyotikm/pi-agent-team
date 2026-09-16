"""Bounded repetition recovery and version-preserving review/task delegation."""
import hashlib
import json
import re
import time
from pathlib import Path
from configuration import AGENTS, LEAD


def initialize(s):
    s.db.executescript('''
    CREATE TABLE IF NOT EXISTS repetition_samples(run TEXT,agent TEXT,event_id INTEGER,key TEXT,output TEXT,progress TEXT,created REAL);
    CREATE TABLE IF NOT EXISTS participant_recovery(run TEXT,agent TEXT,attempts INTEGER,state TEXT,checkpoint TEXT,created REAL,PRIMARY KEY(run,agent));
    CREATE TABLE IF NOT EXISTS job_polls(run TEXT,agent TEXT,job_id TEXT,command TEXT,expires REAL,PRIMARY KEY(run,agent,job_id));
    ''')


def strict(s):
    return (s.root/'strict-workflow.json').exists()


def normalize_command(command):
    # Strip a trailing display-only date command; preserve shell quoting and other code.
    return re.sub(r'\s*(?:&&|;)\s*date(?:\s+-[IuR])?\s*$', '', command).strip()


def meaningful(value):
    if isinstance(value,dict):
        return {k:meaningful(v) for k,v in value.items() if k not in ('created','updated','evidence_id','injected_at','presented_at','age_seconds','idle_seconds')}
    if isinstance(value,list):return [meaningful(v) for v in value]
    if isinstance(value,str):
        return '\n'.join(x for x in value.splitlines() if not re.fullmatch(r'(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+.*\d{4}|\d{4}-\d\d-\d\d[T ].*',x.strip()))
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def unsuccessful_search(args,result):
    """Conservative heuristic for literal filename searches with no matching output.

    It signals repeated unsuccessful searching, never proof that an artifact was lost.
    Content searches, wildcards, failed permissions and commands with mutations are excluded.
    """
    command=args.get('command','')
    if not re.search(r'\bfind\s',command) or result.get('stderr') or result.get('error') or result.get('timed_out'):return None
    if result.get('exit_code',0) not in (0,1):return None
    if re.search(r'\b(?:rm|mv|cp|touch|mkdir|tee|python\d*|sed|perl)\b|(?<![2>])>(?![/>&])',command):return None
    names=re.findall(r'-i?name\s+[\"\']?([A-Za-z0-9_-]+\.(?:py|json|yaml|yml|md|csv|pt|npz))(?=[\"\'\s;)]|$)',command)
    absent=sorted({name for name in names if name not in result.get('stdout','')})
    return absent[0] if absent else None


def state(s,run,agent):
    row=s.db.execute('SELECT * FROM participant_recovery WHERE run=? AND agent=?',(run,agent)).fetchone()
    return dict(row) if row else None


def stalled(s,run,agent):
    return (state(s,run,agent) or {}).get('state')=='stalled'


def reset(s,run,agent):
    with s.lock,s.db:
        s.db.execute('DELETE FROM participant_recovery WHERE run=? AND agent=?',(run,agent))
        s.db.execute('DELETE FROM repetition_samples WHERE run=? AND agent=?',(run,agent))
        s.event(run,'controller','recovery_reset',{'agent':agent,'reason':'Explicit operator retry'})


def observe(s,run,agent,op,args,result,event_id):
    """Observe completed calls only: never abort a still-executing command."""
    if (op=='write' and result.get('changed')) or (op=='run' and result.get('workspace_version_before')!=result.get('workspace_version_after')):
        with s.lock,s.db:s.db.execute('DELETE FROM repetition_samples WHERE run=? AND agent=?',(run,agent))
        return None
    if op not in ('run','read','team_status','team_inbox','team_reference','team_evidence') and not result.get('error'):return None
    if op=='run' and args.get('poll_job_id'):
        row=s.db.execute('SELECT * FROM job_polls WHERE run=? AND agent=? AND job_id=?',(run,agent,args['poll_job_id'])).fetchone()
        if row and row['expires']>time.time() and row['command']==args['command']:return None
    a=dict(args)
    if 'command' in a:a['command']=normalize_command(a['command'])
    key=fingerprint({'op':op,'args':a});out=fingerprint(meaningful(result))
    target=unsuccessful_search(args,result) if op=='run' else None
    if target:
        key=fingerprint({'artifact_search':target});out=fingerprint('No matching filename in returned output')
        a={'artifact_search':target,'latest_command':args['command']}
    progress=fingerprint([{k:t.get(k) for k in ('id','state','version','assignment_id','review_id')} for t in s.tasks(run)])
    with s.lock,s.db:
        rec=state(s,run,agent)
        if rec and rec['state']=='stalled':return {'yield':True,'recovery':'stalled'}
        s.db.execute('INSERT INTO repetition_samples VALUES(?,?,?,?,?,?,?)',(run,agent,event_id,key,out,progress,time.time()))
        rows=s.db.execute('SELECT * FROM repetition_samples WHERE run=? AND agent=? ORDER BY event_id DESC LIMIT 12',(run,agent)).fetchall()
        if rows:s.db.execute('DELETE FROM repetition_samples WHERE run=? AND agent=? AND event_id<?',(run,agent,rows[-1]['event_id']))
        matches=[r for r in rows if r['key']==key and r['output']==out and r['progress']==progress]
        if len(matches)<4:return None
        attempt=(rec['attempts'] if rec else 0)+1
        next_state='pending' if attempt==1 else 'stalled'
        checkpoint={'agent':agent,'attempt':attempt,'state':next_state,'tool':op,'args':a,'repeated_evidence_ids':[r['event_id'] for r in matches],
            'confirmed_result':json.dumps(meaningful(result))[:2500],
            'tasks':[{k:t.get(k) for k in ('id','owner','reviewer','state','version','handoff_error')} for t in s.tasks(run) if t['state'] not in ('done','cancelled')],
            'instruction':'The repeated operation returned unchanged results or repeated unsuccessful searches for the same artifact. Stop searching without new source evidence. Absence from search output does not establish deletion or loss. Record the scoped uncertainty and concrete blocker, make a pending decision, or perform different substantive work. Use team_report(submission="partial") for unfinished work. Review claims can use team_claim_review when the original reviewer is stalled. No task requirements are waived.'}
        s.db.execute('INSERT OR REPLACE INTO participant_recovery VALUES(?,?,?,?,?,?)',(run,agent,attempt,next_state,json.dumps(checkpoint),time.time()))
        s.db.execute('DELETE FROM repetition_samples WHERE run=? AND agent=?',(run,agent))
        path=s.directory(run)/'agents'/agent/f'loop-recovery-{attempt}.json';path.write_text(json.dumps(checkpoint,indent=2)+'\n')
        s.event(run,agent,'loop_recovery',checkpoint)
        if next_state=='pending':s.message(run,'controller',agent,{'kind':'loop_recovery',**checkpoint},True)
        else:
            for peer in AGENTS:
                if peer!=agent:s.message(run,'controller',peer,{'kind':'participant_stalled','agent':agent,'instruction':'Automatic recovery was exhausted. Continue your work; eligible peers may claim its reviews or existing tasks with team_claim_review/team_claim_task. Do not invent a new scope or approve your own work.'},True)
        return {'yield':True,'recovery':next_state,'checkpoint':str(path)}


def register_poll(s,run,actor,args):
    # Scope and expiry must refer to real current local process identity, not a prose job name.
    import os
    pid=int(args['pid']);seconds=int(args['expires_seconds'])
    if seconds<1 or seconds>3600:raise ValueError('Polling lease must be between 1 and 3600 seconds')
    proc=Path('/proc')/str(pid)
    if not proc.exists() or proc.stat().st_uid!=os.getuid():raise ValueError('A currently running process owned by this account is required')
    start=(proc/'stat').read_text().rsplit(')',1)[1].split()[19]
    job=f'{pid}:{start}'
    with s.db:s.db.execute('INSERT OR REPLACE INTO job_polls VALUES(?,?,?,?,?)',(run,actor,job,args['command'],time.time()+seconds))
    s.event(run,actor,'poll_registered',{'job_id':job,'command':args['command'],'expires_seconds':seconds})
    return {'job_id':job,'instruction':'Only this exact command is exempt while this process identity is alive and the lease is valid.'}


def poll_valid(s,run,agent,args):
    if not args.get('poll_job_id'):return
    row=s.db.execute('SELECT * FROM job_polls WHERE run=? AND agent=? AND job_id=?',(run,agent,args['poll_job_id'])).fetchone()
    try:
        pid,start=args['poll_job_id'].split(':');actual=(Path('/proc')/pid/'stat').read_text().rsplit(')',1)[1].split()[19]
    except (ValueError,OSError):raise ValueError('Polling process is no longer alive')
    if not row or row['expires']<=time.time() or row['command']!=args['command'] or actual!=start:raise ValueError('Polling lease is expired or does not match this command/process')


def review_eligible(s,run,t,now=None):
    if t['state']!='review':return False
    if stalled(s,run,t['reviewer']):return True
    now=time.time() if now is None else now
    # Legacy tasks may have no transition timestamp; use their actual reminder history.
    alerts=s.db.execute("SELECT data,created FROM events WHERE run=? AND kind='workflow_alert'",(run,)).fetchall()
    matches=[]
    for row in alerts:
        issue=json.loads(row['data']).get('issue',{})
        if issue.get('task_id')==t['id'] and issue.get('reviewer')==t['reviewer'] and issue.get('version')==t.get('version') and issue.get('kind')=='blocking_review':matches.append(row)
    since=t.get('state_changed_at') or min((r['created'] for r in matches),default=now)
    return len(matches)>=2 and now-since>=120


def claim_review(s,run,actor,args):
    from store import uid
    t=s.task(run,args['task_id'])
    if not t or actor not in AGENTS or actor==t['owner'] or stalled(s,run,actor):raise PermissionError('A non-stalled independent participant must review')
    if args['version']!=t['version']:raise ValueError('Claim must name the exact submitted version')
    if not review_eligible(s,run,t):raise ValueError('Reviewer is neither stalled nor overdue after reminders')
    old=t['reviewer'];t['reviewer']=actor;t['review_id']=uid();t['dispatch_id']=uid()
    t.setdefault('reviewer_history',[]).append({'previous':old,'new':actor,'version':t['version'],'reason':args['reason']})
    s.save_task(run,t);s.message(run,'controller',actor,{'kind':'review','task':t},True)
    s.message(run,'controller',old,{'kind':'information','text':f'Review of {t["id"]} at {t["version"]} reassigned to {actor}.'},False)
    s.event(run,'controller','review_delegated',{'task_id':t['id'],'old':old,'new':actor,'version':t['version']})
    return t


def claim_task(s,run,actor,args):
    from store import uid
    import workflow
    t=s.task(run,args['task_id'])
    if not t or actor not in AGENTS or stalled(s,run,actor):raise PermissionError('Valid available participant required')
    if not stalled(s,run,t['owner']) or t['state'] not in ('assigned','paused','queued'):raise ValueError('Only existing executable work of a stalled owner can be claimed; blocked work requires resolution')
    if s.db.execute("SELECT 1 FROM calls WHERE run=? AND agent=? AND state='executing'",(run,t['owner'])).fetchone():raise ValueError('Owner still has an executing tool')
    if any(s.task(run,d)['state']!='done' for d in t['dependencies']):raise ValueError('Dependencies must be completed')
    workflow.validate_references(t.get('references',[]))
    old=t['owner'];t['owner']=actor
    if t['reviewer']==actor or stalled(s,run,t['reviewer']):
        peers=[a for a in AGENTS if a!=actor and not stalled(s,run,a)]
        if not peers:raise ValueError('No independent reviewer available')
        t['reviewer']=peers[0]
    t['state']='assigned';t['assignment_id']=uid();t['dispatch_id']=uid();t['result']=None
    t['previous_owner_workspace']=str(s.directory(run)/'agents'/old/'workspace')
    t.setdefault('ownership_history',[]).append({'old':old,'new':actor,'reason':args['reason']})
    s.save_task(run,t);s.message(run,'controller',actor,{'kind':'assignment','task':t,'instruction':'Original scope and acceptance remain unchanged. Inspect preserved previous-owner work; do not overwrite it.'},True)
    s.event(run,'controller','task_claimed',{'task_id':t['id'],'old':old,'new':actor})
    return t


def delegate_reviews(s,run,activity):
    with s.lock:
        for t in s.tasks(run):
            if not review_eligible(s,run,t):continue
            candidates=[a for a in AGENTS if a not in (t['owner'],t['reviewer']) and not stalled(s,run,a) and activity.get((run,a),{}).get('state')=='waiting']
            if candidates:claim_review(s,run,candidates[0],{'task_id':t['id'],'version':t['version'],'reason':'Idle independent reviewer takes an overdue/stalled review; acceptance unchanged.'})
