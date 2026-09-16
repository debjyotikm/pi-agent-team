"""Persistent Pi RPC actors with native workstation or container tools."""
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from store import AGENTS, uid
from host_executor import HostExecutor
from configuration import LEAD, OUTPUT_TOKENS, PROVIDERS, CREDENTIAL_ENV

ROOT = Path(__file__).resolve().parent
PI = ROOT / 'node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js'


def project_instructions(source, workspace):
    if not source: return ''
    source = Path(source).resolve(); workspace = Path(workspace)
    sections = []
    for directory in [*reversed(source.parents), source]:
        path = directory / 'AGENTS.md'
        text_path = workspace / 'AGENTS.md' if directory == source else path
        if text_path.is_file():
            sections.append(f'Instructions from {path}:\n' + text_path.read_text())
    return '\n\n'.join(sections)


def system_prompt(agent, request, access=None):
    access = access or {'execution_mode': 'host'}
    roster = {a: {**v, 'capabilities': ['inspect files', 'implement code', 'run commands and checks', 'analyze evidence', 'review changes', 'communicate with peers', 'host filesystem and network', 'installed project environments and host devices'],
                  'limits': 'Shared provider concurrency and context limits'} for a, v in AGENTS.items()}
    instructions = project_instructions(access.get('source'), access.get('workspace', '.'))
    lead = '''You are the team lead. Decide who does what, priorities, dependencies, and reviewers.
Use team_assign to dispatch work. For existing input artifacts, call team_reference(paths, source_root)
and pass its complete references (absolute paths, SHA-256, source version) into the assignment.
List future files under proposed_artifacts; they are not verified existing inputs. Do not cite unbound
commits, data pools or modules as established facts. Resolve blocked reviews before expanding the graph.
Use team_resolve_message to record an actual answer/decision to a request; receiving it is not resolution.
Never make corrections depend on approval of the defective submission: use team_request_changes. Workers execute independently; you need not approve routine calls.
Delegate coherent useful work according to the actual task and capabilities; do difficult work yourself when helpful.
Assign independent work before starting long checks. Inspect the inbox between phases. For a review, use team_review; information messages do not record approval. To reopen a submitted task for corrections, call team_request_changes with its version; a plain message does not restore owner permissions.
Resolve blockers, oversee integration, and deliver the final result using team_finish with validation evidence.
Use team_wait and end your response when waiting for workers. Do not repeatedly ask for status.
''' if agent == LEAD else f'''{LEAD} leads this team and decides task assignments and priorities.
Execute your assignments autonomously. Coordinate directly with peers and raise concrete blockers to {LEAD}.
For assigned work: sync, inspect, implement, check, publish changed files, then team_report.
For a review request: call team_review_input(task_id), then inspect only its exact paths and receipts,
check the acceptance criteria and evidence, then team_review. Do not edit another owner's task.
Use team_wait when no actionable work remains. It yields execution after the current tool batch; call it alone. Respond to actionable requests with team_resolve_message or the required task transition.
'''
    return f'''You are {agent}, model {AGENTS[agent]['model']}, reasoning setting {AGENTS[agent]['thinking']}.
You are part of a real task execution team. Everyone knows the roster and capabilities below.
{json.dumps(roster, indent=2)}
{lead}
Applicable project instructions (follow the user request if instructions conflict):
{instructions}

The original user request is:
{request}

The controller detects repeated equivalent calls with unchanged results. After four repeats it yields
and restores your session with a recovery checkpoint. A second loop stalls only your participant until
operator retry; peers can claim eligible reviews/tasks. Use team_wait instead of sleep or hash/status loops.
Legitimate local-job polling can register the actual PID, exact poll command and a finite lease with
team_register_poll; it is not a bypass for arbitrary repetitive work.
Publish drafts early, but team_report(submission="partial",remaining=[...],checks=[...]) keeps your
assignment active. Final team_report(submission="ready",version=...,checks=[...],remaining=[])
requires passing tool evidence for each exact acceptance_checks entry. Each check names criterion,
status, summary and evidence_ids. For tasks with snapshot_verification=true, publish executable source first, then use team_verify(task_id,
version,command) for every required command. It executes a private copy of published inputs, records the
environment and returns retained generated artifacts. Copy declared generated outputs into your workspace,
write the declared reports, and publish them. Report/result-only changes preserve source evidence; source
changes require rerunning. Declared result files are absent at execution start and must be freshly generated.
Use team_evidence_catalog(task_id) to retrieve your eligible execution and exact-version read receipt IDs;
message IDs, publication IDs, writes and model events are not acceptance evidence. For legacy tasks only,
use successful run receipts against matching source or read receipts for the exact published version. Failed or stale evidence cannot support acceptance.
Review approvals require your own independent checks of that submitted version. For failed tests,
fix them and rerun the registered required_test_commands. Do not relabel failures as passed or omit criteria.
team_claim_review can take an exact-version review from a stalled reviewer or after exhausted reminders.
team_claim_task can take existing executable work from a stalled owner; preserve its scope and old workspace.
Neither tool grants new additional permissions. Do not self-assign new tasks or approve your own work.
Work only within this request. Read applicable project AGENTS.md instructions before editing. Never silently weaken acceptance criteria. Distinguish observations from assumptions.
Execution context: {json.dumps(access)}
In host mode your tools run on the workstation under the user's account with its normal filesystem,
network, SSH, Docker, installed software and GPU access. Absolute paths and run(cwd=...) are supported.
Relative paths and default commands target your working copy. Use the actual workspace/shared paths above;
/workspace and /shared are container-mode paths only. Working copies coordinate edits, not restrict access.
Read applicable ancestor and project AGENTS.md files before work. Coordinate changes to shared environments,
services, GPUs, original project files, and shared resources with {LEAD}. Access is not an instruction to launch
training, alter services, send messages externally, or use data outside the user's task.
For a source .venv reused by your working copy, PYTHONPATH prefers your working copy's code; avoid changing
shared editable installs for each worker. Shell commands can use the original absolute .venv/bin/python too.
Only tools registered by this harness are available; network CLI tools remain available.
Output and changes persist between sessions.
team_publish integrates existing regular files with conflict detection and verified snapshot receipts.
Missing file paths reject the whole publication; they NEVER imply deletion. Explicit deletions require
path and full current SHA-256 in deletions. Inspect the returned artifacts/changes before claiming delivery.
Assignments declare required_outputs (relative paths); ready reports and approvals verify every required
file in the exact snapshot. A publication event listing requested names is not proof that old files existed.
Shared directories are read-only snapshots, not a messageboard or place to author deliverables. Write
new code/reports in your own workspace, then team_publish the explicit relative paths under your task.
Do not symlink, merge, delete or manually repair another agent's workspace, shared snapshot or environment.
Use team_sync to receive published changes; include artifact version and evidence IDs when requesting review.
Shell commands fail on unhandled errors and pipeline failures (bash -e -o pipefail). Use if/else or an explicit || handler for expected nonzero exits, and retain the actual test status.
After compaction, missing recollection is not evidence of deleted work. Verify durable write/test/execution
receipts before claiming an artifact existed, a command ran, or files were lost. Correct unsupported claims.
Messages are tool-delivered; ordinary assistant narration does not reach peers.
Information and acknowledgments do not wake peers. Use questions/requests only when action is needed.
After compaction use team_status, team_inbox and team_evidence to retrieve authoritative state.
Formal reviews are dispatched only by a ready team_report carrying publication_id (the verified
team_publish transaction_id), version and passing checks. The controller builds the handoff packet.
Call team_review_input before reviewing; include its handoff_id in team_review. If unavailable, the
controller records one owner blocker: continue another assignment or team_wait. Do not search guessed
paths, fabricate snapshot IDs or try to download supposedly local artifacts. For provisional feedback,
use team_draft_input: it captures actual files, lists missing outputs, and grants no approval authority.
Plain messages are conversation, not review assignments. Label draft feedback explicitly. A task's
ready_dependencies must have independent approvals before the implementation can be submitted ready.
Before ready or approval, fetch team_submission_contract(task_id). Copy its exact criteria once each;
fill pending checks with your own actual evidence. Do not paraphrase, split, or borrow another task's
criteria. A mismatch response lists expected, missing and unexpected criteria. Cancellation preserves
unfinished review obligations: an independent reviewer must use team_review_cancellation to justify
retirement or verify a retained replacement. Cancellation is never acceptance. Blocked decisions and
exhausted reminders remain visible until concretely resolved; do not create duplicate tasks to hide them.
Report relevant evidence IDs, exact artifact versions, and remaining limitations. A model's claim is not a check result.
'''


