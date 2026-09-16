#!/usr/bin/env python3
"""Command line for the configured Pi team."""
import argparse
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from configuration import AGENTS, LEAD, TITLE, DEFAULT_STATE, DEFAULT_PORT, CONTEXT_WINDOW, OUTPUT_TOKENS, CONFIG, PROVIDERS, CREDENTIAL_ENV, ensure_profile

ROOT = Path(__file__).resolve().parent


def configure(state):
    state.mkdir(parents=True, exist_ok=True, mode=0o700); state.chmod(0o700)
    (state/'strict-workflow.json').write_text(json.dumps({'evidence_required':True})+'\n')
    pi = state / 'pi'; pi.mkdir(exist_ok=True)
    ensure_profile(state)
    providers = json.loads(json.dumps(PROVIDERS))
    for provider in providers.values():
        key = provider.pop('api_key_env', None)
        provider.pop('request_overrides', None)
        provider['apiKey'] = key or 'local'
    (pi / 'models.json').write_text(json.dumps({'providers':providers},indent=2)+'\n')
    (pi / 'settings.json').write_text(json.dumps({'compaction':{'enabled':True,**CONFIG['compaction']},
        'retry':{'enabled':True,'maxRetries':3},'transport':'sse','quietStartup':True})+'\n')


def request(state, port, path, data=None):
    headers = {}
    if data is not None:
        headers = {'Authorization': 'Bearer ' + (state / 'admin.token').read_text().strip(), 'Content-Type': 'application/json'}
    req = urllib.request.Request(f'http://127.0.0.1:{port}' + path, data=None if data is None else json.dumps(data).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=90) as response: return json.load(response)
    except urllib.error.HTTPError as exc: raise RuntimeError(exc.read().decode()) from None


def main():
    p = argparse.ArgumentParser(description=TITLE)
    p.add_argument('--state', type=Path, default=DEFAULT_STATE); p.add_argument('--port', type=int, default=DEFAULT_PORT)
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('configure'); sub.add_parser('serve'); sub.add_parser('status'); sub.add_parser('doctor')
    s = sub.add_parser('submit'); s.add_argument('prompt', nargs='?'); s.add_argument('--prompt-file', type=Path); s.add_argument('--project', type=Path); s.add_argument('--execution-mode', choices=('host','container'), default='host')
    s = sub.add_parser('inspect-project'); s.add_argument('--project', type=Path, required=True)
    for name in ('pause', 'resume'):
        s = sub.add_parser(name); s.add_argument('run')
    s = sub.add_parser('message'); s.add_argument('run'); s.add_argument('text'); s.add_argument('--recipient', default=LEAD)
    s = sub.add_parser('retry'); s.add_argument('run'); s.add_argument('agent')
    args = p.parse_args(); state = args.state.resolve()
    if args.command == 'inspect-project':
        from project_files import inspect
        print(json.dumps(inspect(args.project), indent=2)); return
    if args.command == 'configure': configure(state); print('Configured ' + TITLE + '.'); return
    if args.command == 'serve':
        import fcntl
        from server import serve
        state.mkdir(parents=True, exist_ok=True)
        lock = (state / 'controller.lock').open('w')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock.write(str(os.getpid())); lock.flush()
        serve(state, args.port); return
    if args.command == 'doctor':
        from runtime import PI
        result = {'pi': subprocess.check_output(['node', str(PI), '--version'], text=True).strip()}
        checks = {'pi_version': result['pi']=='0.85.1'}
        checks['credential_environment'] = all(os.environ.get(k) for k in CREDENTIAL_ENV)
        result['providers'] = {}
        for name, provider in PROVIDERS.items():
            headers = {}
            key = provider.get('api_key_env')
            if key and os.environ.get(key): headers['Authorization']='Bearer '+os.environ[key]
            try:
                req=urllib.request.Request(provider['baseUrl'].rstrip('/')+'/models',headers=headers)
                with urllib.request.urlopen(req,timeout=10) as resp: available=[m['id'] for m in json.load(resp)['data']]
                result['providers'][name]={'available_models':available}
                checks[name] = all(a['model'] in available for a in AGENTS.values() if a['provider']==name)
            except Exception:
                result['providers'][name]={'error':'Model discovery failed; check endpoint, credentials and provider support for GET /models'}
                checks[name]=False
        result['worker_image'] = bool(__import__('shutil').which('docker')) and subprocess.run(['docker','image','inspect','pi-team-worker:0.1'],capture_output=True).returncode==0
        checks['host_python'] = __import__('sys').version_info >= (3,12)
        result['execution_mode'] = 'host'
        result['team'] = TITLE
        result['lead'] = LEAD
        result['roster'] = AGENTS
        result['context_window'] = CONTEXT_WINDOW
        result['host_python'] = __import__('sys').version.split()[0]
        result['checks'] = checks; result['ready'] = all(checks.values())
        print(json.dumps(result, indent=2))
        if not result['ready']: raise SystemExit(1)
        return
    if args.command == 'status': result = request(state, args.port, '/api/status')
    elif args.command == 'submit':
        prompt = args.prompt_file.read_text() if args.prompt_file else args.prompt
        if not prompt: p.error('Supply a prompt or --prompt-file')
        result = request(state, args.port, '/api/submit', {'prompt': prompt, 'project': str(args.project.resolve()) if args.project else None, 'execution_mode': args.execution_mode})
    else:
        data = {'run': args.run}
        if args.command == 'message': data.update(text=args.text, recipient=args.recipient)
        if args.command == 'retry': data['agent'] = args.agent
        result = request(state, args.port, '/api/' + args.command, data)
    print(json.dumps(result, indent=2))


if __name__ == '__main__': main()
