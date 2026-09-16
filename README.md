# Pi Agent Team

A local, persistent multi-agent coding harness built around [Pi](https://github.com/earendil-works/pi). A lead assigns work; separate Pi sessions implement, communicate, publish changes and independently review the results.

The controller keeps task ownership, messages, tool receipts and versioned artifacts in SQLite and local files. Agents run when they have actionable work and yield when idle. A model's completion claim does not bypass the configured review checks.

## Requirements

- Linux, Python 3.12+, Node.js 22+, Git and Bash.
- A running, tool-capable model endpoint supported by Pi. This repository does not install or launch a model server.
- Docker only if you choose container command execution.

Pi is pinned in `package-lock.json`. Python controller and tests use the standard library.

## Setup

```bash
npm ci --ignore-scripts
cp examples/team.example.json team.local.json
```

Edit `team.local.json` with your provider URL, real model ID, context/output limits, team roster and provider concurrency. The example names are placeholders; it does not work against a model until configured. Any supported roster has one lead and at least one independent reviewer. The configuration accepts up to 100 identities, but this is a validation limit, not a tested capacity claim. Size concurrency for your backend.

For an authenticated endpoint, set the environment variable named by `api_key_env`. Keep the credential out of JSON. For an unauthenticated local endpoint, remove `api_key_env`. Only named credential variables are forwarded to the Pi process. Native shell tools inherit the controller account's environment and access.

```bash
python3 team.py configure
python3 team.py doctor
python3 team.py serve
```

Open **http://127.0.0.1:18890** for the task board and conversation/tool activity. In another terminal:

```bash
python3 team.py submit \
  --project /path/to/your/project \
  'Add input validation and tests. Divide independent work, review the changes, and report what passed.'
```

Supply an existing project of your own at the path above. You can omit `--project` to start an empty workspace.

```bash
python3 team.py submit --project /path/to/project --prompt-file /path/to/task.txt
python3 team.py status
python3 team.py message RUN_ID 'Please prioritize the failing test.' --recipient lead
python3 team.py pause RUN_ID
python3 team.py resume RUN_ID
python3 team.py retry RUN_ID worker-1
```

Use `PI_TEAM_CONFIG=/path/to/team.json` to select another configuration, and `--state /path/to/state --port PORT` before the subcommand to select another controller. Keep the same configuration for every command operating on a state directory. Configuration is pinned to durable state; changing it requires a separate state directory. Separate controllers need separate ports.

`doctor` checks installed Pi, credential-variable presence and model discovery through `GET /models`. A provider that uses a different discovery API can fail that check even if Pi supports its inference API. No model inference is performed by `doctor`.

## Execution and privacy

Host execution is the default. Commands use the controller's Unix account, filesystem, network, installed environments and devices. Private workspaces coordinate changes; they are **not a security sandbox**. Use a dedicated account or container environment when you need stronger isolation.

Container command execution is optional:

```bash
docker build -t pi-team-worker:0.1 .
python3 team.py submit --execution-mode container 'Build a small command-line tool and test it.'
```

The Pi process and controller still run on the host. Container commands get writable workspace and read-only shared mounts. `team_verify` currently supports host execution only; container tasks must use the ordinary execution/read evidence path.

The dashboard binds to loopback. Read-only endpoints expose task text and tool output to local clients; administrative and agent mutations use bearer tokens. Keep it local, or use an authenticated tunnel you control. It is not designed as a public multi-user service.

Runtime state contains prompts, conversations, source snapshots, execution output and potentially sensitive data. Keep `state/`, local configuration, environment files and session archives out of Git. `.gitignore` and the export checker cover common mistakes; they cannot determine whether arbitrary prose or source code is confidential.

## Workflow

1. The lead assigns a task with an owner, a different reviewer, dependencies, outputs and acceptance checks.
2. The owner works in its private copy and publishes explicit files. Publication verifies hashes, rejects missing files and detects conflicting edits.
3. For snapshot verification, the owner publishes executable inputs, runs `team_verify`, copies generated results into its workspace, writes the report and publishes them.
4. `team_evidence_catalog` retrieves usable receipts. `team_submission_contract` supplies exact criteria. An explicit ready submission either creates a verified review handoff or returns a rejection.
5. The reviewer inspects the exact submitted version and supplies independent evidence before approval. Completion requires the lead's final delivery after required reviews.

Declared Markdown reports and generated JSON/text/CSV results are excluded from snapshot execution inputs. Report-only changes retain successful evidence; source or declared policy changes require a new verification. Generated result files must match the retained execution output. Other files remain conservatively included in the source manifest.

These checks establish provenance and workflow consistency. They do not prove that a test covers the requirements or that an agent's interpretation is correct. See [ARCHITECTURE.md](ARCHITECTURE.md).

## Development

```bash
python3 -m unittest discover -s tests -v
python3 scripts/smoke_runtime.py
python3 scripts/check_export.py --staged
```

The smoke check starts a real Pi RPC process and loads the extension without requesting model inference. The export check reads the staged Git blobs; the default mode checks tracked working files. CI runs these checks without model credentials. See [VALIDATION.md](VALIDATION.md) for the validation scope.
