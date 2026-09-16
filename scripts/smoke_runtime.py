"""Start an actual Pi RPC process and load the extension without requesting inference."""
import sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from configuration import LEAD
from team import configure
from store import Store
from runtime import Runtime

def main():
 with tempfile.TemporaryDirectory(prefix='pi-runtime-check-') as tmp:
  root=Path(tmp);configure(root);s=Store(root);r=s.create('Installation check');events=[]
  rt=None
  try:
   rt=Runtime(s,r,LEAD,'http://127.0.0.1:1','installation-check-token',events.append)
   state=rt.rpc.request('get_state')
   assert state['model']['id']
   assert not any(e.get('type')=='extension_error' for e in events),events
   print('PASS: Pi RPC initialization, model selection and extension loading; no inference requested.')
  finally:
   if rt:rt.close()
   s.db.close()
if __name__=='__main__':main()
