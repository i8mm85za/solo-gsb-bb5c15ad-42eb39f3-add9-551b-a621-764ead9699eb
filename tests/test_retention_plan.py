"""POST /retention-plan 增量保留规划单元测试（仅依赖标准库 + 本项目模块）。

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from app.serial import (
    HALF,
    MAX_SERIAL,
    AXFR,
    BUDGET,
    GAP,
    IXFR,
    LOOP,
    TARGET_INCOMPARABLE,
    UP_TO_DATE,
    DeltaValidationError,
    build_sized_delta_chain,
    plan_retention,
)


def _plan(target, replicas, deltas, budget):
    """replicas: [(current, priority)]；deltas: [(from, to, bytes)]。"""
    chain, sizes = build_sized_delta_chain(deltas)
    return plan_retention(target, replicas, chain, sizes, budget)


class BuildSizedDeltaChainTests(unittest.TestCase):
    def test_dedup_identical_deltas(self):
        chain, sizes = build_sized_delta_chain([(1, 2, 5), (1, 2, 5), (2, 3, 7)])
        self.assertEqual(chain, {1: 2, 2: 3})
        self.assertEqual(sizes, {(1, 2): 5, (2, 3): 7})

    def test_reject_bytes_conflict(self):
        with self.assertRaises(DeltaValidationError) as ctx:
            build_sized_delta_chain([(1, 2, 5), (3, 4, 6), (1, 2, 9)])
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], "delta_bytes_conflict")
        self.assertEqual(detail["from_serial"], 1)
        self.assertEqual(detail["to_serial"], 2)
        self.assertEqual(detail["bytes"], [5, 9])

    def test_reject_fork(self):
        with self.assertRaises(DeltaValidationError) as ctx:
            build_sized_delta_chain([(1, 2, 5), (1, 3, 5)])
        self.assertEqual(ctx.exception.detail["error"], "delta_fork")
        self.assertEqual(ctx.exception.detail["to_serials"], [2, 3])

    def test_reject_non_newer_delta(self):
        with self.assertRaises(DeltaValidationError) as ctx:
            build_sized_delta_chain([(0, HALF, 1)])
        self.assertEqual(
            ctx.exception.detail["error"], "delta_not_strictly_newer"
        )

    def test_non_newer_checked_before_bytes_conflict(self):
        # (0, 0) 本身非法，优先以非严格 newer 拒绝，而非字节数冲突
        with self.assertRaises(DeltaValidationError) as ctx:
            build_sized_delta_chain([(0, 0, 1), (0, 0, 2)])
        self.assertEqual(
            ctx.exception.detail["error"], "delta_not_strictly_newer"
        )

    def test_non_newer_checked_before_fork(self):
        with self.assertRaises(DeltaValidationError) as ctx:
            build_sized_delta_chain([(0, 0, 1), (0, 1, 1)])
        self.assertEqual(
            ctx.exception.detail["error"], "delta_not_strictly_newer"
        )

    def test_wrap_delta_is_newer(self):
        chain, sizes = build_sized_delta_chain([(MAX_SERIAL, 0, 3)])
        self.assertEqual(chain, {MAX_SERIAL: 0})
        self.assertEqual(sizes, {(MAX_SERIAL, 0): 3})


class PlanRetentionTests(unittest.TestCase):
    def test_single_replica_full_chain(self):
        retained, used, results = _plan(
            4, [(1, 5)], [(1, 2, 3), (2, 3, 4), (3, 4, 5)], 12
        )
        self.assertEqual(retained, [(1, 2), (2, 3), (3, 4)])
        self.assertEqual(used, 12)
        self.assertEqual(results, [(IXFR, None)])

    def test_budget_insufficient_retains_nothing(self):
        retained, used, results = _plan(
            4, [(1, 5)], [(1, 2, 3), (2, 3, 4), (3, 4, 5)], 11
        )
        self.assertEqual(retained, [])
        self.assertEqual(used, 0)
        self.assertEqual(results, [(AXFR, BUDGET)])

    def test_priority_maximization(self):
        # 预算只够一条链，选 priority 更高的副本
        retained, used, results = _plan(
            100, [(1, 3), (2, 7)], [(1, 100, 10), (2, 100, 10)], 10
        )
        self.assertEqual(retained, [(2, 100)])
        self.assertEqual(used, 10)
        self.assertEqual(results, [(AXFR, BUDGET), (IXFR, None)])

    def test_shared_chain_bytes_counted_once(self):
        # 两个副本共享链后缀：并集字节数只计一次
        deltas = [(1, 2, 6), (2, 3, 6), (3, 4, 6)]
        retained, used, results = _plan(4, [(1, 1), (2, 1)], deltas, 18)
        self.assertEqual(retained, [(1, 2), (2, 3), (3, 4)])
        self.assertEqual(used, 18)
        self.assertEqual(results, [(IXFR, None), (IXFR, None)])

    def test_shared_chain_partial_budget(self):
        deltas = [(1, 2, 6), (2, 3, 6), (3, 4, 6)]
        retained, used, results = _plan(4, [(1, 1), (2, 1)], deltas, 12)
        self.assertEqual(retained, [(2, 3), (3, 4)])
        self.assertEqual(used, 12)
        self.assertEqual(results, [(AXFR, BUDGET), (IXFR, None)])

    def test_tie_break_covered_count(self):
        # {a, b} 与 {c} 的 priority 总和相同（10），取覆盖副本数更多的 {a, b}
        deltas = [(1, 100, 10), (2, 100, 10), (3, 100, 20)]
        retained, used, results = _plan(
            100, [(1, 5), (2, 5), (3, 10)], deltas, 20
        )
        self.assertEqual(retained, [(1, 100), (2, 100)])
        self.assertEqual(used, 20)
        self.assertEqual(
            results, [(IXFR, None), (IXFR, None), (AXFR, BUDGET)]
        )

    def test_tie_break_fewer_bytes(self):
        # priority、覆盖数相同，取占用字节更少者
        retained, used, results = _plan(
            100, [(1, 5), (2, 5)], [(1, 100, 10), (2, 100, 8)], 10
        )
        self.assertEqual(retained, [(2, 100)])
        self.assertEqual(used, 8)
        self.assertEqual(results, [(AXFR, BUDGET), (IXFR, None)])

    def test_tie_break_lexicographic(self):
        # priority、覆盖数、字节数都相同，取增量序列字典序更小者
        retained, used, results = _plan(
            100, [(10, 5), (5, 5)], [(10, 100, 10), (5, 100, 10)], 10
        )
        self.assertEqual(retained, [(5, 100)])
        self.assertEqual(used, 10)
        self.assertEqual(results, [(AXFR, BUDGET), (IXFR, None)])

    def test_up_to_date_costs_nothing(self):
        retained, used, results = _plan(5, [(5, 100), (1, 1)], [(1, 5, 10)], 0)
        self.assertEqual(retained, [])
        self.assertEqual(used, 0)
        self.assertEqual(results, [(UP_TO_DATE, None), (AXFR, BUDGET)])

    def test_zero_byte_delta_retained_at_zero_budget(self):
        retained, used, results = _plan(2, [(1, 7)], [(1, 2, 0)], 0)
        self.assertEqual(retained, [(1, 2)])
        self.assertEqual(used, 0)
        self.assertEqual(results, [(IXFR, None)])

    def test_inherent_gap_not_relabeled_budget(self):
        # 完整集合下就是缺口：预算再小也保持 gap，而不是 budget
        retained, used, results = _plan(5, [(7, 1)], [(1, 5, 1)], 0)
        self.assertEqual(retained, [])
        self.assertEqual(results, [(AXFR, GAP)])

    def test_inherent_loop_reason(self):
        deltas = [
            (0, HALF - 1, 1),
            (HALF - 1, MAX_SERIAL - 1, 1),
            (MAX_SERIAL - 1, 0, 1),
        ]
        retained, used, results = _plan(5, [(0, 1)], deltas, 100)
        self.assertEqual(retained, [])
        self.assertEqual(used, 0)
        self.assertEqual(results, [(AXFR, LOOP)])

    def test_inherent_target_incomparable_reason(self):
        # 当前值与目标相差正好 2^31：预算再充足也必须 AXFR
        deltas = [(0, 1, 1), (1, HALF, 1)]
        retained, used, results = _plan(HALF, [(0, 1)], deltas, 100)
        self.assertEqual(retained, [])
        self.assertEqual(results, [(AXFR, TARGET_INCOMPARABLE)])

    def test_wraparound_chain(self):
        deltas = [
            (MAX_SERIAL - 1, MAX_SERIAL, 3),
            (MAX_SERIAL, 0, 3),
            (0, 1, 3),
        ]
        retained, used, results = _plan(1, [(MAX_SERIAL - 1, 1)], deltas, 9)
        # 输出按 (from_serial, to_serial) 升序，而非链路顺序
        self.assertEqual(
            retained, [(0, 1), (MAX_SERIAL - 1, MAX_SERIAL), (MAX_SERIAL, 0)]
        )
        self.assertEqual(used, 9)
        self.assertEqual(results, [(IXFR, None)])

    def test_unrelated_deltas_never_retained(self):
        # 与任何副本链无关的增量不会被保留
        deltas = [(1, 2, 5), (50, 60, 1)]
        retained, used, results = _plan(2, [(1, 1)], deltas, 100)
        self.assertEqual(retained, [(1, 2)])
        self.assertEqual(used, 5)
        self.assertEqual(results, [(IXFR, None)])

    def test_empty_delta_set(self):
        retained, used, results = _plan(2, [(1, 1), (2, 2)], [], 10)
        self.assertEqual(retained, [])
        self.assertEqual(used, 0)
        self.assertEqual(results, [(AXFR, GAP), (UP_TO_DATE, None)])


# 路由层测试：直接以函数方式调用 FastAPI 端点（无需 httpx/TestClient）。
try:
    from fastapi import HTTPException as _HTTPException

    from app.main import retention_plan
    from app.schemas import RetentionPlanRequest

    _HAS_FASTAPI = True
except ImportError:  # pragma: no cover - CI 中 fastapi 必装
    _HAS_FASTAPI = False


@unittest.skipUnless(_HAS_FASTAPI, "fastapi 未安装")
class RetentionPlanEndpointTests(unittest.TestCase):
    def _request(self, payload):
        return retention_plan(RetentionPlanRequest.model_validate(payload))

    def test_endpoint_basic_flow(self):
        payload = {
            "target": 4,
            "budget": 12,
            "replicas": [
                {"name": "ns-a", "current": 1, "priority": 10},
                {"name": "ns-b", "current": 3, "priority": 5},
                {"name": "ns-c", "current": 4, "priority": 100},
                {"name": "ns-d", "current": 7, "priority": 1},
            ],
            "deltas": [
                {"from_serial": 3, "to_serial": 4, "bytes": 6},
                {"from_serial": 1, "to_serial": 2, "bytes": 3},
                {"from_serial": 2, "to_serial": 3, "bytes": 3},
                {"from_serial": 1, "to_serial": 2, "bytes": 3},  # 重复，去重
            ],
        }
        resp = self._request(payload)
        self.assertEqual(resp.target, 4)
        self.assertEqual(resp.budget, 12)
        # 全链 12 字节恰好等于预算：ns-a、ns-b 均被覆盖
        self.assertEqual(resp.used_bytes, 12)
        self.assertEqual(
            [(d.from_serial, d.to_serial, d.bytes) for d in resp.retained_deltas],
            [(1, 2, 3), (2, 3, 3), (3, 4, 6)],
        )
        self.assertEqual([r.name for r in resp.results],
                         ["ns-a", "ns-b", "ns-c", "ns-d"])
        a, b, c, d = resp.results
        self.assertEqual((a.mode, a.reason), (IXFR, None))
        self.assertEqual((b.mode, b.reason), (IXFR, None))
        self.assertEqual((c.mode, c.reason), (UP_TO_DATE, None))
        self.assertEqual((d.mode, d.reason), (AXFR, GAP))

    def test_endpoint_budget_shortfall_still_200(self):
        # 预算无法保留任何完整链：仍正常返回，保留集为空
        resp = self._request(
            {
                "target": 2,
                "budget": 1,
                "replicas": [{"name": "ns-a", "current": 1, "priority": 5}],
                "deltas": [{"from_serial": 1, "to_serial": 2, "bytes": 10}],
            }
        )
        self.assertEqual(resp.used_bytes, 0)
        self.assertEqual(resp.retained_deltas, [])
        self.assertEqual(
            [(r.mode, r.reason) for r in resp.results], [(AXFR, BUDGET)]
        )

    def test_endpoint_rejects_bytes_conflict_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "target": 3,
                    "budget": 100,
                    "replicas": [{"name": "ns-a", "current": 1, "priority": 1}],
                    "deltas": [
                        {"from_serial": 1, "to_serial": 2, "bytes": 10},
                        {"from_serial": 1, "to_serial": 2, "bytes": 20},
                    ],
                }
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.detail["error"], "delta_bytes_conflict")

    def test_endpoint_rejects_fork_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "target": 3,
                    "budget": 100,
                    "replicas": [{"name": "ns-a", "current": 1, "priority": 1}],
                    "deltas": [
                        {"from_serial": 1, "to_serial": 2, "bytes": 1},
                        {"from_serial": 1, "to_serial": 3, "bytes": 1},
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
                    "budget": 100,
                    "replicas": [{"name": "ns-a", "current": 0, "priority": 1}],
                    "deltas": [{"from_serial": 0, "to_serial": HALF, "bytes": 1}],
                }
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(
            ctx.exception.detail["error"], "delta_not_strictly_newer"
        )

    def test_endpoint_too_many_replicas_422(self):
        replicas = [
            {"name": f"ns-{i}", "current": i, "priority": 1} for i in range(13)
        ]
        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "target": 100,
                    "budget": 100,
                    "replicas": replicas,
                    "deltas": [],
                }
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.detail["error"], "too_many_replicas")
        self.assertEqual(ctx.exception.detail["limit"], 12)
        self.assertEqual(ctx.exception.detail["got"], 13)

    def test_endpoint_twelve_replicas_ok(self):
        replicas = [
            {"name": f"ns-{i}", "current": 100, "priority": 1}
            for i in range(12)
        ]
        resp = self._request(
            {
                "target": 100,
                "budget": 0,
                "replicas": replicas,
                "deltas": [],
            }
        )
        self.assertEqual(len(resp.results), 12)
        self.assertTrue(all(r.mode == UP_TO_DATE for r in resp.results))

    def test_endpoint_duplicate_replica_name_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "target": 2,
                    "budget": 10,
                    "replicas": [
                        {"name": "ns-a", "current": 1, "priority": 1},
                        {"name": "ns-a", "current": 2, "priority": 1},
                    ],
                    "deltas": [],
                }
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(
            ctx.exception.detail["error"], "duplicate_replica_name"
        )

    def test_endpoint_negative_fields_rejected(self):
        from pydantic import ValidationError

        base = {
            "target": 2,
            "budget": 10,
            "replicas": [{"name": "ns-a", "current": 1, "priority": 1}],
            "deltas": [{"from_serial": 1, "to_serial": 2, "bytes": 1}],
        }
        with self.assertRaises(ValidationError):
            RetentionPlanRequest.model_validate(
                {**base, "replicas": [
                    {"name": "ns-a", "current": 1, "priority": -1}
                ]}
            )
        with self.assertRaises(ValidationError):
            RetentionPlanRequest.model_validate({**base, "budget": -1})
        with self.assertRaises(ValidationError):
            RetentionPlanRequest.model_validate(
                {**base, "deltas": [
                    {"from_serial": 1, "to_serial": 2, "bytes": -1}
                ]}
            )
        # bool 不是合法的非负整数
        with self.assertRaises(ValidationError):
            RetentionPlanRequest.model_validate(
                {**base, "replicas": [
                    {"name": "ns-a", "current": 1, "priority": True}
                ]}
            )


if __name__ == "__main__":
    unittest.main()
