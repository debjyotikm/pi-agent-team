"""Verified file publication: explicit deletes, atomic validation, durable receipts."""
import json
import re
import shutil
from pathlib import Path
from workflow import file_hash


def normalized_paths(root,names):
    from store import safe
    result=[]
    for name in names:
        safe(root,name)
        p=Path(name)
        if p.name=='.env' or p.name.startswith('.env.'):raise ValueError('Environment credential files are excluded from publication')
        # Reject symlink components, including links to files inside the root.
        current=Path(root)
        for part in p.parts:
            current=current/part
            if current.is_symlink():raise ValueError('Publication paths must not use symlinks')
        result.append(str(p))
    if len(set(result))!=len(result):raise ValueError('Provide distinct file paths')
    return result


def verify_tree(root,journal):
    from store import manifest,revision,safe
    root=Path(root)
    selected=manifest(root) if root.is_dir() else {}
    if not root.is_dir() or revision(selected)!=journal['version']:raise ValueError('Publication snapshot version mismatch; transaction retained')
    for entry in journal['artifacts']:
        normalized_paths(root,[entry['path']]);p=safe(root,entry['path'])
        if selected.get(entry['path'])!=entry['sha256'] or not p.is_file() or p.stat().st_size!=entry['size_bytes'] or file_hash(p)!=entry['sha256']:
            raise ValueError('Published file missing or hash/size mismatch: '+entry['path'])
    for entry in journal['deleted']:
        p=safe(root,entry['path'])
        if p.exists() or p.is_symlink():raise ValueError('Explicit deletion not reflected in snapshot: '+entry['path'])


def publish(s,run,agent,args):
    from store import manifest,revision,copy_source,safe,uid,atomic_json
    s.repair_publication(run)
    task=s.task(run,args['task_id'])
    if not task or task['owner']!=agent or task['state']!='assigned':raise PermissionError('Publication requires your active lead-assigned task')
    d=s.directory(run);a=d/'agents'/agent;workspace=a/'workspace'
    base=json.loads((a/'base.json').read_text());current=manifest(d/'project')
    names=normalized_paths(workspace,args.get('files',[]));deletes=args.get('deletions',[])
    removed=normalized_paths(d/'project',[x['path'] for x in deletes])
    all_names=names+removed
    if not all_names or len(set(all_names))!=len(all_names):raise ValueError('Publish/delete lists must be nonempty, distinct and disjoint')
    artifacts=[];deleted=[]
    for name in all_names:
        safe(d/'project',name)
        if current.get(name)!=base.get(name):raise ValueError('Publication conflict for '+name+'; sync and resolve it first')
    for name in names:
        p=safe(workspace,name)
        if not p.is_file():raise ValueError('Required publication file does not exist as a regular file: '+name+'; missing files are never deletions')
        sha=file_hash(p)
        artifacts.append({'path':name,'sha256':sha,'size_bytes':p.stat().st_size,'change':'added' if name not in current else 'unchanged' if current[name]==sha else 'modified','previous_sha256':current.get(name)})
    for request,name in zip(deletes,removed):
        sha=request.get('sha256','')
        if not re.fullmatch('[0-9a-f]{64}',sha) or current.get(name)!=sha:raise ValueError('Deletion requires an existing file and its exact current full SHA-256: '+name)
        deleted.append({'path':name,'sha256':sha,'size_bytes':(d/'project'/name).stat().st_size,'change':'deleted'})
    # No mutation of project/base/task until every input has been validated.
    staged=d/('publish-'+uid());copy_source(d/'project',staged,keep_git=True)
    for entry in artifacts:
        dst=safe(staged,entry['path']);dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(safe(workspace,entry['path']),dst)
    for entry in deleted:safe(staged,entry['path']).unlink()
    new=manifest(staged);expected=dict(current)
    for entry in artifacts:expected[entry['path']]=entry['sha256']
    for entry in deleted:expected.pop(entry['path'],None)
    if new!=expected:
        atomic_json(staged.with_suffix('.rejected.json'),{'error':'Staged manifest differs from explicit file changes','expected':expected,'actual':new})
        raise ValueError('Staged file hash/size or selection mismatch; unrequested changes/deletions are forbidden')
    after=dict(base)
    for entry in artifacts:after[entry['path']]=entry['sha256']
    for entry in deleted:after.pop(entry['path'],None)
    journal={'schema':2,'transaction_id':uid(),'staged':staged.name,'agent':agent,'task_id':task['id'],'base_after':after,'version':revision(new),'files':names,'artifacts':artifacts,'deleted':deleted}
    try:verify_tree(staged,journal)
    except Exception as exc:
        # Preserve staging evidence without creating a replayable success journal.
        atomic_json(staged.with_suffix('.rejected.json'),{'error':str(exc),'journal':journal});raise
    atomic_json(d/'publication.json',journal)
    return s.repair_publication(run)


def repair(s,run):
    from store import manifest,revision,atomic_json
    d=s.directory(run);path=d/'publication.json'
    if not path.exists():return
    j=json.loads(path.read_text())
    if j.get('schema')!=2:
        raise ValueError('Legacy publication journal needs explicit audit; missing paths must not be inferred as deletes')
    staged=d/j['staged'];old=d/'project.old';project=d/'project'
    if staged.exists():
        verify_tree(staged,j)
        if project.exists():
            if old.exists():raise RuntimeError('Ambiguous interrupted publication; preserve and inspect its trees')
            project.rename(old)
        staged.rename(project)
    verify_tree(project,j)
    version=s.seal(run);verify_tree(d/'versions'/version,j)
    receipt={'schema':2,'verified':True,'transaction_id':j['transaction_id'],'task_id':j['task_id'],'version':version,'files':j['files'],'artifacts':j['artifacts'],'deleted':j['deleted'],
        'changes':{kind:[x['path'] for x in j['artifacts']+j['deleted'] if x['change']==kind] for kind in ('added','modified','unchanged','deleted')}}
    receipts=d/'publications';receipts.mkdir(exist_ok=True);atomic_json(receipts/(j['transaction_id']+'.json'),receipt)
    atomic_json(d/'agents'/j['agent']/'base.json',j['base_after'])
    task=s.task(run,j['task_id'])
    if not task:raise RuntimeError('Publication task missing')
    task['version']=version
    records=task.setdefault('publication_receipts',[])
    if not any(r['transaction_id']==j['transaction_id'] for r in records):records.append(receipt)
    s.save_task(run,task)
    prior=s.db.execute("SELECT id FROM events WHERE run=? AND kind='publication_committed' AND json_extract(data,'$.transaction_id')=?",(run,j['transaction_id'])).fetchone()
    if not prior:s.event(run,j['agent'],'publication_committed',receipt)
    if old.exists():shutil.rmtree(old)
    path.unlink()
    return receipt


def verify_outputs(s,run,task,version):
    from store import safe
    required=task.get('required_outputs',[])
    if not required:return []
    root=s.directory(run)/'versions'/version;bindings=[]
    for name in normalized_paths(root,required):
        p=safe(root,name)
        if not p.is_file():raise ValueError('Required deliverable absent from submitted snapshot: '+name)
        bindings.append({'path':name,'sha256':file_hash(p),'size_bytes':p.stat().st_size,'version':version})
    # Recheck approved/review-ready bindings; snapshot directories must not mutate.
    previous=task.get('submitted_outputs',[])
    if previous and task.get('submitted_outputs_version')==version and previous!=bindings:raise ValueError('Submitted output hashes changed after readiness')
    return bindings
