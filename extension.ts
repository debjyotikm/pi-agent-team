import { Type } from '@earendil-works/pi-ai';
import { defineTool, type ExtensionAPI } from '@earendil-works/pi-coding-agent';

const S = Type.String, O = Type.Optional;
const Check = Type.Object({criterion:S(),status:Type.Union([Type.Literal('pass'),Type.Literal('fail'),Type.Literal('pending')]),summary:S(),evidence_ids:Type.Array(Type.Number())});
const definitions = [
  ['team_verify','Execute a command on a private copy of the current published task snapshot. Captures source, environment, outputs and exit status. Copy returned generated artifacts into your workspace before publishing them with your report. Report-only edits retain eligibility; source edits require rerunning.',Type.Object({task_id:S(),version:S(),command:S(),timeout_seconds:O(Type.Number())})],
  ['team_evidence_catalog','Get your task-scoped eligible evidence receipts, or explain exactly why supplied IDs are invalid. Eligibility is provenance, not proof of a criterion.',Type.Object({task_id:S(),version:O(S()),evidence_ids:O(Type.Array(Type.Number()))})],
  ['team_submission_contract','Fetch exact acceptance criteria as an unfilled check template, required files, commands, version and publication ID. Fill it with real evidence; never mark pending criteria pass without verification.',Type.Object({task_id:S()})],
  ['team_review_cancellation','Independent cancellation reviewer: inspect preserved scope and record whether a retained task carries it or it is unnecessary. No acceptance is granted. Transferred scope stays open until the replacement is independently complete.',Type.Object({task_id:S(),disposition:Type.Union([Type.Literal('superseded'),Type.Literal('unnecessary')]),replacement_task_id:O(S()),summary:S(),evidence_ids:Type.Array(Type.Number())})],
  ['team_review_input','Fetch the controller-verified formal review packet: exact paths, publication and test receipts. Missing inputs generate one owner blocker; return to other work instead of guessing paths.',Type.Object({task_id:S()})],
  ['team_draft_input','Capture existing required output files from a task owner for explicitly labelled draft feedback. Lists missing files. Does not create a formal review or approve anything.',Type.Object({task_id:S()})],
  ['team_claim_review','Claim an exact-version review when its reviewer is stalled or overdue after two reminders. Self-review is prohibited.',Type.Object({task_id:S(),version:S(),reason:S()})],
  ['team_claim_task','Claim an existing executable task of a stalled owner. Scope, criteria and dependencies remain unchanged; prior workspace is preserved.',Type.Object({task_id:S(),reason:S()})],
  ['team_register_poll','Register a temporary polling exemption for a live local process owned by this account. Use the exact command and returned job_id in run.poll_job_id; at most one hour.',Type.Object({pid:Type.Number(),command:S(),expires_seconds:Type.Number()})],
  ['team_reference', 'Inspect existing input files and return verified absolute paths, full SHA-256 hashes and current source versions for team_assign. Does not grant additional permissions.', Type.Object({paths:Type.Array(S()),source_root:O(S())})],
  ['team_resolve_message', 'Recipient or lead: record the concrete answer/decision resolving an actionable message; include supporting evidence IDs. Receipt alone is not resolution. Task transitions also resolve linked requests.', Type.Object({message_id:Type.Number(),summary:S(),evidence_ids:O(Type.Array(Type.Number()))})],
  ['team_status', 'Read the full team roster, original request, task board, and current artifact version.', Type.Object({})],
  ['team_assign', 'Team lead only: assign a task with acceptance criteria, valid dependencies and a different reviewer. Existing artifact inputs require references from team_reference; list files to create in proposed_artifacts. Do not put unbound file or commit claims in the description. Pause existing work before reassignment.', Type.Object({task_id:S(),owner:S(),reviewer:S(),description:S(),acceptance:S(),dependencies:O(Type.Array(S())),required_outputs:Type.Array(S()),ready_dependencies:O(Type.Array(S())),acceptance_checks:Type.Array(S()),required_test_commands:O(Type.Array(S())),snapshot_verification:O(Type.Boolean()),report_outputs:O(Type.Array(S())),verification_outputs:O(Type.Array(S())),references:Type.Array(Type.Object({path:S(),sha256:S(),source_root:S(),source_version:S()})),proposed_artifacts:Type.Array(S())})],
  ['team_message', 'Send a peer a message. Only question/request/blocker wakes them; information/ack is recorded without causing reply loops.', Type.Object({recipient:S(),text:S(),kind:Type.Union(['question','request','blocker','information','ack'].map(x=>Type.Literal(x))),task_id:O(S())})],
  ['team_inbox', 'Retrieve messages, including information that did not wake you.', Type.Object({after_id:O(Type.Number())})],
  ['team_sync', 'Refresh your read-only shared snapshot and clean workspace files to the latest integrated project. Refuses to overwrite private changes.', Type.Object({})],
  ['team_publish', 'Publish existing regular files from your workspace. Missing inputs reject the entire operation. Explicit deletions require path and current full SHA-256 in deletions. Success includes verified snapshot paths, hashes, sizes and changes.', Type.Object({task_id:S(),files:Type.Array(S()),deletions:O(Type.Array(Type.Object({path:S(),sha256:S()})))})],
  ['team_report', 'Report your assignment for review, or report a blocker to the team lead. Use partial while anything remains: publication does not relinquish your assignment. Ready requires the exact published version and passing evidence for every acceptance criterion.', Type.Object({task_id:S(),summary:S(),submission:Type.Union([Type.Literal('partial'),Type.Literal('ready')]),remaining:Type.Array(S()),version:O(S()),publication_id:O(S()),checks:Type.Array(Check),blocked:O(Type.Boolean())})],
  ['team_request_changes', 'Lead or assigned reviewer: reopen a submitted task for corrections on its exact version; this restores owner write/run access and notifies them. Use this instead of merely messaging corrections.', Type.Object({task_id:S(),version:S(),summary:S()})],
  ['team_review', 'Assigned reviewer: approve or request changes on the exact submitted version. Read its files and check the acceptance criteria first.', Type.Object({task_id:S(),version:S(),approved:Type.Boolean(),summary:S(),handoff_id:O(S()),checks:Type.Array(Check)})],
  ['team_cancel_task', 'Team lead only: retire an unnecessary subtask with a recorded reason. This does not change the original user requirements; dependent tasks must be reassigned explicitly.', Type.Object({task_id:S(),reason:S()})],
  ['team_pause_task', 'Team lead only: pause a task before redirecting or reassigning its owner.', Type.Object({task_id:S()})],
  ['team_finish', 'Team lead only: deliver the completed user task after reviews and relevant checks. Must name the current integrated version.', Type.Object({version:S(),summary:S(),validation:S()})],
  ['team_wait', 'Yield at the end of this tool batch. Call alone. No new model request is made until actionable work arrives.', Type.Object({})],
  ['team_events', 'List durable event IDs in order, including older evidence after compaction. Fetch full records with team_evidence.', Type.Object({after_id:O(Type.Number())})],
  ['team_evidence', 'Read a durable execution record by event ID. Output is paginated; pass offset for large records.', Type.Object({event_id:Type.Number(),offset:O(Type.Number())})],
  ['read', 'Read a UTF-8 file. In host mode absolute/tilde paths access the workstation; relative paths use your working copy. Shared/version scopes address team snapshots. Returns a content hash and paginated content.', Type.Object({path:S(),task_id:O(S()),scope:O(Type.Union([Type.Literal('workspace'),Type.Literal('shared'),Type.Literal('version')])),version:O(S()),offset:O(Type.Number())})],
  ['write', 'Write a UTF-8 file. In host mode absolute/tilde paths access the workstation; relative paths use your working copy. Use publish to integrate working-copy changes.', Type.Object({path:S(),content:S()})],
  ['run', 'Execute a shell command on the workstation in host mode, with the user account filesystem, network, SSH, GPU, and software access. Optional cwd selects any host directory. Defaults to your working copy. Full output is retained as evidence.', Type.Object({command:S(),cwd:O(S()),timeout_seconds:O(Type.Number()),poll_job_id:O(S())})],
] as const;

