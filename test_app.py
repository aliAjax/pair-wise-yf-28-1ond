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

    def test_pause_blocks_enrollment_but_keeps_followup_and_unblinding(self):
        enrolled = self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "low"})
        hold = self.store.pause_enrollment("coord", self.trial["id"], "出现疑似严重不良事件聚集，等待安全委员会评估")
        self.assertTrue(hold["active"])
        summary = self.store.trial_summary("coord", self.trial["id"])
        self.assertEqual(summary["trial"]["status"], "paused")
        self.assertEqual(summary["enrollment_hold"]["id"], hold["id"])
        # 新受试者被阻断，响应中带当前阻断原因
        with self.assertRaises(BusinessError) as ctx:
            self.store.enroll("site1", self.trial["id"], "S001-002", {"risk": "low"})
        self.assertEqual(ctx.exception.code, "enrollment_paused")
        self.assertEqual(ctx.exception.details["reason"], hold["reason"])
        # 已入组编号的重复请求仍幂等
        again = self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "low"})
        self.assertTrue(again["idempotent"])
        # 已入组受试者仍可申请并完成揭盲
        req = self.store.request_unblinding("site1", enrolled["id"], "受试者需要紧急救治必须知晓分组")
        self.store.approve_unblinding("monitor1", req["id"])
        second = self.store.approve_unblinding("monitor2", req["id"])
        self.assertEqual(second["status"], "approved")

    def test_resume_continues_sequence_and_reuses_no_number(self):
        p1 = self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "low"})
        self.store.pause_enrollment("coord", self.trial["id"], "安全信号：肝酶升高比例异常")
        with self.assertRaises(BusinessError):
            self.store.resume_enrollment("coord", self.trial["id"], "太短")
        self.store.resume_enrollment("coord", self.trial["id"], "完成数据核查并更新知情同意，安全委员会同意恢复")
        p2 = self.store.enroll("site1", self.trial["id"], "S001-002", {"risk": "low"})
        self.assertNotEqual(p1["allocation_code"], p2["allocation_code"])
        with self.store.connect() as conn:
            used = conn.execute("SELECT allocation_id FROM participants ORDER BY id").fetchall()
            sequences = [r["sequence"] for r in conn.execute(
                "SELECT a.sequence FROM allocations a JOIN participants p ON p.allocation_id=a.id ORDER BY p.id"
            ).fetchall()]
            self.assertEqual(len(used), 2)
            self.assertEqual(sorted(sequences), sequences)
            self.assertEqual(len(set(sequences)), 2)
        summary = self.store.trial_summary("coord", self.trial["id"])
        self.assertEqual(summary["trial"]["status"], "running")
        self.assertIsNone(summary["enrollment_hold"])
        self.assertFalse(summary["hold_history"][0]["active"])
        self.assertIn("resolution_note", summary["hold_history"][0])
        actions = [a["action"] for a in summary["audit"]]
        self.assertIn("enrollment.pause", actions)
        self.assertIn("enrollment.resume", actions)

    def test_site_registers_completion_and_withdrawal_with_reason_and_time(self):
        p1 = self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "low"})
        p2 = self.store.enroll("site1", self.trial["id"], "S001-002", {"risk": "low"})
        done = self.store.set_participant_status("site1", p1["id"], "completed", "随访结束，所有评估完成")
        gone = self.store.set_participant_status("site1", p2["id"], "withdrawn", "受试者撤回知情同意")
        self.assertEqual(done["status"], "completed")
        self.assertIsNotNone(done["status_changed_at"])
        self.assertEqual(done["status_changed_by"], "site1")
        self.assertEqual(gone["status"], "withdrawn")
        # 终态不能重复登记
        with self.assertRaises(BusinessError) as ctx:
            self.store.set_participant_status("site1", p1["id"], "withdrawn", "再次变更")
        self.assertEqual(ctx.exception.code, "status_locked")
        # 其他中心不能代登记
        other = self.store.enroll("site2", self.trial["id"], "S002-001", {"risk": "low"})
        with self.assertRaises(BusinessError) as ctx:
            self.store.set_participant_status("site1", other["id"], "completed", "越权登记")
        self.assertEqual(ctx.exception.code, "site_isolation")
        # 已退出/完成的编号不回收：新受试者拿到后续编号
        with self.store.connect() as conn:
            occupied = conn.execute("SELECT COUNT(*) FROM allocations WHERE used_by IS NOT NULL").fetchone()[0]
        p3 = self.store.enroll("site1", self.trial["id"], "S001-003", {"risk": "low"})
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM allocations WHERE used_by IS NOT NULL").fetchone()[0], occupied + 1)
            self.assertIsNotNone(conn.execute(
                "SELECT a.* FROM allocations a JOIN participants p ON p.allocation_id=a.id WHERE p.id=?", (p3["id"],)
            ).fetchone())
        # 变更原因和时间进入审计记录
        summary = self.store.trial_summary("coord", self.trial["id"])
        details = {a["action"]: a["detail"] for a in summary["audit"]}
        self.assertEqual(details["participant.completed"]["reason"], "随访结束，所有评估完成")
        self.assertIn("changed_at", details["participant.withdrawn"])

    def test_only_coordinator_can_pause_or_resume(self):
        with self.assertRaises(BusinessError) as ctx:
            self.store.pause_enrollment("site1", self.trial["id"], "中心自行暂停，理由足够长吧")
        self.assertEqual(ctx.exception.code, "forbidden")
        self.store.pause_enrollment("coord", self.trial["id"], "安全信号触发暂停，等待监查与整改")
        with self.assertRaises(BusinessError) as ctx:
            self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "low"})
        self.assertEqual(ctx.exception.code, "enrollment_paused")
        with self.assertRaises(BusinessError) as ctx:
            self.store.resume_enrollment("monitor1", self.trial["id"], "监查员不能直接恢复入组，需协调员确认")
        self.assertEqual(ctx.exception.code, "forbidden")


if __name__ == "__main__":
    unittest.main()
