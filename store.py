"""Durable task ownership, messages, receipts, and versioned project files."""
from functools import wraps
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from project_files import files as source_files, copy_source, working_copy, inspect as inspect_project

from configuration import AGENTS, LEAD, ensure_profile
import workflow
import resilience
import handoffs
import obligations

IGNORED = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', '.pytest_cache', '.pi', '.DS_Store'}


def uid():
    return uuid.uuid4().hex[:16]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe(root, name):
    root = Path(root).resolve()
    p = root / name
    if Path(name).is_absolute() or not name or '..' in Path(name).parts:
        raise ValueError('Use a relative path inside the project')
    if any(x in IGNORED for x in Path(name).parts):
        raise ValueError('Runtime/dependency metadata is excluded from project artifacts')
    if p.is_symlink() or not p.resolve().is_relative_to(root):
        raise ValueError('Symlinks outside the project are not supported')
    return p


def manifest(root):
    return {name: digest((Path(root) / name).read_bytes()) for name in source_files(root)}


def revision(files):
    return digest(json.dumps(files, sort_keys=True).encode())[:24]


def atomic_json(path, value):
    temp = path.with_name('.' + path.name + '-' + uid())
    with temp.open('w') as output:
        json.dump(value, output); output.flush(); os.fsync(output.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def locked(method):
    @wraps(method)
    def invoke(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return invoke


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        ensure_profile(self.root)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / 'team.db', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, state TEXT, prompt TEXT, created REAL, final TEXT);
          CREATE TABLE IF NOT EXISTS tasks(run TEXT, id TEXT, data TEXT, PRIMARY KEY(run,id));
          CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT, sender TEXT,
            recipient TEXT, body TEXT, wake INTEGER, state TEXT DEFAULT 'pending', created REAL);
          CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT, agent TEXT,
            kind TEXT, data TEXT, created REAL);
          CREATE TABLE IF NOT EXISTS calls(run TEXT, agent TEXT, id TEXT, request TEXT, result TEXT,
            state TEXT, PRIMARY KEY(run,agent,id));
        ''')
        workflow.initialize(self)
        resilience.initialize(self)
        handoffs.initialize(self)
        self.db.commit()

    def event(self, run, agent, kind, data):
        with self.lock, self.db:
            cur = self.db.execute('INSERT INTO events(run,agent,kind,data,created) VALUES(?,?,?,?,?)',
                                  (run, agent, kind, json.dumps(data), time.time()))
            return cur.lastrowid

    @locked
    def run(self, run):
        row = self.db.execute('SELECT * FROM runs WHERE id=?', (run,)).fetchone()
        if not row:
            raise ValueError('Unknown run')
        return dict(row)

    @locked
    def directory(self, run):
        self.run(run)
        return self.root / 'runs' / run

    def create(self, prompt, project=None, execution_mode="host"):
        if execution_mode not in ('host','container'): raise ValueError('Unknown execution mode')
        if not prompt.strip():
            raise ValueError('A real task prompt is required')
        with self.lock:
            active = self.db.execute("SELECT id FROM runs WHERE state='running'").fetchone()
            if active:
                raise ValueError('An active task already exists; pause or finish it first')
            run = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uid()[:6]
            d = self.root / 'runs' / run
            canonical = d / 'project'
            info = {'execution_mode': execution_mode, 'source': None}
            if project:
                source = Path(project).expanduser().resolve()
                if not source.is_dir() or source == self.root or self.root.is_relative_to(source):
                    raise ValueError('Select a project directory that does not contain team runtime state')
                info.update(inspect_project(source))
                working_copy(source, canonical, 'pi-team/' + run + '/integration')
            else:
                canonical.mkdir(parents=True)
            atomic_json(d / 'project-info.json', info)
            with self.db:
                self.db.execute('INSERT INTO runs VALUES(?,?,?,?,?)', (run, 'running', prompt, time.time(), None))
            self.seal(run)
            for agent in AGENTS:
                a = d / 'agents' / agent; a.mkdir(parents=True)
                working_copy(canonical, a / 'workspace', 'pi-team/' + run + '/' + agent)
                copy_source(canonical, a / 'shared')
                if execution_mode == 'host' and info.get('environment') and not (a / 'workspace/.venv').exists():
                    (a / 'workspace/.venv').symlink_to(info['environment'], target_is_directory=True)
                (a / 'base.json').write_text(json.dumps(manifest(canonical)))
            (d / 'request.txt').write_text(prompt)
            self.message(run, 'user', LEAD, {'kind': 'request', 'text': prompt}, True)
            return run

    def project_info(self, run):
        path = self.directory(run) / 'project-info.json'
        return json.loads(path.read_text()) if path.exists() else {'execution_mode': 'container', 'source': None}

    @locked
    def seal(self, run):
        d = self.directory(run); files = manifest(d / 'project'); ver = revision(files)
        target = d / 'versions' / ver
        if not target.exists():
            target.parent.mkdir(exist_ok=True)
            tmp = target.with_name('.' + uid())
            copy_source(d / 'project', tmp)
            tmp.rename(target)
        return ver

    @locked
    def tasks(self, run):
        return [json.loads(r['data']) for r in self.db.execute('SELECT data FROM tasks WHERE run=? ORDER BY rowid', (run,))]

    @locked
    def task(self, run, ident):
        return next((t for t in self.tasks(run) if t['id'] == ident), None)

    @locked
    def save_task(self, run, task):
        previous = self.task(run, task['id'])
        if not previous or previous['state'] != task['state']: task['state_changed_at'] = time.time()
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO tasks VALUES(?,?,?)', (run, task['id'], json.dumps(task)))

    def message(self, run, sender, recipient, body, wake=False):
        if recipient not in AGENTS:
            raise ValueError('Unknown recipient')
        with self.lock, self.db:
            cur = self.db.execute('INSERT INTO messages(run,sender,recipient,body,wake,created) VALUES(?,?,?,?,?,?)',
                                 (run, sender, recipient, json.dumps(body), int(wake), time.time()))
            workflow.track_message(self,cur.lastrowid,run,sender,recipient,body,time.time())
            return {'message_id': cur.lastrowid}

    def pending(self, run, agent):
        with self.lock:
            rows = self.db.execute("SELECT * FROM messages WHERE run=? AND recipient=? AND wake=1 AND state='pending' ORDER BY CASE WHEN json_extract(body,'$.kind')='loop_recovery' THEN 0 ELSE 1 END,id LIMIT 20", (run, agent)).fetchall()
            return [{**dict(x), 'body': json.loads(x['body'])} for x in rows]

    @locked
    def claim_pending(self, run, agent):
        """Claim current actionable messages; stale task generations never steer a turn."""
        if self.run(run)['state'] != 'running': return []
        selected = []
        for message in self.pending(run, agent):
            body = message['body']; task = body.get('task')
            if task and body.get('kind') in ('assignment', 'changes_requested', 'review'):
                current = self.task(run, task['id'])
                review = body['kind'] == 'review'
                if (not current or current['reviewer' if review else 'owner'] != agent
                    or current['state'] != ('review' if review else 'assigned')
                    or any(current.get(k) != task.get(k) for k in ('assignment_id', 'dispatch_id'))
                    or (review and current.get('review_id') != task.get('review_id'))):
                    self.delivered([message['id']], 'superseded'); continue
            if task and body.get('kind') in ('assignment','changes_requested'):
                try: workflow.validate_references(current.get('references',[]))
                except ValueError as exc:
                    current['state']='blocked';current['handoff_error']=str(exc);self.save_task(run,current)
                    self.delivered([message['id']], 'superseded');continue
            selected.append(message)
        self.delivered([m['id'] for m in selected], 'delivering')
        return selected

    @locked
    def settle_delivery(self, run, agent, state):
        ids = [r[0] for r in self.db.execute(
            "SELECT id FROM messages WHERE run=? AND recipient=? AND state='delivering'", (run, agent))]
        self.delivered(ids, state)

    def delivered(self, ids, state):
        with self.lock, self.db:
            for ident in ids:
                self.db.execute('UPDATE messages SET state=? WHERE id=?', (state, ident))

    def recover(self, run):
        with self.lock, self.db:
            self.db.execute("UPDATE messages SET state='pending' WHERE run=? AND state='delivering'", (run,))
            # Repair a crash between assignment persistence and notification delivery.
            for task in self.tasks(run):
                if task['state'] not in ('assigned', 'review'): continue
                recipient = task['owner'] if task['state'] == 'assigned' else task['reviewer']
                rows = self.db.execute('SELECT body FROM messages WHERE run=? AND recipient=?', (run,recipient)).fetchall()
                present = any(json.loads(r['body']).get('task',{}).get('assignment_id') == task.get('assignment_id')
                    and json.loads(r['body']).get('task',{}).get('dispatch_id') == task.get('dispatch_id')
                    and json.loads(r['body']).get('kind') in (('assignment','changes_requested') if task['state']=='assigned' else ('review',))
                    and (task['state'] != 'review' or json.loads(r['body']).get('task',{}).get('review_id') == task.get('review_id')) for r in rows)
                if not present:
                    self.message(run,'controller',recipient,{'kind':'assignment' if task['state']=='assigned' else 'review','task':task},True)
            self.dispatch_ready(run)

    def status(self, run):
        with self.lock:
            d = self.directory(run)
            return {'participant_recovery':[dict(r) for r in self.db.execute('SELECT * FROM participant_recovery WHERE run=?',(run,))], 'workflow_issues': workflow.dependency_issues(self.tasks(run)), 'message_actions': workflow.actions(self,run), 'run': self.run(run), 'roster': AGENTS, 'access': self.project_info(run), 'tasks': self.tasks(run),
                    'version': revision(manifest(d / 'project')),
                    'pending': {a: len(self.pending(run, a)) for a in AGENTS}}

    @locked
    def dispatch_ready(self, run):
        tasks = self.tasks(run)
        done = {t['id'] for t in tasks if t['state'] == 'done'}
        for task in tasks:
            if task['state'] == 'queued' and set(task['dependencies']) <= done:
                try: workflow.validate_references(task.get('references',[]))
                except ValueError as exc:
                    task['handoff_error']=str(exc);self.save_task(run,task);continue
                task.pop('handoff_error',None)
                task['state'] = 'assigned'; task['dispatch_id'] = uid(); self.save_task(run, task)
                self.message(run, LEAD, task['owner'], {'kind': 'assignment', 'task': task}, True)

    @locked
    def assign(self, run, actor, args):
        if actor != LEAD:
            raise PermissionError('Only the team lead assigns or reassigns work')
        ident = args['task_id']; owner = args['owner']; reviewer = args['reviewer']
        if owner not in AGENTS or reviewer not in AGENTS or reviewer == owner:
            raise ValueError('Choose a valid owner and a different reviewer')
        deps = args.get('dependencies', [])
        ready=args.get('ready_dependencies',(self.task(run,ident) or {}).get('ready_dependencies',[]))
        if ident in ready or any(not self.task(run,x) or self.task(run,x)['state']=='cancelled' for x in ready):raise ValueError('Ready dependencies must name reachable existing other tasks')
        if ident in deps or any(not self.task(run, x) for x in deps):
            raise ValueError('Dependencies must name existing other tasks')
        if any(self.task(run,x)['state']=='cancelled' for x in deps):
            raise ValueError('Dependencies cannot name cancelled tasks')
        candidate={'id':ident,'owner':owner,'state':'queued','dependencies':deps+ready}
        graph=[{**t,'dependencies':t['dependencies']+t.get('ready_dependencies',[])} for t in self.tasks(run) if t['id']!=ident]+[candidate]
        if any(i['task_id']==ident for i in workflow.dependency_issues(graph)):
            raise ValueError('Dependency chain is unreachable or cyclic')
        refs=workflow.validate_handoff(args)
        import publication
        outputs=publication.normalized_paths(self.directory(run)/'project',args.get('required_outputs',args.get('proposed_artifacts',[])))
        if resilience.strict(self) and args.get('proposed_artifacts') and not outputs:raise ValueError('Artifact-producing tasks must declare required_outputs')
        old = self.task(run, ident)
        if old:
            if old['state'] not in ('blocked', 'paused', 'queued'):
                raise ValueError('Pause an active task before reassigning it')
            # A task may not depend on a task which already depends on it.
            def ancestors(name, visited):
                if name in visited: return set()
                visited.add(name)
                t = self.task(run, name)
                return set(t['dependencies']) | set().union(*(ancestors(x, visited) for x in t['dependencies']))
            if any(ident in ancestors(x, set()) for x in deps):
                raise ValueError('Task dependency cycle')
        task = {'id': ident, 'owner': owner, 'reviewer': reviewer, 'description': args['description'],
                'acceptance': args['acceptance'], 'dependencies': deps, 'state': 'queued',
                'result': None, 'version': None, 'assignment_id': uid(),
                'references': refs, 'proposed_artifacts': args.get('proposed_artifacts',[]),
                'evidence_required': resilience.strict(self),
                'snapshot_verification':args.get('snapshot_verification',False),
                'report_outputs':args.get('report_outputs',[]),'verification_outputs':args.get('verification_outputs',[]),
                'acceptance_checks': args.get('acceptance_checks') or [args['acceptance']],
                'required_test_commands': args.get('required_test_commands',[]), 'required_outputs': outputs,
                'ready_dependencies':args.get('ready_dependencies', (old or {}).get('ready_dependencies',[]))}
        import snapshot_evidence
        snapshot_evidence.policy(task)
        if task['verification_outputs'] and not task['snapshot_verification']:raise ValueError('Generated verification outputs require snapshot_verification')
        if old:
            task['contract_history']=[*old.get('contract_history',[]),{k:old.get(k) for k in ('assignment_id','description','acceptance_checks','required_outputs','required_test_commands','version','review')}]
        self.save_task(run, task); self.dispatch_ready(run)
        return task

    @locked
    def sync(self, run, agent):
        d = self.directory(run); a = d / 'agents' / agent
        old = json.loads((a / 'base.json').read_text()); new = manifest(d / 'project')
        local = manifest(a / 'workspace'); conflicts = []
        for name in set(old) | set(new):
            if old.get(name) == new.get(name): continue
            if local.get(name) not in (old.get(name), new.get(name)):
                conflicts.append(name)
        if conflicts:
            raise ValueError('Sync would overwrite private changes: ' + ', '.join(conflicts))
        shared = a / 'shared'
        if shared.is_symlink(): raise ValueError('Shared snapshot is a symlink; preserve and repair it before syncing')
        raw_names = {str(p.relative_to(shared)) for p in shared.rglob('*') if p.is_file() or p.is_symlink()}
        if manifest(shared) != old or raw_names != set(old):
            backup = a / ('shared-preserved-' + uid())
            shutil.copytree(shared, backup, symlinks=True)
            self.event(run, agent, 'shared_snapshot_preserved', {'path': str(backup)})
        for name in set(old) | set(new):
            if old.get(name) == new.get(name): continue
            dst = safe(a / 'workspace', name)
            if name in new:
                dst.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(safe(d / 'project', name), dst)
            elif dst.exists(): dst.unlink()
        # Preserve the directory inode because it is bind-mounted in the worker.
        for p in (a / 'shared').iterdir():
            shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
        copy_source(d / 'project', a / 'shared')
        (a / 'base.json').write_text(json.dumps(new))
        return {'version': revision(new), 'files': list(new)}

    @locked
    def publish(self, run, agent, args):
        import publication
        return publication.publish(self,run,agent,args)

    @locked
    def repair_publication(self, run):
        import publication
        return publication.repair(self,run)

    def board_tool(self, run, actor, op, args):
        with self.lock:
            self.repair_publication(run)
            if op == 'team_evidence_catalog':
                import acceptance
                return acceptance.catalog(self,run,actor,args)
            if op == 'team_submission_contract':
                import acceptance
                return acceptance.contract(self,run,actor,args)
            if op == 'team_review_cancellation':return obligations.review(self,run,actor,args)
            if op == 'team_review_input':return handoffs.review_input(self,run,actor,args)
            if op == 'team_draft_input':return handoffs.draft_input(self,run,actor,args)
            if op == 'team_claim_review': return resilience.claim_review(self,run,actor,args)
            if op == 'team_claim_task': return resilience.claim_task(self,run,actor,args)
            if op == 'team_register_poll': return resilience.register_poll(self,run,actor,args)
            if op == 'team_reference':
                return workflow.describe_references(self,run,actor,args['paths'],args.get('source_root'))
            if op == 'team_resolve_message': return workflow.resolve(self,run,actor,args)
            if op == 'team_status': return self.status(run)
            if op == 'team_assign': return self.assign(run, actor, args)
            if op == 'team_message':
                if resilience.strict(self):handoffs.validate_message(self,run,args)
                kind = args.get('kind', 'information')
                return self.message(run, actor, args['recipient'], {'kind': kind, 'text': args['text'], 'task_id': args.get('task_id')}, kind in ('question', 'request', 'blocker'))
            if op == 'team_sync': return self.sync(run, actor)
            if op == 'team_publish': return self.publish(run, actor, args)
            if op == 'team_inbox':
                rows = self.db.execute('SELECT * FROM messages WHERE run=? AND recipient=? AND id>? ORDER BY id LIMIT 100', (run, actor, args.get('after_id', 0))).fetchall()
                return [{**dict(x), 'body': json.loads(x['body'])} for x in rows]
            if op == 'team_events':
                rows = self.db.execute('SELECT id,agent,kind,created FROM events WHERE run=? AND id>? ORDER BY id LIMIT 100', (run,args.get('after_id',0))).fetchall()
                return [dict(r) for r in rows]
            if op == 'team_evidence':
                row = self.db.execute('SELECT * FROM events WHERE run=? AND id=?', (run, args['event_id'])).fetchone()
                if not row: raise ValueError('Unknown evidence ID')
                return {**dict(row), 'data': json.loads(row['data'])}
            if op == 'team_report':
                task = self.task(run, args['task_id'])
                import acceptance
                if not task or task['owner'] != actor or task['state'] != 'assigned':
                    raise acceptance.TransitionPermissionError(task,'You are the reviewer. Use team_review' if task and task['reviewer']==actor and task['state']=='review' else 'Report only on your active assignment')
                if args.get('submission')=='ready' and (args.get('remaining') or args.get('blocked')):
                    raise acceptance.TransitionError(task,'Ready conflicts with remaining work or blocked=true; submit partial or resolve the blocker')
                import acceptance
                if not args.get('blocked') and (acceptance.partial(args) or (resilience.strict(self) and args.get('submission')!='ready')):
                    draft={'summary':args['summary'],'remaining':args.get('remaining',[]),'version':task.get('version'),'checks':args.get('checks',[])}
                    task.setdefault('draft_reports',[]).append(draft);task['remaining']=draft['remaining']
                    self.save_task(run,task);self.event(run,actor,'partial_submission',{'task_id':task['id'],**draft})
                    self.message(run,actor,task['reviewer'],{'kind':'information','task_id':task['id'],'draft':draft,'instruction':'Draft only; owner retains active work. Final acceptance is not requested.'},False)
                    return task
                packet=None
                if not args.get('blocked'):
                    if resilience.strict(self):packet=handoffs.prepare(self,run,task,args)
                    acceptance.validate(self,run,actor,task,args)
                if not args.get('blocked'):
                    import publication
                    task['submitted_outputs']=publication.verify_outputs(self,run,task,task.get('version') or args.get('version',''))
                    task['submitted_outputs_version']=task.get('version')
                task['review_handoff']=packet
                task['submission_checks']=args.get('checks',[])
                task['remaining']=[]
                task['result'] = args['summary']
                task['state'] = 'blocked' if args.get('blocked', False) else 'review'
                task['review_id'] = uid(); task['dispatch_id'] = uid()
                task['version'] = task['version'] or revision(manifest(self.directory(run) / 'project'))
                self.save_task(run, task)
                recipient = LEAD if task['state'] == 'blocked' else task['reviewer']
                self.message(run, 'controller', recipient, {'kind': task['state'], 'task': task}, True)
                if recipient != LEAD: self.message(run, actor, LEAD, {'kind': 'progress', 'task': task}, False)
                return task
            if op == 'team_request_changes':
                task = self.task(run, args['task_id'])
                if not task or (actor != LEAD and actor != task['reviewer']):
                    raise PermissionError('Only the lead or assigned reviewer can request changes')
                if task['state'] != 'review': raise ValueError('Request changes on a submitted task in review')
                if args['version'] != task['version']: raise ValueError('Correction must name the submitted version')
                if not args['summary'].strip(): raise ValueError('Explain the required corrections')
                task['review_history'] = [*task.get('review_history', []),
                    {'actor': actor, 'approved': False, 'version': task['version'], 'summary': args['summary']}]
                task['state'] = 'assigned'; task['dispatch_id'] = uid(); task['review'] = None
                self.save_task(run, task)
                self.message(run, actor, task['owner'], {'kind': 'changes_requested', 'task': task,
                    'instruction': args['summary']}, True)
                self.event(run, actor, 'corrections_requested', {'task_id': task['id'], 'version': task['version'], 'summary': args['summary']})
                return task
            if op == 'team_review':
                import acceptance
                task = self.task(run, args['task_id'])
                if not task or task['reviewer'] != actor or task['state'] != 'review':
                    raise acceptance.TransitionPermissionError(task,'Only the assigned reviewer can review a submitted task')
                if args['version'] != task['version']: raise acceptance.TransitionError(task,'Review must name the submitted version')
                if resilience.strict(self):
                    if not args.get('handoff_id'):raise acceptance.TransitionError(task,'Fetch team_review_input and supply its handoff_id')
                    handoffs.validate(self,run,task,args['handoff_id'])
                if args['approved']:
                    if task.get('handoff_error'):raise ValueError('Resolve invalid handoff before approval: '+task['handoff_error'])
                    workflow.validate_references(task.get('references',[]))
                    import acceptance
                    acceptance.validate(self,run,actor,task,args,review=True)
                task['review'] = {'reviewer': actor, **args}
                task['state'] = 'done' if args['approved'] else 'assigned'
                task['dispatch_id'] = uid()
                self.save_task(run, task)
                self.message(run, actor, LEAD, {'kind': 'review_result', 'task': task}, True)
                if not args['approved']: self.message(run, actor, task['owner'], {'kind': 'changes_requested', 'task': task}, True)
                self.dispatch_ready(run)
                return task
            if op == 'team_cancel_task':
                if actor != LEAD: raise PermissionError('Only the team lead cancels subtasks')
                task = self.task(run, args['task_id'])
                if not task: raise ValueError('Unknown task')
                if not args['reason'].strip(): raise ValueError('Explain why this subtask is no longer needed within the user scope')
                if task['state']=='cancelled':return task
                if resilience.strict(self) and task['state']!='done':
                    task['retirement_obligation']=obligations.capture(task,args['reason'])
                    self.message(run,'controller',task['retirement_obligation']['reviewer'],{'kind':'cancellation_review','task_id':task['id'],'obligation':task['retirement_obligation'],'instruction':'Independently inspect whether the cancelled scope is unnecessary or carried by a retained task. Use team_review_cancellation with evidence. Cancellation does not approve the work.'},True)
                task['state'] = 'cancelled'; task['cancellation_reason'] = args['reason']; self.save_task(run, task)
                return task
            if op == 'team_pause_task':
                if actor != LEAD: raise PermissionError('Only the team lead pauses tasks')
                task = self.task(run, args['task_id'])
                if not task: raise ValueError('Unknown task')
                task['state'] = 'paused'; self.save_task(run, task)
                self.message(run, LEAD, task['owner'], {'kind': 'pause_task', 'task': task}, False)
                return task
            if op == 'team_finish':
                if actor != LEAD: raise PermissionError('Only the team lead delivers the final result')
                if obligations.unresolved(self,run):raise ValueError('Resolve cancellation obligations before final delivery: '+json.dumps(obligations.unresolved(self,run)))
                unresolved = [t['id'] for t in self.tasks(run) if t['state'] not in ('done', 'cancelled')]
                if unresolved: raise ValueError('Resolve unfinished tasks first: ' + ', '.join(unresolved))
                ver = revision(manifest(self.directory(run) / 'project'))
                if args['version'] != ver: raise ValueError('Final delivery must reference the current version')
                import publication
                for completed in self.tasks(run):
                    if completed['state']=='done':publication.verify_outputs(self,run,completed,ver)
                if not args['validation'].strip(): raise ValueError('Describe validation and limitations')
                result = {**args, 'version': ver, 'cancelled_tasks': [{'id': t['id'], 'reason': t['cancellation_reason']} for t in self.tasks(run) if t['state']=='cancelled']}
                with self.db: self.db.execute('UPDATE runs SET state=?,final=? WHERE id=?', ('complete', json.dumps(result), run))
                (self.directory(run) / 'FINAL.md').write_text(args['summary'] + '\n\nValidation: ' + args['validation'] + '\n')
                return result
            if op == 'team_wait': return {'waiting': True, 'yield': True, 'note': 'Controller will yield after this tool batch. Relevant work or messages will wake you.'}
            raise ValueError('Unknown team operation')
