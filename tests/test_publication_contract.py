import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from store import Store,manifest,revision,digest
import publication


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Store(Path(self.tmp.name));self.r=self.s.create('Publish verified files');self.d=self.s.directory(self.r)
        self.s.assign(self.r,'lead',{'task_id':'a','owner':'worker-3','reviewer':'worker-4','description':'Build','acceptance':'Verified'})
        self.w=self.d/'agents/worker-3/workspace'
    def tearDown(self):self.s.db.close();self.tmp.cleanup()
    def put(self,name,text='one'):
        p=self.w/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text);return p
    def publish(self,files,**kw):return self.s.publish(self.r,'worker-3',{'task_id':'a','files':files,**kw})
    def count(self):return self.s.db.execute("SELECT count(*) FROM events WHERE kind='publication_committed'").fetchone()[0]
    def test_missing_file_never_deletes_existing_file(self):
        p=self.put('code.py');self.publish(['code.py']);p.unlink();before=self.count()
        with self.assertRaisesRegex(ValueError,'missing files are never deletions'):self.publish(['code.py'])
        self.assertEqual((self.d/'project/code.py').read_text(),'one');self.assertEqual(self.count(),before)
    def test_mixed_valid_missing_is_atomic(self):
        self.put('valid.py');before=manifest(self.d/'project');base=(self.d/'agents/worker-3/base.json').read_bytes()
        with self.assertRaises(ValueError):self.publish(['valid.py','absent.py'])
        self.assertEqual(before,manifest(self.d/'project'));self.assertEqual(base,(self.d/'agents/worker-3/base.json').read_bytes())
        self.assertEqual(self.count(),0);self.assertFalse((self.d/'publication.json').exists());self.assertIsNone(self.s.task(self.r,'a')['version'])
    def test_explicit_delete_requires_current_hash(self):
        self.put('old.py');first=self.publish(['old.py'])
        with self.assertRaises(ValueError):self.publish([],deletions=[{'path':'old.py','sha256':'0'*64}])
        result=self.publish([],deletions=[{'path':'old.py','sha256':digest(b'one')}])
        self.assertFalse((self.d/'project/old.py').exists());self.assertTrue((self.d/'versions'/first['version']/'old.py').exists())
        self.assertEqual(result['changes']['deleted'],['old.py']);self.assertEqual(result['artifacts'],[])
    def test_receipt_matches_immutable_bytes_and_changes(self):
        self.put('code.py');first=self.publish(['code.py']);self.assertEqual(first['changes']['added'],['code.py'])
        self.put('code.py','two');result=self.publish(['code.py']);self.assertEqual(result['changes']['modified'],['code.py'])
        e=result['artifacts'][0];self.assertEqual(e['sha256'],digest(b'two'));self.assertEqual(e['size_bytes'],3)
        self.assertEqual((self.d/'versions'/result['version']/e['path']).read_bytes(),b'two')
        persisted=json.loads((self.d/'publications'/(result['transaction_id']+'.json')).read_text());self.assertEqual(result,persisted)
    def test_changed_copy_hash_rejected_before_project_swap(self):
        self.put('code.py');real=publication.shutil.copy2
        def bad(src,dst,*a,**kw):
            answer=real(src,dst,*a,**kw)
            if Path(src)==self.w/'code.py':Path(dst).write_text('corrupt')
            return answer
        with patch.object(publication.shutil,'copy2',side_effect=bad):
            with self.assertRaisesRegex(ValueError,'hash/size'):self.publish(['code.py'])
        self.assertEqual(manifest(self.d/'project'),{});self.assertEqual(self.count(),0)
    def test_excluded_file_is_rejected_before_mutation(self):
        self.put('.gitignore','ignored.py\n');self.publish(['.gitignore']);self.put('ignored.py')
        with self.assertRaises(ValueError):self.publish(['ignored.py'])
        self.assertFalse((self.d/'project/ignored.py').exists())
    def test_ignore_change_cannot_silently_delete_an_unlisted_file(self):
        self.put('kept.py');self.publish(['kept.py']);self.put('.gitignore','kept.py\n')
        with self.assertRaisesRegex(ValueError,'unrequested changes'):self.publish(['.gitignore'])
        self.assertTrue((self.d/'project/kept.py').exists());self.assertFalse((self.d/'project/.gitignore').exists())
    def test_final_delivery_rechecks_required_files(self):
        self.put('code.py');v=self.publish(['code.py'])['version'];t=self.s.task(self.r,'a');t['required_outputs']=['code.py'];t['state']='done';self.s.save_task(self.r,t)
        (self.d/'project/code.py').unlink();new=self.s.seal(self.r)
        with self.assertRaisesRegex(ValueError,'Required deliverable'):self.s.board_tool(self.r,'lead','team_finish',{'version':new,'summary':'ready','validation':'checked'})
    def test_symlink_and_directory_rejected(self):
        self.put('real.py');(self.w/'alias.py').symlink_to('real.py');(self.w/'folder').mkdir()
        for p in ('alias.py','folder'):
            with self.assertRaises(ValueError):self.publish([p])
    def test_recovery_after_receipt_event_is_idempotent(self):
        self.put('code.py');original=self.s.event
        def fail(*args):
            value=original(*args)
            if args[2]=='publication_committed':raise OSError('crash after durable event')
            return value
        with patch.object(self.s,'event',side_effect=fail):
            with self.assertRaises(OSError):self.publish(['code.py'])
        receipt=self.s.repair_publication(self.r);self.assertTrue(receipt['verified']);self.assertEqual(self.count(),1)
        self.assertEqual(len(self.s.task(self.r,'a')['publication_receipts']),1)
    def test_corrupt_existing_snapshot_never_claims_success(self):
        self.put('code.py');v=revision(manifest(self.w));target=self.d/'versions'/v;target.mkdir();(target/'code.py').write_text('bad')
        with self.assertRaises(ValueError):self.publish(['code.py'])
        self.assertEqual(self.count(),0);self.assertTrue((self.d/'publication.json').exists())
    def test_missing_required_output_blocks_ready_and_approval(self):
        self.put('report.md');v=self.publish(['report.md'])['version'];t=self.s.task(self.r,'a');t['required_outputs']=['report.md','code.py'];self.s.save_task(self.r,t)
        with self.assertRaisesRegex(ValueError,'Required deliverable'):self.s.board_tool(self.r,'worker-3','team_report',{'task_id':'a','summary':'ready','version':v})
        self.assertEqual(self.s.task(self.r,'a')['state'],'assigned')
        t['state']='review';self.s.save_task(self.r,t)
        with self.assertRaisesRegex(ValueError,'Required deliverable'):self.s.board_tool(self.r,'worker-4','team_review',{'task_id':'a','version':v,'approved':True,'summary':'approved'})
    def test_ready_binds_output_hash_and_rejects_later_tampering(self):
        self.put('code.py');v=self.publish(['code.py'])['version'];t=self.s.task(self.r,'a');t['required_outputs']=['code.py'];self.s.save_task(self.r,t)
        t=self.s.board_tool(self.r,'worker-3','team_report',{'task_id':'a','summary':'ready','version':v});self.assertEqual(t['submitted_outputs'][0]['sha256'],digest(b'one'))
        (self.d/'versions'/v/'code.py').write_text('tampered')
        with self.assertRaisesRegex(ValueError,'hashes changed'):self.s.board_tool(self.r,'worker-4','team_review',{'task_id':'a','version':v,'approved':True,'summary':'approved'})
    def test_legacy_journal_does_not_infer_deletion(self):
        (self.d/'publication.json').write_text(json.dumps({'files':['missing.py']}))
        with self.assertRaisesRegex(ValueError,'Legacy publication'):self.s.repair_publication(self.r)
        self.assertEqual(self.count(),0)
