"""Exercise actual actor loops with deterministic Pi process doubles."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from server import Controller
from store import AGENTS


class SchedulerTests(unittest.TestCase):
    def test_dispatch_review_completion_and_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=Controller(Path(tmp),'http://127.0.0.1:1');s=c.store;r=s.create('Create two independently reviewed files.')
            calls=[];gate=threading.Barrier(2);worker_started=[];test=self
            class FakeRPC:
                def prompt(self,text):
                    calls.append((self.agent,text))
                    if self.agent=='lead':
                        if not s.tasks(r):
                            for i in (1,2):
                                s.board_tool(r,'lead','team_assign',{'task_id':str(i),'owner':f'worker-{i}','reviewer':f'worker-{i+2}','description':'Write a file','acceptance':'File contains its task ID'})
                        elif all(t['state']=='done' for t in s.tasks(r)):
                            s.board_tool(r,'lead','team_finish',{'version':s.status(r)['version'],'summary':'Files ready','validation':'Both independently reviewed'})
                    elif self.agent in ('worker-1','worker-2'):
                        task=next(t for t in s.tasks(r) if t['owner']==self.agent)
                        worker_started.append(self.agent);gate.wait(timeout=5)
                        workspace=s.directory(r)/'agents'/self.agent/'workspace';(workspace/(task['id']+'.txt')).write_text(task['id'])
                        s.board_tool(r,self.agent,'team_publish',{'task_id':task['id'],'files':[task['id']+'.txt']})
                        s.board_tool(r,self.agent,'team_report',{'task_id':task['id'],'summary':'Created file'})
                    else:
                        task=next(t for t in s.tasks(r) if t['reviewer']==self.agent)
                        test.assertEqual((s.directory(r)/'versions'/task['version']/(task['id']+'.txt')).read_text(),task['id'])
                        s.board_tool(r,self.agent,'team_review',{'task_id':task['id'],'version':task['version'],'approved':True,'summary':'Read immutable artifact'})
            class FakeRuntime:
                def __init__(self,a):self.rpc=FakeRPC();self.rpc.agent=a
                def close(self):pass
            def runtime(run,agent):
                key=(run,agent)
                if key not in c.runtimes:c.runtimes[key]=FakeRuntime(agent)
                return c.runtimes[key]
            c.ensure_runtime=runtime
            try:
                c.start(r)
                deadline=time.monotonic()+8
                while s.run(r)['state']=='running' and time.monotonic()<deadline:time.sleep(.03)
                self.assertEqual(s.run(r)['state'],'complete',c.activity)
                self.assertEqual(set(worker_started),{'worker-1','worker-2'})
                count=len(calls);time.sleep(.7);self.assertEqual(len(calls),count)
            finally:
                c.close()
                for th in c.threads.values():th.join(timeout=3)
                s.db.close()

    def test_waiting_actor_makes_no_prompt_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=Controller(Path(tmp),'http://127.0.0.1:1');r=c.store.create('Wait for an assignment')
            class FakeRuntime:
                def __init__(self):self.rpc=self
                def prompt(self,text):raise AssertionError('Idle worker must not call a model')
                def close(self):pass
            c.ensure_runtime=lambda r,a:FakeRuntime()
            th=threading.Thread(target=c.actor,args=(r,'worker-3'));th.start()
            try:
                time.sleep(.7);self.assertEqual(c.activity[(r,'worker-3')]['state'],'waiting')
                self.assertEqual(c.failures.get((r,'worker-3'),0),0)
            finally:c.close();th.join(timeout=3);c.store.db.close()

if __name__=='__main__':unittest.main()
