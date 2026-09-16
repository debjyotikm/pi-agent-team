import copy,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from configuration import CONFIG,validate_config,ensure_profile
from team import configure

class ConfigurationTests(unittest.TestCase):
 def test_configure_writes_env_reference_not_secret(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);configure(p);text=(p/'pi/models.json').read_text()
   self.assertEqual(json.loads(text)['providers']['local']['apiKey'],'MODEL_API_KEY')
   self.assertNotIn('api_key_env',text)
 def test_profile_change_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);ensure_profile(p);data=json.loads((p/'team-profile.json').read_text());data['configuration']['agents']['lead']['model']='different';(p/'team-profile.json').write_text(json.dumps(data))
   with self.assertRaisesRegex(ValueError,'profile does not match'):ensure_profile(p)
 def test_invalid_identity_model_secret_and_limits_rejected(self):
  for mutate in [lambda c:c['agents'].update({'../bad':c['agents']['lead']}),lambda c:c['agents']['lead'].update(model='missing'),lambda c:c['providers']['local'].update(apiKey='DO_NOT_STORE_CREDENTIALS'),lambda c:c['concurrency'].update(local=0),lambda c:c['compaction'].update(reserveTokens=32768)]:
   c=copy.deepcopy(CONFIG);mutate(c)
   with self.assertRaises(ValueError):validate_config(c)
 def test_custom_roster_loads_in_clean_process(self):
  c=copy.deepcopy(CONFIG);c['agents']={k:v for k,v in c['agents'].items() if k in ('lead','worker-1')}
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'team.json';p.write_text(json.dumps(c))
   out=subprocess.run([sys.executable,'-c','from configuration import AGENTS; assert len(AGENTS)==2'],env={**os.environ,'PI_TEAM_CONFIG':str(p)},capture_output=True,text=True)
   self.assertEqual(out.returncode,0,out.stderr)
