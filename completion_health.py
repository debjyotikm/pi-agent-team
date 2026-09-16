"""Detect unresolved delivery work without treating publication as approval."""
import json

def issues(s, run, activity, now):
    if not (s.root/'strict-workflow.json').exists():return []
    result=[]
    for task in s.tasks(run):
        if task['state']=='blocked' and now-task.get('state_changed_at',now)>=180:
            from configuration import LEAD
            result.append({'kind':'blocked_decision','task_id':task['id'],'owner':task['owner'],'recipient':LEAD,'assignment_id':task.get('assignment_id'),'blocked_since':task.get('state_changed_at')})
        if task['state']!='assigned':continue
        owner=task['owner'];base={'task_id':task['id'],'owner':owner,
            'assignment_id':task.get('assignment_id'),'version':task.get('version')}
        worker=activity.get((run,owner),{})
        executing=s.db.execute("SELECT 1 FROM calls WHERE run=? AND agent=? AND state='executing'",(run,owner)).fetchone()
        if worker.get('state')=='waiting' and now-worker.get('updated',now)>=120 and not executing:
            result.append({'kind':'idle_assignment',**base,'idle_seconds':round(now-worker['updated'])})
        publication=s.db.execute("SELECT id,created,data FROM events WHERE run=? AND kind='publication_committed' AND json_extract(data,'$.task_id')=? ORDER BY id DESC LIMIT 1",(run,task['id'])).fetchone()
        if not publication:continue
        record=json.loads(publication['data'])
        if record.get('version')!=task.get('version'):continue
        root=s.directory(run)/'versions'/record['version']
        missing=[name for name in task.get('required_outputs',[]) if not (root/name).is_file()]
        # Partial publication is allowed; the missing required outputs stay visible.
        if missing and now-publication['created']>=60:
            result.append({'kind':'publication_output_gap',**base,'missing':missing})
        if now-publication['created']<300:continue
        partial=s.db.execute("SELECT data FROM events WHERE run=? AND kind='partial_submission' AND id>? AND json_extract(data,'$.task_id')=? ORDER BY id DESC LIMIT 1",(run,publication['id'],task['id'])).fetchone()
        if partial and json.loads(partial['data']).get('remaining'):continue
        result.append({'kind':'publication_without_resolution',**base})
    return result
