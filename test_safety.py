import ast
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
import delivery
import store

class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent/'runtime')
        self.old=store._DB
        store._DB=str(Path(self.tmp.name)/'queue.db')
        store.init_db()
    def tearDown(self):
        store._DB=self.old
        self.tmp.cleanup()
    def test_cloud_replay_does_not_requeue_done(self):
        self.assertTrue(store.enqueue('id','group','hello',time.time()+60))
        store.claim_next(); store.finish('id',True)
        self.assertFalse(store.enqueue('id','group','hello',time.time()+600))
        self.assertEqual(store.get_status('id'),'done')
        self.assertIsNone(store.claim_next())
    def test_restart_holds_ambiguous_send(self):
        store.enqueue('id','group','hello',time.time()+60)
        store.claim_next(); store.requeue_stuck()
        self.assertEqual(store.get_status('id'),'uncertain')
        self.assertIsNone(store.claim_next())
    def test_migration_preserves_old_queue(self):
        with sqlite3.connect(store._DB) as c:
            c.execute('DROP TABLE messages')
            c.execute("CREATE TABLE messages(msg_id TEXT PRIMARY KEY,grp TEXT,text TEXT,status TEXT, error TEXT,created_at TEXT,updated_at TEXT)")
            c.execute("INSERT INTO messages(msg_id,grp,text,status) VALUES('legacy','group','hello','sending')")
        c.close()
        store.init_db();store.requeue_stuck()
        self.assertEqual(store.get_status('legacy'),'uncertain')
    def test_invalid_message_rejected(self):
        with self.assertRaises(ValueError): store.enqueue('', 'group','hello',1)

class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.base=Path(__file__).parent
        self.row={'msg_id':'test','grp':'group','text':'simulated','expires_at':time.time()+60}
        self.cfg={'wechat':{'groups':['group']},'delivery':{'send_timeout_sec':2}}
    def run_mode(self,mode):
        return delivery.deliver(self.row,self.cfg,self.base,test_mode=mode)[0]
    def test_success(self): self.assertEqual(self.run_mode('success'),'done')
    def test_window_unavailable_is_deferred(self): self.assertEqual(self.run_mode('deferred'),'pending')
    def test_ambiguous_is_held(self): self.assertEqual(self.run_mode('ambiguous'),'uncertain')
    def test_crash_is_held(self): self.assertEqual(self.run_mode('crash'),'uncertain')
    def test_timeout_kills_process_and_holds(self):
        self.cfg['delivery']['send_timeout_sec']=0.2
        start=time.monotonic()
        self.assertEqual(self.run_mode('timeout'),'uncertain')
        self.assertLess(time.monotonic()-start,5)
    def test_expired_not_dispatched(self):
        self.row['expires_at']=time.time()-1
        self.assertEqual(self.run_mode('success'),'expired')
    def test_legacy_without_expiry_not_dispatched(self):
        self.row['expires_at']=None
        self.assertEqual(self.run_mode('success'),'expired')
    def test_unapproved_group_not_dispatched(self):
        self.row['grp']='other'
        self.assertEqual(self.run_mode('success'),'failed')

if __name__=='__main__':
    (Path(__file__).parent/'runtime').mkdir(exist_ok=True)
    unittest.main(verbosity=2)
