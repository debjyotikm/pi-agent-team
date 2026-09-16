"""Verification of published inputs in a private execution copy, separate from reports."""
import hashlib,json,os,platform,shlex,shutil,sys
from pathlib import Path

def sha(value):return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()

def policy(task):
    from store import safe
    reports=task.get('report_outputs',[]);outputs=task.get('verification_outputs',[])
    for name in reports+outputs:
        safe(Path('/tmp'),name)
        if name not in task.get('required_outputs',[]):raise ValueError('Excluded report/result must be a declared required output: '+name)
    if any(Path(n).suffix.lower()!='.md' for n in reports):raise ValueError('Report outputs must be Markdown, not executable source')
    if any(Path(n).suffix.lower() not in ('.json','.txt','.csv') for n in outputs):raise ValueError('Verification outputs must be declared JSON/text/CSV results')
    if len(set(reports+outputs))!=len(reports+outputs):raise ValueError('Report/result exclusions must be unique')
    return {'report_outputs':sorted(reports),'verification_outputs':sorted(outputs),'references':task.get('references',[])}

def binding(s,run,task,version):
    from store import manifest,revision
    if not isinstance(version,str) or len(version)!=24 or any(c not in '0123456789abcdef' for c in version):raise ValueError('Exact published snapshot version required')
    root=s.directory(run)/'versions'/version
    if not root.is_dir():raise ValueError('Published snapshot is absent')
    full=manifest(root)
    if revision(full)!=version:raise ValueError('Published snapshot content does not match its version')
    p=policy(task);excluded=set(p['report_outputs']+p['verification_outputs']);inputs={k:v for k,v in full.items() if k not in excluded}
    return {'source_hash':sha(inputs),'policy_hash':sha(p),'inputs':inputs,'policy':p}

def execute(c,run,actor,args):
    from store import uid,atomic_json,digest
    import workflow
    s=c.store;t=s.task(run,args['task_id'])
    if not t or not ((actor==t['owner'] and t['state']=='assigned') or (actor==t['reviewer'] and t['state']=='review')):raise ValueError('Verify only your assigned task or submitted independent review')
    if s.project_info(run).get('execution_mode')!='host':raise ValueError('Snapshot verification currently requires host execution mode')
    if args['version']!=t.get('version'):raise ValueError('Verify the current task publication; old evidence may be reused only when source binding matches')
    with s.lock:
        b=binding(s,run,t,args['version']);workflow.validate_references(t.get('references',[]))
        root=s.directory(run)/'verifications'/uid();work=root/'work';work.mkdir(parents=True)
        source=s.directory(run)/'versions'/args['version']
        for name,expected in b['inputs'].items():
            data=(source/name).read_bytes()
            if digest(data)!=expected:raise ValueError('Snapshot changed during capture')
            p=work/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data);p.chmod((source/name).stat().st_mode & 0o777)
        atomic_json(root/'INPUTS.json',b)
    rt=c.runtimes[(run,actor)];timeout=max(1,min(21600,int(args.get('timeout_seconds',600))))
    command=args['command'];tokens=shlex.split(command);exe=shutil.which(tokens[0]) if tokens else None
    environment={'platform':platform.platform(),'controller_python':sys.version,'execution_mode':'host','program':exe,
        'program_sha256':digest(Path(exe).read_bytes()) if exe and Path(exe).is_file() else None,
        'pythonpath':str(work),'python_dont_write_bytecode':True,'external_access':'Native host access; absolute paths and external data are not sandboxed. Declared references are separately checked.'}
    if exe and Path(exe).name.startswith('python'):
        script='import sys,json,importlib.metadata as m; print(json.dumps({"python":sys.version,"executable":sys.executable,"packages":sorted((d.metadata.get("Name",""),d.version) for d in m.distributions())}))'
        env_result=rt.execute(shlex.quote(exe)+' -c '+shlex.quote(script),30,cwd=str(work))
        environment['interpreter_inventory']=env_result
        if env_result.get('exit_code')!=0:raise ValueError('Cannot record verification interpreter environment')
    actual='export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH='+shlex.quote(str(work))+'\n'+command
    result=rt.execute(actual,timeout,cwd=str(work))
    changed=[n for n,h in b['inputs'].items() if not (work/n).is_file() or digest((work/n).read_bytes())!=h]
    artifacts=[]
    for name in b['policy']['verification_outputs']:
        p=work/name
        if p.is_file() and not p.is_symlink():
            data=p.read_bytes();out=root/'outputs'/name;out.parent.mkdir(parents=True,exist_ok=True);out.write_bytes(data)
            artifacts.append({'path':name,'absolute_path':str(out),'sha256':digest(data),'size_bytes':len(data)})
    try:workflow.validate_references(t.get('references',[]))
    except ValueError as exc:result['error']='Declared external input changed: '+str(exc)
    if changed:result['error']='Verification modified captured source inputs: '+', '.join(changed[:10])
    record={'schema':1,'task_id':t['id'],'actor':actor,'version':args['version'],'source_hash':b['source_hash'],'policy_hash':b['policy_hash'],
        'command':command,'cwd':str(work),'environment':environment,'environment_hash':sha(environment),'artifacts':artifacts,'changed_inputs':changed,**result}
    atomic_json(root/'RESULT.json',record)
    return {k:v for k,v in record.items() if k!='environment'} | {'verification_id':root.name,'record_path':str(root/'RESULT.json')}

def validate(s,run,t,receipt,version):
    """Verify the sealed receipt against its execution record and the current submitted inputs."""
    r=receipt['result'];ident=r.get('verification_id','')
    if len(ident)!=16 or any(c not in '0123456789abcdef' for c in ident):raise ValueError('Missing verification identity')
    path=s.directory(run)/'verifications'/ident/'RESULT.json'
    record=json.loads(path.read_text())
    for key in ('task_id','actor','source_hash','policy_hash','command','environment_hash','artifacts','exit_code'):
        if record.get(key)!=r.get(key):raise ValueError('Verification receipt differs from retained execution record')
    if record.get('error') or record.get('timed_out') or record.get('exit_code')!=0:raise ValueError('Failed execution cannot support a passing check')
    if r['task_id']!=t['id']:raise ValueError('Verification belongs to a different task')
    inputs=json.loads((path.parent/'INPUTS.json').read_text())
    if sha(inputs['inputs'])!=record['source_hash'] or sha(inputs['policy'])!=record['policy_hash'] or sha(record['environment'])!=record['environment_hash']:raise ValueError('Retained execution binding is inconsistent')
    if receipt['args'].get('command')!=record['command'] or receipt['args'].get('task_id')!=record['task_id']:raise ValueError('Receipt command/task does not match execution record')
    for item in record['artifacts']:
        p=path.parent/'outputs'/item['path']
        if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=item['sha256']:raise ValueError('Retained generated output changed')
    current=binding(s,run,t,version)
    if r['source_hash']!=current['source_hash'] or r['policy_hash']!=current['policy_hash']:raise ValueError('Tested source or declared input/output policy changed; rerun snapshot verification')
    for item in record['artifacts'] if record['actor']==t['owner'] else []:
        p=s.directory(run)/'versions'/version/item['path']
        if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=item['sha256']:raise ValueError('Published generated output does not match verification: '+item['path'])
    return record
