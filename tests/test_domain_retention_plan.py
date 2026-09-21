"""POST /domain-retention-plan 按故障域增量保留规划单元测试。

仅依赖标准库 + 本项目模块。
运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from app.serial import (
    HALF,
    AXFR,
    BUDGET,
    GAP,
    IXFR,
    TARGET_INCOMPARABLE,
    UP_TO_DATE,
    build_sized_delta_chain,
    plan_domain_retention,
)


def _plan(target, replicas, deltas, budget):
    """replicas: [(current, priority, failure_domain)]；deltas: [(from,to,bytes)]。"""
    chain, sizes = build_sized_delta_chain(deltas)
    return plan_domain_retention(target, replicas, chain, sizes, budget)


class PlanDomainRetentionTests(unittest.TestCase):
    def test_single_domain_full_chain(self):
        retained, used, results, summaries = _plan(
            4,
            [(1, 5, "dc1")],
            [(1, 2, 3), (2, 3, 4), (3, 4, 5)],
            12,
        )
        self.assertEqual(retained, [(1, 2), (2, 3), (3, 4)])
        self.assertEqual(used, 12)
        self.assertEqual(results, [(IXFR, None)])
        self.assertEqual(summaries, [("dc1", 5, 5)])

    def test_budget_insufficient_retains_nothing(self):
        retained, used, results, summaries = _plan(
            4,
            [(1, 5, "dc1")],
            [(1, 2, 3), (2, 3, 4), (3, 4, 5)],
            11,
        )
        self.assertEqual(retained, [])
        self.assertEqual(used, 0)
        self.assertEqual(results, [(AXFR, BUDGET)])
        # 无任何已覆盖副本：covered_priority 为 0，total_priority 仍为 5
        self.assertEqual(summaries, [("dc1", 0, 5)])

    def test_vector_maximin_beats_total_priority(self):
        # 逐域升序向量字典序最大化优先于全体 priority 总和。
        # 三个域 C、P、Q，预算 20：
        # 覆盖两个 P 副本（各 priority 2，共 4）与 Q 副本（3）
        #   -> 向量 (0,3,4)，全体总和 7；
        # 仅覆盖 C 副本（priority 6）
        #   -> 向量 (0,0,6)，全体总和 6（更小，但末位 6 > 4，向量更差）。
        retained, used, results, summaries = _plan(
            100,
            [
                (1, 2, "P"),
                (2, 2, "P"),
                (3, 3, "Q"),
                (4, 6, "C"),
            ],
            [
                (1, 100, 5),
                (2, 100, 5),
                (3, 100, 10),
                (4, 100, 20),
            ],
            20,
        )
        self.assertEqual(retained, [(1, 100), (2, 100), (3, 100)])
        self.assertEqual(used, 20)
        self.assertEqual(
            results,
            [
                (IXFR, None),
                (IXFR, None),
                (IXFR, None),
                (AXFR, BUDGET),
            ],
        )
        self.assertEqual(
            summaries,
            [("C", 0, 6), ("P", 4, 4), ("Q", 3, 3)],
        )

    def test_vector_domains_anchored_and_sorted_by_name(self):
        # 输入中出现的全部域都作为分量（即便完全未覆盖），域分量按域名升序；
        # 两个对称方案（各覆盖一个域、priority 均为 10、字节均为 10）向量
        # 同为 (0,10)，前三目标全平，最终由增量序列字典序最小者裁决。
        retained, _used, _results, summaries = _plan(
            100,
            [(5, 10, "zebra"), (8, 10, "alpha")],
            [(5, 100, 10), (8, 100, 10)],
            10,
        )
        self.assertEqual(retained, [(5, 100)])
        self.assertEqual(summaries, [("alpha", 0, 10), ("zebra", 10, 10)])

    def test_vector_tie_then_covered_count(self):
        # 方案一：B 单个 priority 5 副本 -> 向量 (0,5)，覆盖 1 个；
        # 方案二：A 的两个 priority 3 / 2 副本 -> 向量 (0,5)，覆盖 2 个。
        # 升序向量相同（(0,5)）、全体 priority 相同（5），取覆盖副本数更多者。
        retained, _used, results, summaries = _plan(
            100,
            [(1, 3, "A"), (2, 2, "A"), (3, 5, "B")],
            [(1, 100, 5), (2, 100, 5), (3, 100, 10)],
            10,
        )
        self.assertEqual(retained, [(1, 100), (2, 100)])
        self.assertEqual(
            results, [(IXFR, None), (IXFR, None), (AXFR, BUDGET)]
        )
        self.assertEqual(summaries, [("A", 5, 5), ("B", 0, 5)])

    def test_covered_vs_total_priority_in_summary(self):
        # covered_priority 只计 ixfr/up_to_date 副本，total_priority 计该域全部
        # （含 budget 未覆盖与固有 gap 的副本）；混合两种 axfr 原因。
        retained, _used, results, summaries = _plan(
            4,
            [
                (3, 10, "A"),    # 只需 (3,4)：预算内 -> ixfr
                (4, 6, "A"),     # up_to_date，零成本覆盖
                (9, 3, "A"),     # 固有 gap
                (1, 2, "B"),     # 需完整三条链，预算不足 -> budget
            ],
            [(1, 2, 5), (2, 3, 5), (3, 4, 5)],
            5,
        )
        # 预算 5 仅保留 (3,4)：覆盖 A 域 ixfr + up_to_date，B 域落空
        self.assertEqual(retained, [(3, 4)])
        self.assertEqual(results[0], (IXFR, None))
        self.assertEqual(results[1], (UP_TO_DATE, None))
        self.assertEqual(results[2], (AXFR, GAP))
        self.assertEqual(results[3], (AXFR, BUDGET))
        self.assertEqual(summaries, [("A", 16, 19), ("B", 0, 2)])

    def test_tie_then_fewer_bytes(self):
        # 两域各一个 priority 5 副本：升序向量同为 (0,5)，覆盖数相同，
        # 取占用字节更少者。
        retained, used, _results, summaries = _plan(
            100,
            [(1, 5, "A"), (2, 5, "B")],
            [(1, 100, 10), (2, 100, 8)],
            10,
        )
        self.assertEqual(retained, [(2, 100)])
        self.assertEqual(used, 8)
        self.assertEqual(summaries, [("A", 0, 5), ("B", 5, 5)])

    def test_tie_then_lexicographic_deltas(self):
        # 同域两个独立副本，priority、字节完全相同：最后取增量序列字典序最小者。
        retained, _used, _results, _summaries = _plan(
            100,
            [(10, 5, "A"), (5, 5, "A")],
            [(10, 100, 10), (5, 100, 10)],
            10,
        )
        self.assertEqual(retained, [(5, 100)])

    def test_up_to_date_always_in_base_vector(self):
        # up_to_date 副本零成本恒覆盖，其 priority 直接进入域向量基值；
        # dc1 基值 100，故即便预算为 0 也显示 covered=100。
        retained, used, results, summaries = _plan(
            5,
            [(5, 100, "dc1"), (1, 7, "dc1"), (1, 9, "dc2")],
            [(1, 5, 10)],
            0,
        )
        self.assertEqual(retained, [])
        self.assertEqual(used, 0)
        self.assertEqual(
            results,
            [(UP_TO_DATE, None), (AXFR, BUDGET), (AXFR, BUDGET)],
        )
        self.assertEqual(summaries, [("dc1", 100, 107), ("dc2", 0, 9)])

    def test_inherent_gap_keeps_reason_and_counts_total(self):
        # 完整集合下即缺口：原因保持 gap，其 priority 只计入 total_priority。
        retained, _used, results, summaries = _plan(
            5,
            [(7, 3, "dc1"), (5, 4, "dc1")],
            [(1, 5, 1)],
            0,
        )
        self.assertEqual(retained, [])
        self.assertEqual(results, [(AXFR, GAP), (UP_TO_DATE, None)])
        self.assertEqual(summaries, [("dc1", 4, 7)])

    def test_inherent_target_incomparable(self):
        retained, _used, results, summaries = _plan(
            HALF,
            [(0, 5, "dc1")],
            [(0, 1, 1), (1, HALF, 1)],
            100,
        )
        self.assertEqual(retained, [])
        self.assertEqual(results, [(AXFR, TARGET_INCOMPARABLE)])
        self.assertEqual(summaries, [("dc1", 0, 5)])

    def test_shared_chain_union_counted_once(self):
        # 两副本共享链后缀，并集字节只计一次，且分属不同域时同时抬高向量。
        deltas = [(1, 2, 6), (2, 3, 6), (3, 4, 6)]
        retained, used, results, summaries = _plan(
            4,
            [(1, 1, "A"), (2, 1, "B")],
            deltas,
            18,
        )
        self.assertEqual(retained, [(1, 2), (2, 3), (3, 4)])
        self.assertEqual(used, 18)
        self.assertEqual(results, [(IXFR, None), (IXFR, None)])
        self.assertEqual(summaries, [("A", 1, 1), ("B", 1, 1)])

    def test_wraparound_chain(self):
        deltas = [
            (4294967295 - 1, 4294967295, 3),
            (4294967295, 0, 3),
            (0, 1, 3),
        ]
        retained, used, results, summaries = _plan(
            1,
            [(4294967295 - 1, 2, "dc1")],
            deltas,
            9,
        )
        self.assertEqual(
            retained,
            [(0, 1), (4294967295 - 1, 4294967295), (4294967295, 0)],
        )
        self.assertEqual(used, 9)
        self.assertEqual(results, [(IXFR, None)])
        self.assertEqual(summaries, [("dc1", 2, 2)])

    def test_zero_byte_delta_at_zero_budget(self):
        retained, used, results, summaries = _plan(
            2, [(1, 7, "dc1")], [(1, 2, 0)], 0
        )
        self.assertEqual(retained, [(1, 2)])
        self.assertEqual(used, 0)
        self.assertEqual(results, [(IXFR, None)])
        self.assertEqual(summaries, [("dc1", 7, 7)])

    def test_unrelated_deltas_never_retained(self):
        deltas = [(1, 2, 5), (50, 60, 1)]
        retained, used, results, summaries = _plan(
            2, [(1, 1, "dc1")], deltas, 100
        )
        self.assertEqual(retained, [(1, 2)])
        self.assertEqual(used, 5)
        self.assertEqual(results, [(IXFR, None)])
        self.assertEqual(summaries, [("dc1", 1, 1)])

    def test_deterministic_same_input(self):
        # 相同输入（含乱序增量与乱序副本）重复调用必须得到完全一致的结果。
        deltas_a = [(2, 3, 3), (1, 2, 3), (3, 4, 6)]
        deltas_b = list(reversed(deltas_a))
        replicas_a = [(1, 10, "b"), (3, 5, "a"), (4, 100, "a")]
        replicas_b = list(reversed(replicas_a))
        r1 = _plan(4, replicas_a, deltas_a, 12)
        r2 = _plan(4, replicas_b, deltas_b, 12)
        # 保留集与按域汇总不依赖输入排列；逐副本模式按 current 归一后比较
        self.assertEqual(r1[0], r2[0])
        self.assertEqual(r1[1], r2[1])
        modes1 = {c: mode for (c, _p, _d), (mode, _r)
                  in zip(replicas_a, r1[2])}
        modes2 = {c: mode for (c, _p, _d), (mode, _r)
                  in zip(replicas_b, r2[2])}
        self.assertEqual(modes1, modes2)
        self.assertEqual(r1[3], r2[3])
        self.assertEqual(r1[0], [(1, 2), (2, 3), (3, 4)])
        self.assertEqual(r1[3], [("a", 105, 105), ("b", 10, 10)])


# 路由层测试：直接以函数方式调用 FastAPI 端点（无需 httpx/TestClient）。
try:
    from fastapi import HTTPException as _HTTPException

    from app.main import domain_retention_plan
    from app.schemas import DomainRetentionPlanRequest

    _HAS_FASTAPI = True
except ImportError:  # pragma: no cover - CI 中 fastapi 必装
    _HAS_FASTAPI = False


@unittest.skipUnless(_HAS_FASTAPI, "fastapi 未安装")
class DomainRetentionPlanEndpointTests(unittest.TestCase):
    def _request(self, payload):
        return domain_retention_plan(
            DomainRetentionPlanRequest.model_validate(payload)
        )

    def test_endpoint_basic_flow(self):
        payload = {
            "target": 4,
            "budget": 12,
            "replicas": [
                {"name": "ns-a", "current": 1, "priority": 10,
                 "failure_domain": "dc-east"},
                {"name": "ns-b", "current": 3, "priority": 5,
                 "failure_domain": "dc-west"},
                {"name": "ns-c", "current": 4, "priority": 100,
                 "failure_domain": "dc-west"},
                {"name": "ns-d", "current": 7, "priority": 1,
                 "failure_domain": "dc-east"},
            ],
            "deltas": [
                {"from_serial": 3, "to_serial": 4, "bytes": 6},
                {"from_serial": 1, "to_serial": 2, "bytes": 3},
                {"from_serial": 2, "to_serial": 3, "bytes": 3},
                {"from_serial": 1, "to_serial": 2, "bytes": 3},  # 重复
            ],
        }
        resp = self._request(payload)
        self.assertEqual(resp.used_bytes, 12)
        self.assertEqual(
            [(d.from_serial, d.to_serial, d.bytes)
             for d in resp.retained_deltas],
            [(1, 2, 3), (2, 3, 3), (3, 4, 6)],
        )
        a, b, c, d = resp.results
        self.assertEqual((a.failure_domain, a.mode, a.reason),
                         ("dc-east", IXFR, None))
        self.assertEqual((b.failure_domain, b.mode, b.reason),
                         ("dc-west", IXFR, None))
        self.assertEqual((c.failure_domain, c.mode, c.reason),
                         ("dc-west", UP_TO_DATE, None))
        self.assertEqual((d.failure_domain, d.mode, d.reason),
                         ("dc-east", AXFR, GAP))
        # 按域名升序：dc-east 覆盖 ns-a(10)；dc-west 覆盖 ns-b(5)+ns-c(100)
        self.assertEqual(
            [(s.failure_domain, s.covered_priority, s.total_priority)
             for s in resp.domain_summaries],
            [("dc-east", 10, 11), ("dc-west", 105, 105)],
        )

    def test_endpoint_budget_shortfall_still_200(self):
        resp = self._request(
            {
                "target": 2,
                "budget": 1,
                "replicas": [
                    {"name": "ns-a", "current": 1, "priority": 5,
                     "failure_domain": "dc1"}
                ],
                "deltas": [{"from_serial": 1, "to_serial": 2, "bytes": 10}],
            }
        )
        self.assertEqual(resp.used_bytes, 0)
        self.assertEqual(resp.retained_deltas, [])
        self.assertEqual(
            [(r.mode, r.reason) for r in resp.results], [(AXFR, BUDGET)]
        )
        self.assertEqual(
            [(s.failure_domain, s.covered_priority, s.total_priority)
             for s in resp.domain_summaries],
            [("dc1", 0, 5)],
        )

    def test_endpoint_maximin_prefers_balanced_domains(self):
        # 三个域 C、P、Q：均衡方案向量 (0,3,4) 优先于集中方案向量 (0,0,6)
        # （后者全体 priority 6 < 7，却因末位更大而落败，体现向量优先）。
        resp = self._request(
            {
                "target": 100,
                "budget": 20,
                "replicas": [
                    {"name": "p1", "current": 1, "priority": 2,
                     "failure_domain": "P"},
                    {"name": "p2", "current": 2, "priority": 2,
                     "failure_domain": "P"},
                    {"name": "q1", "current": 3, "priority": 3,
                     "failure_domain": "Q"},
                    {"name": "c1", "current": 4, "priority": 6,
                     "failure_domain": "C"},
                ],
                "deltas": [
                    {"from_serial": 1, "to_serial": 100, "bytes": 5},
                    {"from_serial": 2, "to_serial": 100, "bytes": 5},
                    {"from_serial": 3, "to_serial": 100, "bytes": 10},
                    {"from_serial": 4, "to_serial": 100, "bytes": 20},
                ],
            }
        )
        self.assertEqual(
            [(d.from_serial, d.to_serial) for d in resp.retained_deltas],
            [(1, 100), (2, 100), (3, 100)],
        )
        self.assertEqual(
            [(s.failure_domain, s.covered_priority, s.total_priority)
             for s in resp.domain_summaries],
            [("C", 0, 6), ("P", 4, 4), ("Q", 3, 3)],
        )
        by_name = {r.name: r for r in resp.results}
        self.assertEqual(by_name["c1"].mode, AXFR)
        self.assertEqual(by_name["c1"].reason, BUDGET)
        self.assertEqual(by_name["p1"].mode, IXFR)
        self.assertEqual(by_name["q1"].mode, IXFR)

    def test_endpoint_rejects_bytes_conflict_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "target": 3,
                    "budget": 100,
                    "replicas": [
                        {"name": "ns-a", "current": 1, "priority": 1,
                         "failure_domain": "dc1"}
                    ],
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
                    "replicas": [
                        {"name": "ns-a", "current": 1, "priority": 1,
                         "failure_domain": "dc1"}
                    ],
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
                    "replicas": [
                        {"name": "ns-a", "current": 0, "priority": 1,
                         "failure_domain": "dc1"}
                    ],
                    "deltas": [
                        {"from_serial": 0, "to_serial": HALF, "bytes": 1}
                    ],
                }
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(
            ctx.exception.detail["error"], "delta_not_strictly_newer"
        )

    def test_endpoint_too_many_replicas_422(self):
        replicas = [
            {"name": f"ns-{i}", "current": i, "priority": 1,
             "failure_domain": "dc1"}
            for i in range(13)
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
            {"name": f"ns-{i}", "current": 100, "priority": 1,
             "failure_domain": f"dc-{i % 3}"}
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
        # 三个域各 4 个 priority 1 副本，全部 up_to_date
        self.assertEqual(
            [(s.failure_domain, s.covered_priority, s.total_priority)
             for s in resp.domain_summaries],
            [("dc-0", 4, 4), ("dc-1", 4, 4), ("dc-2", 4, 4)],
        )

    def test_endpoint_duplicate_replica_name_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "target": 2,
                    "budget": 10,
                    "replicas": [
                        {"name": "ns-a", "current": 1, "priority": 1,
                         "failure_domain": "dc1"},
                        {"name": "ns-a", "current": 2, "priority": 1,
                         "failure_domain": "dc2"},
                    ],
                    "deltas": [],
                }
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(
            ctx.exception.detail["error"], "duplicate_replica_name"
        )

    def test_endpoint_blank_failure_domain_rejected(self):
        from pydantic import ValidationError

        base = {
            "target": 2,
            "budget": 10,
            "replicas": [
                {"name": "ns-a", "current": 1, "priority": 1,
                 "failure_domain": "dc1"}
            ],
            "deltas": [{"from_serial": 1, "to_serial": 2, "bytes": 1}],
        }
        for blank in ("", "   ", "\t"):
            with self.assertRaises(ValidationError):
                DomainRetentionPlanRequest.model_validate(
                    {**base, "replicas": [
                        {"name": "ns-a", "current": 1, "priority": 1,
                         "failure_domain": blank}
                    ]}
                )
        with self.assertRaises(ValidationError):
            DomainRetentionPlanRequest.model_validate(
                {**base, "replicas": [
                    {"name": "ns-a", "current": 1, "priority": 1}
                ]}
            )

    def test_endpoint_negative_fields_rejected(self):
        from pydantic import ValidationError

        base = {
            "target": 2,
            "budget": 10,
            "replicas": [
                {"name": "ns-a", "current": 1, "priority": 1,
                 "failure_domain": "dc1"}
            ],
            "deltas": [{"from_serial": 1, "to_serial": 2, "bytes": 1}],
        }
        with self.assertRaises(ValidationError):
            DomainRetentionPlanRequest.model_validate(
                {**base, "replicas": [
                    {"name": "ns-a", "current": 1, "priority": -1,
                     "failure_domain": "dc1"}
                ]}
            )
        with self.assertRaises(ValidationError):
            DomainRetentionPlanRequest.model_validate({**base, "budget": -1})
        with self.assertRaises(ValidationError):
            DomainRetentionPlanRequest.model_validate(
                {**base, "deltas": [
                    {"from_serial": 1, "to_serial": 2, "bytes": -1}
                ]}
            )
        with self.assertRaises(ValidationError):
            DomainRetentionPlanRequest.model_validate(
                {**base, "replicas": [
                    {"name": "ns-a", "current": 1, "priority": True,
                     "failure_domain": "dc1"}
                ]}
            )


if __name__ == "__main__":
    unittest.main()
