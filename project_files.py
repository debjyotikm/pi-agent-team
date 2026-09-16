"""Git-aware source selection. Data and environments stay at their host locations."""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

EXCLUDED = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', '.pytest_cache', '.pi', '.DS_Store'}


def git_root(root):
    result = subprocess.run(['git','-C',str(root),'rev-parse','--show-toplevel'],capture_output=True,text=True)
    return result.returncode == 0 and Path(result.stdout.strip()).resolve() == Path(root).resolve()


def files(root):
    root = Path(root).resolve()
    if git_root(root):
        command=['git','-C',str(root),'ls-files','-z','--cached','--others','--exclude-standard']
        result=subprocess.run(command,capture_output=True,check=True)
        names=result.stdout.decode('utf-8',errors='surrogateescape').split('\0')
    else:
        # A private, empty index also applies nested .gitignore rules in a non-Git source directory.
        with tempfile.TemporaryDirectory(prefix='pi-team-index-') as tmp:
            gitdir=Path(tmp)/'git'
            subprocess.run(['git','init','--bare','--quiet',str(gitdir)],check=True,capture_output=True)
            result=subprocess.run(['git','--git-dir='+str(gitdir),'--work-tree='+str(root),
                                   'ls-files','-z','--others','--exclude-standard'],cwd=root,capture_output=True,check=True)
            names=result.stdout.decode('utf-8',errors='surrogateescape').split('\0')
    selected=[]
    for name in sorted(set(names)):
        if not name:continue
        parts=Path(name).parts
        if any(x in EXCLUDED or x.startswith('.venv-') for x in parts):continue
        if Path(name).name=='.env' or (Path(name).name.startswith('.env.') and Path(name).name!='.env.example'):continue
        path=root/name
        # Never dereference symlinks while selecting or copying source. They remain accessible on the host.
        if path.is_file() and not path.is_symlink() and not any((root/Path(*parts[:i])).is_symlink() for i in range(1,len(parts))):
            selected.append(name)
    return selected


def copy_source(source, destination, keep_git=False):
    source=Path(source);destination=Path(destination);destination.mkdir(parents=True,exist_ok=True)
    for name in files(source):
        dst=destination/name;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source/name,dst)
    if keep_git and (source/'.git').exists():
        # A source working copy created by clone has independent refs/index and a real .git directory.
        shutil.copytree(source/'.git',destination/'.git',dirs_exist_ok=True)


def working_copy(source, destination, branch):
    source=Path(source);destination=Path(destination)
    if git_root(source):
        head=subprocess.run(['git','-C',str(source),'rev-parse','--verify','HEAD'],capture_output=True)
        if head.returncode==0:
            subprocess.run(['git','clone','--local','--no-checkout','--quiet',str(source),str(destination)],check=True,capture_output=True)
            subprocess.run(['git','-C',str(destination),'checkout','--quiet','-b',branch],check=True,capture_output=True)
            # Reproduce uncommitted changes and deletions without changing the source checkout.
            selected=set(files(source))
            for name in files(destination):
                if name not in selected:(destination/name).unlink()
            copy_source(source,destination)
            return
    copy_source(source,destination)


def inspect(source):
    root=Path(source).expanduser().resolve();selected=files(root)
    head=subprocess.run(['git','-C',str(root),'rev-parse','--verify','HEAD'],capture_output=True,text=True)
    return {'source':str(root),'is_git':git_root(root),'source_commit':head.stdout.strip() if head.returncode==0 else None,
            'selected_files':len(selected),'selected_bytes':sum((root/n).stat().st_size for n in selected),
            'environment':str(root/'.venv') if (root/'.venv/bin/python').exists() else None,
            'selection':'Git tracked files plus non-ignored untracked files; runtime dependencies and symlinks excluded from copies'}
