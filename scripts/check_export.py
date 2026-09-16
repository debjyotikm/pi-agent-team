"""Check staged/tracked source for accidentally included local runtime material."""
import argparse,ipaddress,re,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
ROOT_FILES={'.gitignore','.dockerignore','Dockerfile','README.md','ARCHITECTURE.md','VALIDATION.md','package.json','package-lock.json','configuration.py','team.py','runtime.py','server.py','store.py','acceptance.py','completion_health.py','extension.ts','handoffs.py','host_executor.py','index.html','obligations.py','progress.py','project_files.py','publication.py','resilience.py','snapshot_evidence.py','workflow.py'}
OTHER_FILES={'.github/workflows/ci.yml','examples/team.example.json','scripts/check_export.py','scripts/smoke_runtime.py'}
PATTERNS={
 'private key':r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
 'cloud access key':r'\bAKIA[A-Z0-9]{16}\b',
 'provider token':r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{24,})',
 'home directory':r'(?:/home|/Users)/[a-zA-Z0-9_.-]+/',
 'authenticated URL':r'https?://[^\s/@]+:[^\s/@]+@',
 'hardware identity':r'\bGPU-[a-fA-F0-9]{8}-[a-fA-F0-9-]{20,}',
}
def inspect(name,data,mode='100644'):
 errors=[]
 if name not in ROOT_FILES|OTHER_FILES and not re.fullmatch(r'tests/test_[a-z_]+\.py',name):errors.append('file is outside the source allowlist')
 if mode not in ('100644','100755'):errors.append('symlinks and submodules are not allowed')
 try:text=data.decode('utf-8')
 except UnicodeDecodeError:return errors+['binary content is not allowed']
 for label,pattern in PATTERNS.items():
  if re.search(pattern,text):errors.append(label)
 for match in re.finditer(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])',text):
  try:ip=ipaddress.ip_address(match.group())
  except ValueError:continue
  if not ip.is_loopback and (ip.is_private or ip in ipaddress.ip_network((0x64400000,10))):errors.append('non-loopback private network address')
 return sorted(set(errors))
def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--staged',action='store_true');args=parser.parse_args()
 entries=subprocess.check_output(['git','ls-files','--stage','-z'],cwd=ROOT).split(b'\0');failures=[];count=0
 for entry in entries:
  if not entry:continue
  info,name=entry.split(b'\t',1);mode,oid,stage=info.decode().split();name=name.decode();count+=1
  if stage!='0':failures.append((name,['unmerged index entry']));continue
  data=subprocess.check_output(['git','cat-file','blob',oid],cwd=ROOT) if args.staged else (ROOT/name).read_bytes()
  issues=inspect(name,data,mode)
  if issues:failures.append((name,issues))
 if not count:raise SystemExit('No tracked files; stage the intended export first')
 for name,issues in failures:print(name+': '+', '.join(issues),file=sys.stderr)
 if failures:raise SystemExit(1)
 print(f'PASS: {count} source files checked; no disallowed files or matched private-data patterns.')
if __name__=='__main__':main()
