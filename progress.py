"""Durable, bounded reconciliation of idle actors against unfinished team work."""
import hashlib
import json
import time
import workflow
import resilience
from store import AGENTS
from configuration import LEAD


class ProgressReconciler:
    def __init__(self, store, idle_grace=30, retry_delay=60, max_attempts=2):
        self.store = store
        self.idle_grace = idle_grace
        self.retry_delay = retry_delay
        self.max_attempts = max_attempts
        with store.lock, store.db:
            store.db.execute('''CREATE TABLE IF NOT EXISTS progress_repairs(
                run TEXT, fingerprint TEXT, attempts INTEGER, last_sent REAL,
                PRIMARY KEY(run, fingerprint))''')

    def check(self, run, activity, *, now=None, repair=False):
        now = time.time() if now is None else now
        s = self.store
        with s.lock, s.db:
            lifecycle = s.run(run)['state']
            if lifecycle != 'running':
                return {'state': lifecycle, 'reason': 'Run is not active.'}
            tasks = s.tasks(run)
            agents = {a: activity.get((run, a), {}) for a in AGENTS}
            busy = [a for a, v in agents.items() if v.get('state') not in ('waiting', 'error', 'stopped', 'stalled')]
            stalled_agents=[a for a in AGENTS if resilience.stalled(s,run,a)]
            if stalled_agents:
                return {'state':'participant_stalled','agents':stalled_agents,'working_agents':busy,'reason':'Bounded loop recovery exhausted. Eligible independent peers may claim existing reviews/tasks. Operator retry preserves the session.'}
            priorities=workflow.priorities(s,run,activity,now)
            if priorities and (busy or any(p['kind'] in ('unreachable_dependency','invalid_handoff','idle_assignment','publication_output_gap','publication_without_resolution','blocked_decision','unresolved_cancellation') for p in priorities)):
                if repair:
                    for issue in priorities[:8]:workflow.alert(s,run,issue,now)
                return {'state':'coordination_required','agents':busy,'issues':priorities,
                    'reason':'Resolve blocking reviews, unreachable dependencies or unanswered decisions; activity elsewhere does not clear these issues.'}
            if busy:
                delayed = [dict(r) for r in s.db.execute(
                    "SELECT id,sender,recipient,created FROM messages WHERE run=? AND wake=1 AND state='pending' AND created<? ORDER BY id LIMIT 10", (run, now-60))]
                return {'state': 'active', 'agents': busy,
                    'reason': ('Agents are active, but actionable messages have waited over 60 seconds.' if delayed else 'Agent turns or tool operations are active.'),
                    'delayed_messages': delayed}

            # A disconnected frontend must not make an executing backend job look idle.
            executing = s.db.execute("SELECT count(*) FROM calls WHERE run=? AND state='executing'", (run,)).fetchone()[0]
            if executing:
                return {'state': 'executing', 'reason': 'Backend tool calls remain unresolved.', 'executing_calls': executing}
            failed = [a for a, v in agents.items() if v.get('state') == 'error']
            if failed:
                return {'state': 'participant_error', 'agents': failed,
                        'reason': 'Participant retries are exhausted; inspect the error and retry or reassign.'}
            pending = [a for a in AGENTS if s.pending(run, a)]
            if pending:
                return {'state': 'dispatch_pending', 'agents': pending, 'reason': 'Actionable messages await actor delivery.'}
            unfinished = [t for t in tasks if t['state'] not in ('done', 'cancelled')]
            reviews = [t for t in unfinished if t['state'] == 'review']
            assigned = [t for t in unfinished if t['state'] == 'assigned']
            if reviews:
                task = reviews[0]; recipient = task['reviewer']; kind = 'review'
                reason = f"Task {task['id']} still requires a formal review by {recipient}."
            elif assigned:
                task = assigned[0]; recipient = task['owner']; kind = 'assignment'
                reason = f"Task {task['id']} has an idle owner and no queued continuation."
            elif unfinished:
                task = None; recipient = LEAD; kind = 'blocked'
                reason = 'No agent is working; remaining tasks are blocked, paused, or waiting on dependencies.'
            else:
                task = None; recipient = LEAD; kind = 'finalization'
                reason = 'All subtasks are resolved, but the team lead has not delivered the final result.'
            user = s.db.execute("SELECT max(id) FROM messages WHERE run=? AND sender='user'", (run,)).fetchone()[0]
            identity = {'tasks': [{k: t.get(k) for k in ('id', 'state', 'assignment_id', 'dispatch_id', 'review_id', 'version')} for t in tasks],
                        'latest_user_message': user, 'kind': kind}
            fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            prior = s.db.execute('SELECT attempts,last_sent FROM progress_repairs WHERE run=? AND fingerprint=?', (run, fingerprint)).fetchone()
            attempts, last_sent = (prior['attempts'], prior['last_sent']) if prior else (0, 0)
            peer_ids = [row[0] for row in s.db.execute(
                "SELECT id FROM messages WHERE run=? AND recipient=? AND sender!='controller' AND wake=0 AND state='pending' ORDER BY id DESC LIMIT 10", (run, LEAD))]
            result = {'state': 'awaiting_review' if kind == 'review' else 'awaiting_finalization' if kind == 'finalization' else 'blocked' if kind == 'blocked' else 'stalled',
                      'reason': reason, 'next_actor': recipient, 'task_id': task['id'] if task else None,
                      'repair_attempts': attempts, 'max_repair_attempts': self.max_attempts,
                      'recent_information_ids': sorted(peer_ids)}
            if attempts >= self.max_attempts:
                result.update(state='stalled', next_actor=LEAD,
                              reason=reason + ' Automatic reminders exhausted; inspect and resolve the task or report a concrete blocker.')
                return result
            quiet_since = max((v.get('updated', now) for v in agents.values()), default=now)
            latest_message = s.db.execute('SELECT max(created) FROM messages WHERE run=?', (run,)).fetchone()[0] or 0
            quiet_since = max(quiet_since, latest_message)
            due = max(quiet_since + self.idle_grace, last_sent + self.retry_delay)
            result['retry_after_seconds'] = max(0, round(due - now, 1))
            if not repair or now < due:
                return result
            # Second attempt goes to the lead. Never auto-approve or auto-complete work.
            if attempts:
                recipient = LEAD
            instructions = {
                'review': 'Read the submitted version and evidence, then call team_review with approved=true or false. team_report is for the owner, and an informational message is not a formal review. If blocked, send the team lead a request with the precise issue.',
                'assignment': 'Inspect the current assignment, continue actionable work, and submit with team_report when ready. If work cannot continue, report the concrete blocker rather than silently waiting.',
                'blocked': 'Inspect current blockers and dependencies, arrange the next authorized action or clearly report what user input is required. Do not assume expanded authority or bypass resource/data restrictions.',
                'finalization': 'Read the existing final reviews and recent peer confirmations. If the requested scope is satisfied, call team_finish with the current integrated version and validation evidence. Do not repeat completed checks or start unrelated work merely to close this run.',
            }
            body = {'kind': 'progress_repair', 'reason': reason, 'instruction': instructions[kind],
                    'task_id': task['id'] if task else None, 'expected_recipient': result['next_actor'],
                    'fingerprint': fingerprint, 'attempt': attempts + 1,
                    'recent_information_ids': sorted(peer_ids),
                    'note': 'Fetch team_status first; this is a coordination reminder, not new task authority. Messages and evidence remain available through team_inbox/team_evidence.'}
            if attempts:
                body['escalation'] = 'A prior targeted reminder produced no task transition. Resolve the workflow with the assigned participant; do not substitute your approval for theirs.'
            sent = s.message(run, 'controller', recipient, body, True)
            s.db.execute('INSERT OR REPLACE INTO progress_repairs VALUES(?,?,?,?)', (run, fingerprint, attempts + 1, now))
            s.event(run, 'controller', 'progress_repair', {**body, 'recipient': recipient, **sent})
            result.update(state='repair_queued', next_actor=recipient, repair_attempts=attempts + 1, **sent)
            return result
