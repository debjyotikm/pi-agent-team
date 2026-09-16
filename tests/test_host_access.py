import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from host_executor import HostExecutor
from project_files import files, inspect
from runtime import project_instructions
from server import Controller
from store import Store, manifest


def git(root,*args):
    return subprocess.check_output(['git','-C',str(root),*args],text=True,stderr=subprocess.DEVNULL).strip()


class HostAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()

    def repository(self):
        p=self.root/'source';p.mkdir()
        git(p,'init','--quiet');git(p,'config','user.name','Test');git(p,'config','user.email','test@example.invalid')
        (p/'.gitignore').write_text('artifacts/\n.venv*/\ncache/\n')
        (p/'code.py').write_text('VALUE = 1\n');(p/'delete.py').write_text('DELETE = 1\n')
        git(p,'add','.');git(p,'commit','--quiet','-m','baseline')
        (p/'code.py').write_text('VALUE = 2\n');(p/'delete.py').unlink();(p/'new.py').write_text('NEW = 1\n')
        (p/'artifacts').mkdir();(p/'artifacts/data.bin').write_bytes(b'large corpus')
        (p/'.venv-extra').mkdir();(p/'.venv-extra/package.py').write_text('package')
        return p

    def test_git_ignore_selection_keeps_dirty_source(self):
        p=self.repository();selected=files(p)
        self.assertIn('code.py',selected);self.assertIn('new.py',selected)
        self.assertNotIn('delete.py',selected);self.assertNotIn('artifacts/data.bin',selected)
        self.assertNotIn('.venv-extra/package.py',selected)

    def test_nested_ignore_in_non_git_directory(self):
        p=self.root/'plain';p.mkdir();(p/'.gitignore').write_text('artifacts/\n')
        (p/'a.py').write_text('a');(p/'artifacts').mkdir();(p/'artifacts/big').write_text('ignored')
        self.assertEqual(files(p),['.gitignore','a.py'])

    def test_separate_git_working_copies_preserve_history_and_source(self):
        p=self.repository();before=git(p,'status','--porcelain');head=git(p,'rev-parse','HEAD')
        s=Store(self.root/'state')
        try:
            r=s.create('Task placeholder for software test',p)
            d=s.directory(r);self.assertEqual(s.project_info(r)['execution_mode'],'host')
            for agent in ('lead','worker-1','worker-3'):
                w=d/'agents'/agent/'workspace'
                self.assertEqual(git(w,'rev-parse','HEAD'),head)
                self.assertIn(agent,git(w,'branch','--show-current'))
                self.assertEqual((w/'code.py').read_text(),'VALUE = 2\n')
                self.assertFalse((w/'delete.py').exists());self.assertFalse((w/'artifacts').exists())
            self.assertEqual(git(p,'status','--porcelain'),before)
            self.assertEqual(git(p,'branch','--show-current'),'master')
        finally:s.db.close()

    def test_git_survives_publication_but_is_excluded_from_snapshots(self):
        p=self.repository();s=Store(self.root/'state')
        try:
            r=s.create('Task placeholder',p);d=s.directory(r)
            s.assign(r,'lead',{'task_id':'a','owner':'worker-1','reviewer':'lead','description':'edit','acceptance':'correct'})
            (d/'agents/worker-1/workspace/code.py').write_text('VALUE = 3\n')
            v=s.publish(r,'worker-1',{'task_id':'a','files':['code.py']})['version']
            self.assertEqual(git(d/'project','rev-parse','HEAD'),git(p,'rev-parse','HEAD'))
            self.assertFalse((d/'versions'/v/'.git').exists())
            self.assertEqual((p/'code.py').read_text(),'VALUE = 2\n')
        finally:s.db.close()

    def test_absolute_read_write_and_external_cwd(self):
        c=Controller(self.root/'state','http://127.0.0.1:1');s=c.store
        try:
            r=s.create('Host access unit check');external=self.root/'outside.txt'
            c.execute(r,'lead','write',{'path':str(external),'content':'host access'})
            self.assertEqual(c.execute(r,'lead','read',{'path':str(external)})['content'],'host access')
            w=s.directory(r)/'agents/lead/workspace';h=HostExecutor(w)
            result=h.execute('pwd; cat outside.txt',5,cwd=self.root)
            self.assertEqual(result['exit_code'],0);self.assertIn('host access',result['stdout'])
            h.close()
        finally:s.db.close()

    def test_environment_imports_working_copy_before_shared_install(self):
        p=self.root/'source';p.mkdir();w=self.root/'workspace';w.mkdir();(w/'src').mkdir()
        (w/'src/local_marker.py').write_text('VALUE = "working-copy"')
        h=HostExecutor(w,p)
        try:
            r=h.execute("python3 -c 'import local_marker; print(local_marker.VALUE)'",5)
            self.assertEqual(r['exit_code'],0,r);self.assertEqual(r['stdout'].strip(),'working-copy')
        finally:h.close()

    def test_cancel_terminates_only_owned_process_group(self):
        w=self.root/'workspace';w.mkdir();h=HostExecutor(w)
        unrelated=subprocess.Popen(['sleep','30'])
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(h.execute,'sleep 30 & wait',60)
                deadline=time.monotonic()+5
                while not h.children and time.monotonic()<deadline:time.sleep(.02)
                self.assertTrue(h.children);h.interrupt()
                self.assertNotEqual(future.result(timeout=5)['exit_code'],0)
                self.assertIsNone(unrelated.poll())
        finally:unrelated.terminate();unrelated.wait();h.close()

    def test_timeout_leaves_no_active_command(self):
        w=self.root/'workspace';w.mkdir();h=HostExecutor(w)
        try:
            r=h.execute('sleep 30',.1)
            self.assertEqual(r['exit_code'],124);self.assertTrue(r['timed_out']);self.assertFalse(h.children)
        finally:h.close()

    def test_project_and_ancestor_instructions_are_loaded(self):
        p=self.root/'source';p.mkdir();w=self.root/'workspace';w.mkdir()
        (self.root/'AGENTS.md').write_text('ancestor rule');(w/'AGENTS.md').write_text('project rule')
        text=project_instructions(p,w)
        self.assertIn('ancestor rule',text);self.assertIn('project rule',text)

if __name__=='__main__':unittest.main()