class RPC:
    def __init__(self, command, cwd, env, directory, observe=lambda e: None):
        self.queue = queue.Queue(); self.observe = observe
        self.log = (directory / 'rpc.jsonl').open('a'); self.err = (directory / 'stderr.log').open('a')
        self.process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.err, text=True, bufsize=1)
        self.thread = threading.Thread(target=self._read, daemon=True); self.thread.start()
        self.settled = False; self.ends = []; self.command_lock = threading.RLock()

    def _read(self):
        try:
            for line in self.process.stdout:
                self.log.write(line); self.log.flush()
                try: event = json.loads(line)
                except ValueError: event = {'type': 'invalid_json', 'line': line[:2000]}
                self.queue.put(event)
        finally:
            self.queue.put({'type': 'process_exit', 'code': self.process.poll()})

    def wait(self, predicate, timeout=21600):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try: event = self.queue.get(timeout=.5)
            except queue.Empty: continue
            if event.get('type') == 'agent_end': self.ends.append(event)
            if event.get('type') == 'agent_settled': self.settled = True
            self.observe(event)
            if event.get('type') in ('process_exit', 'invalid_json'): raise RuntimeError(str(event))
            if predicate(event): return event
        raise TimeoutError('Pi operation exceeded six-hour transport watchdog; session retained')

    def request(self, kind, **kwargs):
        with self.command_lock:
            ident = uid()
            self.process.stdin.write(json.dumps({'id': ident, 'type': kind, **kwargs}) + '\n'); self.process.stdin.flush()
            result = self.wait(lambda e: e.get('id') == ident)
            if not result.get('success'): raise RuntimeError(result.get('error', str(result)))
            return result.get('data')

    def prompt(self, text):
        with self.command_lock:
            self.settled = False; self.ends = []; self.intentional_yield = False
            self.request('prompt', message=text)
            if not self.settled: self.wait(lambda e: e.get('type') == 'agent_settled')
            state = self.request('get_state')
            if state['isStreaming'] or state['isCompacting'] or state.get('pendingMessageCount', 0):
                raise RuntimeError('Pi settled with pending work')
            assistants = [m for end in self.ends for m in end.get('messages', []) if m.get('role') == 'assistant']
            if not assistants: raise RuntimeError('Pi returned no assistant result')
            final = assistants[-1]
            if self.intentional_yield and (final.get('stopReason') == 'aborted' or (final.get('stopReason') == 'error' and final.get('errorMessage') == 'This operation was aborted')):
                return {'stopReason':'yield','content':[]}
            if final.get('stopReason') in ('error', 'aborted', 'length'):
                raise RuntimeError(final.get('errorMessage') or final['stopReason'])
            return final

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try: self.process.wait(timeout=5)
            except subprocess.TimeoutExpired: self.process.kill(); self.process.wait()
        self.thread.join(timeout=3)
        if not self.thread.is_alive(): self.log.close()
        self.err.close()


