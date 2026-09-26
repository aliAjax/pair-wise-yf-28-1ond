import tempfile
import unittest
from collections import Counter
from pathlib import Path

from app import BusinessError, RandomizationStore


class RandomizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RandomizationStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.trial = self.store.create_trial(
            "coord", "多中心降压研究", "v1.0", ["A", "B"], ["risk"], 4, "seed-2026-001"
        )
        self.store.start_trial("coord", self.trial["id"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_stratified_block_randomization_and_two_person_unblinding(self):
        participants = [
            self.store.enroll("site1", self.trial["id"], f"S001-{i:03d}", {"risk": "low"})
            for i in range(1, 5)
        ]
        self.assertNotIn("arm", participants[0])
        with self.store.connect() as conn:
            arms = [r["arm"] for r in conn.execute(
                "SELECT a.arm FROM allocations a JOIN participants p ON p.allocation_id=a.id WHERE p.trial_id=? ORDER BY p.id",
                (self.trial["id"],),
            ).fetchall()]
        self.assertEqual(Counter(arms), Counter({"A": 2, "B": 2}))
        request = self.store.request_unblinding("site1", participants[0]["id"], "受试者发生严重不良事件需要紧急处理")
        first = self.store.approve_unblinding("monitor1", request["id"])
        self.assertEqual(first["status"], "pending")
        with self.assertRaises(BusinessError) as ctx:
            self.store.approve_unblinding("monitor1", request["id"])
        self.assertEqual(ctx.exception.code, "distinct_approver_required")
        second = self.store.approve_unblinding("monitor2", request["id"])
        self.assertEqual(second["status"], "approved")
        self.assertIn(second["arm"], {"A", "B"})

    def test_idempotent_enrollment_site_isolation_and_protocol_lock(self):
        first = self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "high"})
        again = self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "high"})
        self.assertEqual(first["id"], again["id"])
        self.assertTrue(again["idempotent"])
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0], 1)
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_participant("site2", first["id"])
        self.assertEqual(ctx.exception.code, "site_isolation")
        with self.assertRaises(BusinessError) as ctx:
            self.store.update_protocol("coord", self.trial["id"], "v2")
        self.assertEqual(ctx.exception.code, "protocol_locked")

    def test_pause_resume_blocks_enrollment_and_sequence_continues(self):
        tid = self.trial["id"]
        p1 = self.store.enroll("site1", tid, "S001-001", {"risk": "low"})
        with self.assertRaises(BusinessError) as ctx:
            self.store.pause_enrollment("site1", tid, "中心越权暂停")
        self.assertEqual(ctx.exception.code, "forbidden")
        with self.assertRaises(BusinessError) as ctx:
            self.store.pause_enrollment("coord", tid, " ")
        self.assertEqual(ctx.exception.code, "reason_required")
        paused = self.store.pause_enrollment("coord", tid, "出现非预期严重不良反应")
        self.assertEqual(paused["enrollment"], "paused")
        with self.assertRaises(BusinessError) as ctx:
            self.store.pause_enrollment("coord", tid, "重复暂停")
        self.assertEqual(ctx.exception.code, "already_paused")
        with self.assertRaises(BusinessError) as ctx:
            self.store.enroll("site1", tid, "S001-002", {"risk": "low"})
        self.assertEqual(ctx.exception.code, "enrollment_paused")
        self.assertIn("非预期严重不良反应", ctx.exception.message)
        # 暂停期间已入组受试者仍可申请揭盲、登记结局
        request = self.store.request_unblinding("site1", p1["id"], "受试者发生严重不良事件需要紧急处理")
        self.assertEqual(request["status"], "pending")
        outcome = self.store.register_outcome("site1", p1["id"], "completed", "完成全部随访")
        self.assertEqual(outcome["status"], "completed")
        summary = self.store.trial_summary("coord", tid)
        self.assertEqual(summary["enrollment"], "paused")
        self.assertEqual(summary["active_hold"]["reason"], "出现非预期严重不良反应")
        with self.assertRaises(BusinessError) as ctx:
            self.store.resume_enrollment("coord", tid, "")
        self.assertEqual(ctx.exception.code, "note_required")
        resumed = self.store.resume_enrollment("coord", tid, "已完成整改并通过伦理审查")
        self.assertEqual(resumed["enrollment"], "enrolling")
        # 恢复后接着原有随机序列发放
        p2 = self.store.enroll("site1", tid, "S001-002", {"risk": "low"})
        with self.store.connect() as conn:
            seq = {
                row["id"]: row["sequence"]
                for row in conn.execute(
                    "SELECT p.id, a.sequence FROM participants p JOIN allocations a ON a.id=p.allocation_id WHERE p.trial_id=?",
                    (tid,),
                ).fetchall()
            }
        self.assertEqual(seq[p2["id"]], seq[p1["id"]] + 1)
        summary = self.store.trial_summary("coord", tid)
        self.assertEqual(summary["enrollment"], "enrolling")
        self.assertIsNone(summary["active_hold"])
        self.assertEqual([h["action"] for h in summary["holds"]], ["pause", "resume"])

    def test_outcome_registration_audit_and_no_random_number_recycle(self):
        tid = self.trial["id"]
        participants = [self.store.enroll("site1", tid, f"S001-{i:03d}", {"risk": "high"}) for i in range(1, 4)]
        with self.assertRaises(BusinessError) as ctx:
            self.store.register_outcome("site2", participants[0]["id"], "withdrawn", "受试者撤回知情同意")
        self.assertEqual(ctx.exception.code, "site_isolation")
        with self.assertRaises(BusinessError) as ctx:
            self.store.register_outcome("site1", participants[0]["id"], "lost", "失联")
        self.assertEqual(ctx.exception.code, "invalid_outcome")
        with self.assertRaises(BusinessError) as ctx:
            self.store.register_outcome("site1", participants[0]["id"], "withdrawn", " ")
        self.assertEqual(ctx.exception.code, "reason_required")
        done = self.store.register_outcome("site1", participants[0]["id"], "withdrawn", "受试者撤回知情同意", "2026-09-20")
        self.assertEqual(done["status"], "withdrawn")
        with self.assertRaises(BusinessError) as ctx:
            self.store.register_outcome("site1", participants[0]["id"], "completed", "重复登记")
        self.assertEqual(ctx.exception.code, "outcome_exists")
        # 退出后随机编号不回收：新入组占用新的分配
        p4 = self.store.enroll("site1", tid, "S001-004", {"risk": "high"})
        with self.store.connect() as conn:
            used = conn.execute("SELECT COUNT(*) FROM allocations WHERE used_by IS NOT NULL").fetchone()[0]
            allocation_ids = [
                row[0]
                for row in conn.execute("SELECT allocation_id FROM participants WHERE trial_id=? ORDER BY id", (tid,)).fetchall()
            ]
        self.assertEqual(used, 4)
        self.assertEqual(len(set(allocation_ids)), 4)
        # 原因和时间进入变更记录
        summary = self.store.trial_summary("coord", tid)
        record = [a for a in summary["audit"] if a["action"] == "participant.withdrawn"]
        self.assertEqual(record[0]["detail"]["reason"], "受试者撤回知情同意")
        self.assertEqual(record[0]["detail"]["occurred_at"], "2026-09-20")
        self.assertEqual(self.store.get_participant("site1", participants[0]["id"])["status"], "withdrawn")
        self.assertEqual(self.store.get_participant("site1", p4["id"])["status"], "enrolled")


if __name__ == "__main__":
    unittest.main()