export default function(pi: ExtensionAPI) {
  const endpoint = process.env.PI_TEAM_ENDPOINT!;
  const token = process.env.PI_TEAM_TOKEN!;
  async function call(path: string, body: unknown, signal?: AbortSignal) {
    const response = await fetch(endpoint + path, {method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+token},body:JSON.stringify(body),signal});
    const result = await response.json();
    if (!response.ok || (result.error && !result.yield)) throw new Error(JSON.stringify(result));
    return result;
  }
  let yieldRequested=false;let yieldCommitted=false;
  const presented = new Set<number>();
  pi.on('agent_start', async () => {yieldRequested=false;yieldCommitted=false;});
  pi.on('message_end', async (event) => {
    const m=event.message as any;
    if(m.role!=='user')return;
    const text=(m.content??[]).filter((x:any)=>x.type==='text').map((x:any)=>x.text).join('\n');
    const marker='PI_TEAM_MESSAGES=';const i=text.indexOf(marker);
    if(i>=0){try{for(const m of JSON.parse(text.slice(i+marker.length)))presented.add(m.id);}catch{}}
  });
  pi.on('tool_call', async () => yieldRequested ? {block:true,reason:'Agent has yielded; remaining calls in this batch are not executed.',terminate:true} : undefined);
  for (const [name, description, parameters] of definitions) {
    pi.registerTool(defineTool({name,label:name,description,parameters,
      async execute(callId, params, signal) {
        const result = await call('/tool', {tool:name,call_id:callId,args:params}, signal);
        if(result.yield)yieldRequested=true;
        if (typeof result.exit_code === 'number' && result.exit_code !== 0) throw new Error(JSON.stringify(result));
        return {content:[{type:'text' as const,text:JSON.stringify(result)}],details:result};
      },
    }));
  }
  pi.on('turn_end', async (_event, ctx) => {
    if(yieldRequested){if(!yieldCommitted){yieldCommitted=true;await call('/yielded',{});ctx.abort();}return;}
    // Pi supports steering after the assistant message and its tool results finish.
    // Claims stay 'delivering' until the whole prompt settles; crash/pause replays them.
    const delivery = await call('/mailbox', {}, AbortSignal.timeout(10000));
    if (delivery.messages.length) {
      pi.sendUserMessage('Controller mid-turn delivery. Handle new corrections, blockers and review requests before continuing stale work.\nPI_TEAM_MESSAGES=' + JSON.stringify(delivery.messages), {deliverAs:'steer'});
    }
  });
  pi.on('before_provider_request', async (event, ctx) => {
    if(presented.size){await call('/presented',{message_ids:[...presented]});presented.clear();}
    const payload = {...event.payload} as any;
    Object.assign(payload, JSON.parse(process.env.PI_TEAM_REQUEST_OVERRIDES ?? '{}'));
    // Record only effective request metadata, never headers or credentials.
    await call('/request_meta', {model:payload.model,reasoning:payload.reasoning?.effort ?? null,
      enable_thinking:payload.chat_template_kwargs?.enable_thinking ?? null,
      max_tokens:payload.max_tokens ?? payload.max_output_tokens ?? null});
    return payload;
  });
  pi.on('session_before_compact', async (event, ctx) => {
    // A bounded, deterministic checkpoint cannot overflow a summarization backend.
    // Full native session history and raw tool receipts remain retrievable.
    const checkpoint = await call('/checkpoint', {});
    const entries = event.branchEntries;
    let first = '';
    for (let i=entries.length-1;i>=0;i--) {
      const e = entries[i] as any;
      if (e.type === 'message' && e.message?.role === 'user') {
        if (JSON.stringify(entries.slice(i)).length <= 24000) first=e.id;
        break;
      }
    }
    return {compaction:{summary:JSON.stringify(checkpoint),firstKeptEntryId:first,
      tokensBefore:event.preparation.tokensBefore,details:{kind:'durable-team-checkpoint'}}};
  });
}
