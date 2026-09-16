"""User-supplied team configuration, pinned to each durable state directory."""
import copy
import json
import os
import re
from pathlib import Path

CORE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get('PI_TEAM_CONFIG', str(CORE / 'team.local.json'))).expanduser()
if not CONFIG_PATH.exists() and 'PI_TEAM_CONFIG' not in os.environ:
    CONFIG_PATH = CORE / 'examples/team.example.json'

def validate_config(raw):
    config = copy.deepcopy(raw)
    agents = config.get('agents', {})
    providers = config.get('providers', {})
    lead = config.get('lead')
    if not 2 <= len(agents) <= 100 or lead not in agents:
        raise ValueError('Configure 2-100 agents and a lead in the roster')
    for name, agent in agents.items():
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', name):
            raise ValueError('Agent IDs must be short lowercase names, not paths')
        if agent.get('role') != ('lead' if name == lead else 'worker'):
            raise ValueError('Exactly the configured lead has role=lead')
        provider = providers.get(agent.get('provider'))
        if not provider or agent.get('model') not in [m['id'] for m in provider.get('models', [])]:
            raise ValueError('Each agent needs a model declared in its provider configuration')
        if agent.get('thinking') not in ('off','minimal','low','medium','high','xhigh'):
            raise ValueError('Unsupported Pi thinking setting')
    for name, provider in providers.items():
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,39}',name):raise ValueError('Invalid provider ID')
        if 'apiKey' in provider:raise ValueError('Use api_key_env, not inline credentials')
        key = provider.get('api_key_env')
        if key and not re.fullmatch(r'[A-Z_][A-Z0-9_]*',key):raise ValueError('api_key_env must name an environment variable')
        if not isinstance(provider.get('baseUrl'),str) or not provider['baseUrl'].startswith(('http://','https://')):raise ValueError('Provider needs an HTTP(S) baseUrl')
        if type(config.get('concurrency',{}).get(name)) is not int or config['concurrency'][name] < 1:
            raise ValueError('Each provider needs a positive concurrency limit')
        overrides = provider.get('request_overrides',{})
        if not isinstance(overrides,dict) or set(overrides)-{'chat_template_kwargs','reasoning_effort','max_tokens','max_output_tokens'}:
            raise ValueError('Unsupported request override; use generation options only')
        for model in provider['models']:
            if type(model.get('contextWindow')) is not int or type(model.get('maxTokens')) is not int or not 0 < model['maxTokens'] < model['contextWindow']:
                raise ValueError('Each model needs 0 < maxTokens < contextWindow')
    context = config.get('context_window',32768)
    output = config.get('output_tokens',4096)
    compact = config.get('compaction',{'reserveTokens':8192,'keepRecentTokens':4096})
    if not all(type(n) is int and n > 0 for n in (context,output,compact['reserveTokens'],compact['keepRecentTokens'])):
        raise ValueError('Context and compaction limits must be positive integers')
    models=[m for p in providers.values() for m in p['models']]
    minimum=min(m['contextWindow'] for m in models)
    if not output <= compact['reserveTokens'] or compact['reserveTokens']+compact['keepRecentTokens'] >= min(context,minimum):
        raise ValueError('Compaction reserve must cover output and leave room within every model context')
    config['compaction']=compact
    return config

CONFIG = validate_config(json.loads(CONFIG_PATH.read_text()))
PROFILE = 'configured'
LEAD = CONFIG['lead']
TITLE = CONFIG.get('title','Pi Agent Team')
AGENTS = CONFIG['agents']
PROVIDERS = CONFIG['providers']
CONCURRENCY = CONFIG['concurrency']
OUTPUT_TOKENS = CONFIG.get('output_tokens',4096)
CONTEXT_WINDOW = CONFIG.get('context_window',32768)
DEFAULT_PORT = CONFIG.get('port',18890)
DEFAULT_STATE = CORE / 'state'
CREDENTIAL_ENV = {p['api_key_env'] for p in PROVIDERS.values() if p.get('api_key_env')}

def ensure_profile(state):
    state=Path(state);state.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=state/'team-profile.json'
    identity={'schema':1,'configuration':CONFIG}
    if path.exists():
        if json.loads(path.read_text()) != identity:
            raise ValueError('Team profile does not match this state directory; use the original configuration or new state')
    else:
        if (state/'team.db').exists():raise ValueError('Cannot adopt unlabelled existing state')
        with path.open('x') as f:json.dump(identity,f,indent=2);f.write('\n')
    return identity
