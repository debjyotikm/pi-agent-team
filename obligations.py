"""Cancellation preserves review obligations until an independent disposition."""
import time
from configuration import LEAD,AGENTS

def capture(task, reason):
    reviewer=task['reviewer']
    if reviewer in (task['owner'],LEAD):reviewer=next(a for a in AGENTS if a not in (task['owner'],LEAD))
    return {'state':'pending_review','reason':reason,'cancelled_by':LEAD,'created':time.time(),
        'owner':task['owner'],'reviewer':reviewer,
        'criteria':task.get('acceptance_checks') or [task['acceptance']],
        'required_outputs':task.get('required_outputs',[]),'version':task.get('version')}

def review(s,run,actor,args):
    t=s.task(run,args['task_id']);o=(t or {}).get('retirement_obligation')
    if not o or t['state']!='cancelled':raise ValueError('No cancelled obligation to review')
    if actor!=o['reviewer'] or actor in (o['owner'],o['cancelled_by']):raise PermissionError('The independent assigned reviewer must adjudicate cancellation; lead/owner cannot self-clear it')
    if not args.get('summary','').strip() or not args.get('evidence_ids'):raise ValueError('Explain coverage or why scope is unnecessary with your own inspection evidence')
    for eid in args['evidence_ids']:
        row=s.db.execute("SELECT data FROM events WHERE run=? AND id=? AND agent=? AND kind='tool_receipt'",(run,eid,actor)).fetchone()
        if not row:raise ValueError('Use your own inspection receipts from this run')
        import json
        receipt=json.loads(row[0]);value=receipt.get('result',{})
        if receipt.get('tool') not in ('read','run') or value.get('error') or value.get('timed_out') or value.get('exit_code',0)!=0:raise ValueError('Use successful read or execution receipts')
    disposition=args['disposition'];target=args.get('replacement_task_id')
    if disposition=='superseded':
        replacement=s.task(run,target)
        if not replacement or replacement['id']==t['id'] or replacement['state']=='cancelled':raise ValueError('Replacement must be a distinct retained task')
        import workflow
        # A replacement waiting on the cancelled task cannot carry its obligations.
        if any(i['task_id']==replacement['id'] for i in workflow.dependency_issues(s.tasks(run))):raise ValueError('Replacement task has unreachable dependencies')
    elif disposition=='unnecessary':
        if target:raise ValueError('Unnecessary disposition has no replacement')
    else:raise ValueError('Choose superseded or unnecessary; cancellation never establishes acceptance')
    o.update(state='reviewed',reviewed_by=actor,disposition=disposition,replacement_task_id=target,summary=args['summary'],evidence_ids=args['evidence_ids'])
    s.save_task(run,t);s.event(run,actor,'cancellation_reviewed',{'task_id':t['id'],**o});return o

def unresolved(s,run):
    result=[]
    for t in s.tasks(run):
        o=t.get('retirement_obligation')
        if t['state']!='cancelled' or not o:continue
        target=s.task(run,o.get('replacement_task_id')) if o.get('replacement_task_id') else None
        if o['state']!='reviewed' or (o.get('disposition')=='superseded' and (not target or target['state']!='done')):
            result.append({'kind':'unresolved_cancellation','task_id':t['id'],'owner':t['owner'],'reviewer':o['reviewer'],'replacement_task_id':o.get('replacement_task_id'),
                'reason':'Independent scope disposition required' if o['state']!='reviewed' else 'Transferred obligation not yet independently completed'})
    return result
