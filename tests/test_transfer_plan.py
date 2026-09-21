"""POST /transfer-plan 增量链规划单元测试（仅依赖标准库 + 本项目模块）。

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from app.serial import (
    HALF,
    MAX_SERIAL,
    MOD,
    AXFR,
    GAP,
    IXFR,
    LOOP,
    TARGET_INCOMPARABLE,
    UP_TO_DATE,
    DeltaValidationError,
    build_delta_chain,
    plan_transfer,
)


class BuildDeltaChainTests(unittest.TestCase):
    def test_dedup_identical_deltas(self):
        chain = build_delta_chain([(1, 2), (1, 2), (2, 3)])
        self.assertEqual(chain, {1: 2, 2: 3})

    def test_reject_fork(self):
        with self.assertRaises(DeltaValidationError) as ctx:
            build_delta_chain([(1, 2), (3, 4), (1, 5)])
        self.assertEqual(ctx.exception.detail["error"], "delta_fork")
        self.assertEqual(ctx.exception.detail["from_serial"], 1)
        self.assertEqual(ctx.exception.detail["to_serials"], [2, 5])

    def test_reject_equal_delta(self):
        with self.assertRaises(DeltaValidationError) as ctx:
            build_delta_chain([(5, 5)])
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], "delta_not_strictly_newer")
        self.assertEqual(detail["relation"], "equal")

    def test_reject_older_delta(self):
        # 整数上更大但在环上更旧（超过半环）
        with self.assertRaises(DeltaValidationError) as ctx:
            build_delta_chain([(0, HALF + 1)])
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], "delta_not_strictly_newer")
        self.assertEqual(detail["relation"], "older")

    def test_reject_antipodal_delta(self):
        with self.assertRaises(DeltaValidationError) as ctx:
            build_delta_chain([(0, HALF)])
        self.assertEqual(
            ctx.exception.detail["error"], "delta_not_strictly_newer"
        )
        self.assertEqual(ctx.exception.detail["relation"], "ambiguous")

    def test_wrap_delta_is_newer(self):
        # MAX -> 0 是合法的严格推进
        self.assertEqual(build_delta_chain([(MAX_SERIAL, 0)]), {MAX_SERIAL: 0})

    def test_non_newer_checked_before_fork(self):
        # (0, 0) 本身非法，优先以非严格 newer 拒绝，而非分叉
        with self.assertRaises(DeltaValidationError) as ctx:
            build_delta_chain([(0, 0), (0, 1)])
        self.assertEqual(
            ctx.exception.detail["error"], "delta_not_strictly_newer"
        )


class PlanTransferTests(unittest.TestCase):
    def test_up_to_date(self):
        mode, reason, ordered = plan_transfer(10, 10, {})
        self.assertEqual((mode, reason, ordered), (UP_TO_DATE, None, []))

    def test_ixfr_orders_unordered_deltas(self):
        # 输入链边完全乱序，输出必须按链路顺序
        chain = build_delta_chain([(3, 4), (1, 2), (4, 5), (2, 3)])
        mode, reason, ordered = plan_transfer(1, 5, chain)
        self.assertEqual(mode, IXFR)
        self.assertIsNone(reason)
        self.assertEqual(ordered, [(1, 2), (2, 3), (3, 4), (4, 5)])

    def test_ixfr_wrap_around(self):
        # 4294967294 -> 4294967295 -> 0 -> 1 是环上的合法连续推进
        chain = build_delta_chain(
            [(MAX_SERIAL, 0), (MAX_SERIAL - 1, MAX_SERIAL), (0, 1)]
        )
        mode, reason, ordered = plan_transfer(MAX_SERIAL - 1, 1, chain)
        self.assertEqual(mode, IXFR)
        self.assertEqual(
            ordered,
            [(MAX_SERIAL - 1, MAX_SERIAL), (MAX_SERIAL, 0), (0, 1)],
        )

    def test_axfr_gap(self):
        chain = build_delta_chain([(1, 2), (3, 4)])  # 缺 2 -> 3
        mode, reason, ordered = plan_transfer(1, 4, chain)
        self.assertEqual((mode, reason, ordered), (AXFR, GAP, []))

    def test_axfr_gap_with_no_deltas(self):
        mode, reason, ordered = plan_transfer(1, 2, {})
        self.assertEqual((mode, reason, ordered), (AXFR, GAP, []))

    def test_axfr_loop(self):
        # 合法环路只能跨 0 回绕（每条边都严格 newer，至少 3 条边，
        # 前向距离之和恰为 2^32）：0 -> H-1 -> M-2 -> 0
        chain = build_delta_chain(
            [(0, HALF - 1), (HALF - 1, MAX_SERIAL - 1), (MAX_SERIAL - 1, 0)]
        )
        mode, reason, ordered = plan_transfer(0, 5, chain)
        self.assertEqual((mode, reason, ordered), (AXFR, LOOP, []))

    def test_target_reachable_before_cycle_is_ixfr(self):
        # 链尾虽带环，但目标 MAX_SERIAL-1 已在入环路径上抵达：仍可 IXFR
        chain = build_delta_chain(
            [
                (1, 2),
                (2, HALF - 1),
                (HALF - 1, MAX_SERIAL - 1),
                (MAX_SERIAL - 1, 2),
            ]
        )
        mode, reason, ordered = plan_transfer(1, MAX_SERIAL - 1, chain)
        self.assertEqual(mode, IXFR)
        self.assertEqual(
            ordered, [(1, 2), (2, HALF - 1), (HALF - 1, MAX_SERIAL - 1)]
        )

    def test_axfr_target_incomparable_antipode(self):
        # 当前值与目标相差正好 2^31：即使存在能连通的边也必须 AXFR
        chain = build_delta_chain([(0, 1), (1, HALF)])
        mode, reason, ordered = plan_transfer(0, HALF, chain)
        self.assertEqual(
            (mode, reason, ordered), (AXFR, TARGET_INCOMPARABLE, [])
        )

    def test_antipode_from_arbitrary_current(self):
        current = 12345
        target = (current + HALF) % MOD
        mode, reason, _ = plan_transfer(current, target, {})
        self.assertEqual((mode, reason), (AXFR, TARGET_INCOMPARABLE))

    def test_complete_chain_across_wrap_to_older_target_is_ixfr(self):
        # 目标在 RFC 1982 意义上"更旧"（10 -> 2 的前向距离 > 2^31），
        # 但存在从副本出发的唯一完整链（跨环推进）：以链路实际连通性
        # 为准，规划为 IXFR
        chain = build_delta_chain([(10, HALF + 9), (HALF + 9, 2)])
        mode, reason, ordered = plan_transfer(10, 2, chain)
        self.assertEqual(mode, IXFR)
        self.assertEqual(ordered, [(10, HALF + 9), (HALF + 9, 2)])

    def test_older_target_without_chain_is_gap(self):
        mode, reason, _ = plan_transfer(10, 2, {10: 11})
        self.assertEqual((mode, reason), (AXFR, GAP))


# 路由层测试：直接以函数方式调用 FastAPI 端点（无需 httpx/TestClient）。
try:
    from fastapi import HTTPException as _HTTPException

    from app.main import transfer_plan
    from app.schemas import TransferPlanRequest

    _HAS_FASTAPI = True
except ImportError:  # pragma: no cover - CI 中 fastapi 必装
    _HAS_FASTAPI = False


@unittest.skipUnless(_HAS_FASTAPI, "fastapi 未安装")
class TransferPlanEndpointTests(unittest.TestCase):
    def _request(self, payload):
        return transfer_plan(TransferPlanRequest.model_validate(payload))

    def test_endpoint_modes_and_replica_order(self):
        payload = {
            "target": 5,
            "replicas": [
                {"name": "ns-a", "current": 5},        # up_to_date
                {"name": "ns-b", "current": 1},        # ixfr
                {"name": "ns-c", "current": 7},        # gap
                {"name": "ns-d", "current": 2},        # ixfr 中间切入
            ],
            "deltas": [
                {"from_serial": 3, "to_serial": 4},
                {"from_serial": 1, "to_serial": 2},
                {"from_serial": 4, "to_serial": 5},
                {"from_serial": 2, "to_serial": 3},
                {"from_serial": 2, "to_serial": 3},    # 重复，去重
            ],
        }
        resp = self._request(payload)
        self.assertEqual([r.name for r in resp.results],
                         ["ns-a", "ns-b", "ns-c", "ns-d"])

        a, b, c, d = resp.results
        self.assertEqual(a.mode, UP_TO_DATE)
        self.assertIsNone(a.reason)
        self.assertEqual(a.ordered_deltas, [])

        self.assertEqual(b.mode, IXFR)
        self.assertEqual(
            [(x.from_serial, x.to_serial) for x in b.ordered_deltas],
            [(1, 2), (2, 3), (3, 4), (4, 5)],
        )

        self.assertEqual((c.mode, c.reason), (AXFR, GAP))
        self.assertEqual(c.ordered_deltas, [])

        self.assertEqual(d.mode, IXFR)
        self.assertEqual(
            [(x.from_serial, x.to_serial) for x in d.ordered_deltas],
            [(2, 3), (3, 4), (4, 5)],
        )

    def test_endpoint_rejects_fork_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "target": 3,
                    "replicas": [{"name": "ns-a", "current": 1}],
                    "deltas": [
                        {"from_serial": 1, "to_serial": 2},
                        {"from_serial": 1, "to_serial": 9},
                    ],
                }
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.detail["error"], "delta_fork")

    def test_endpoint_rejects_non_newer_delta_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "target": 2,
                    "replicas": [{"name": "ns-a", "current": 0}],
                    "deltas": [{"from_serial": 0, "to_serial": HALF}],
                }
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(
            ctx.exception.detail["error"], "delta_not_strictly_newer"
        )

    def test_endpoint_duplicate_replica_name_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "target": 2,
                    "replicas": [
                        {"name": "ns-a", "current": 1},
                        {"name": "ns-a", "current": 2},
                    ],
                    "deltas": [],
                }
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(
            ctx.exception.detail["error"], "duplicate_replica_name"
        )

    def test_endpoint_loop_and_incomparable_reasons(self):
        payload = {
            "target": 100,
            "replicas": [
                {"name": "ns-loop", "current": 1},
                {"name": "ns-antipode", "current": (100 - HALF) % MOD},
            ],
            "deltas": [
                {"from_serial": 1, "to_serial": 2},
                {"from_serial": 2, "to_serial": HALF - 1},
                {"from_serial": HALF - 1, "to_serial": MAX_SERIAL - 1},
                {"from_serial": MAX_SERIAL - 1, "to_serial": 2},
            ],
        }
        resp = self._request(payload)
        loop_item, antipode_item = resp.results
        self.assertEqual((loop_item.mode, loop_item.reason), (AXFR, LOOP))
        self.assertEqual(
            (antipode_item.mode, antipode_item.reason),
            (AXFR, TARGET_INCOMPARABLE),
        )


if __name__ == "__main__":
    unittest.main()