class Runtime:
    def __init__(self, store, run, agent, endpoint, token, observe):
        self.store = store; self.run = run; self.agent = agent
        self.directory = store.directory(run) / 'agents' / agent
        self.container = 'pi-team-' + run.lower() + '-' + agent
        self.lock = threading.RLock()
        self.close_lock = threading.Lock(); self.closed = False
        self.access = store.project_info(run)
        self.mode = self.access.get('execution_mode', 'container')
        self.host = HostExecutor(self.directory / 'workspace', self.access.get('source')) if self.mode == 'host' else None
        if self.mode == 'container': self._container()
        (self.directory / 'sessions').mkdir(exist_ok=True)
        session = self.directory / 'sessions' / 'session.jsonl'
        config = AGENTS[agent]
        command = ['node', str(PI), '--offline', '--mode', 'rpc', '--provider', config['provider'],
                   '--model', config['model'], '--thinking', config['thinking'], '--session', str(session),
                   '--no-builtin-tools', '--no-extensions', '--no-skills', '--no-context-files', '--no-prompt-templates',
                   '--extension', str(ROOT / 'extension.ts'), '--system-prompt', system_prompt(agent, store.run(run)['prompt'], {**self.access, 'workspace': str(self.directory / 'workspace'), 'shared': str(self.directory / 'shared')})]
        # Explicit environment prevents unrelated API credentials or project config from leaking into Pi.
        env = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG', 'NODE_EXTRA_CA_CERTS', 'HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY') if k in os.environ}
        env.update({k:os.environ[k] for k in CREDENTIAL_ENV if k in os.environ})
        env['PI_TEAM_REQUEST_OVERRIDES']=json.dumps(PROVIDERS[config['provider']].get('request_overrides',{}))
        env.update(PI_CODING_AGENT_DIR=str(store.root / 'pi'), PI_TEAM_ENDPOINT=endpoint, PI_TEAM_TOKEN=token, PI_TEAM_OUTPUT_TOKENS=str(OUTPUT_TOKENS))
        try:
            self.rpc = RPC(command, self.directory, env, self.directory, observe)
            state = self.rpc.request('get_state')
            if state.get('model', {}).get('id') != config['model'] or state.get('thinkingLevel') != config['thinking']:
                raise RuntimeError('Pi model or reasoning setting differs from requested roster: ' + str(state))
        except Exception:
            if hasattr(self, 'rpc'): self.rpc.close()
            if self.host: self.host.close()
            else: subprocess.run(['docker','stop','-t','1',self.container],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            raise

    def _container(self):
        p = subprocess.run(['docker', 'inspect', self.container], capture_output=True)
        if p.returncode == 0:
            subprocess.run(['docker', 'start', self.container], check=True, stdout=subprocess.DEVNULL)
            return
        subprocess.run(['docker', 'run', '-d', '--name', self.container, '--label', 'pi-team.managed=true',
                        '--label', 'pi-team.run=' + self.run, '--restart=no', '--init', '--read-only',
                        '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=512',
                        '--tmpfs', '/tmp:rw,nosuid,size=2g', '--user', f'{os.getuid()}:{os.getgid()}',
                        '-v', str(self.directory / 'workspace') + ':/workspace',
                        '-v', str(self.directory / 'shared') + ':/shared:ro',
                        'pi-team-worker:0.1'], check=True, stdout=subprocess.DEVNULL)

    def execute(self, command, timeout, cwd=None):
        if self.host: return self.host.execute(command, timeout, cwd)
        # timeout runs inside the container, so timing out docker exec cannot leave the command running.
        result = subprocess.run(['docker', 'exec', '-w', cwd or '/workspace', self.container, 'timeout', '--kill-after=5s', str(timeout),
                                 'bash', '-e', '-o', 'pipefail', '-lc', command], capture_output=True, text=True, errors='replace', timeout=timeout+15)
        return {'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}

    def interrupt(self):
        if self.closed: return
        # Write abort without waiting for the prompt's command lock. Its reader consumes the response.
        try:
            self.rpc.process.stdin.write(json.dumps({'id': uid(), 'type': 'abort'}) + '\n')
            self.rpc.process.stdin.flush()
        except (BrokenPipeError, ValueError): pass
        if self.host: self.host.interrupt()
        else: subprocess.run(['docker', 'stop', '-t', '1', self.container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def close(self):
        with self.close_lock:
            if self.closed: return
            self.closed = True
            self.rpc.close()
            if self.host: self.host.close()
            else: subprocess.run(['docker', 'stop', '-t', '3', self.container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
