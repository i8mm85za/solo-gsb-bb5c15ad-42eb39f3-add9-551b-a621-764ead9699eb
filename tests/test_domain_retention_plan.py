"""POST /domain-retention-plan 按故障域增量保留规划单元测试。

仅依赖标准库 + 本项目模块（fastapi 存在时额外跑路由层测试）。
运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import itertools
import random
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
    build_sized_delta_chain,
    plan_domain_retention,
)


def _plan(target, replicas, deltas, budget):
    """replicas: [(current, priority, domain)]；deltas: [(from, to, bytes)]。"""
    chain, sizes = build_sized_delta_chain(deltas)
    return plan_domain_retention(target, replicas, chain, sizes, budget)


def _covered_masks(req_masks, union):
    """给定保留集位掩码 union，返回每个副本是否被覆盖（up_to_date 恒覆盖）。"""
    covered = []
    for mask in req_masks:
        if mask is None:
            covered.append(False)
        elif mask == 0:
            covered.append(True)
        else:
            covered.append(mask & ~union == 0)
    return covered


def _brute_force(target, replicas, chain, sizes, budget):
    """小规模位掩码空间下的暴力最优解，作为对照真值。

    最优键：(各域覆盖 priority 升序向量, 全体覆盖 priority, 覆盖数, -字节,
    -增量序列) —— 实现侧取该键最大且增量序列字典序最小。
    """
    from app.serial import plan_transfer

    edges = sorted(sizes)
    edge_id = {edge: i for i, edge in enumerate(edges)}
    edge_bytes = [sizes[edge] for edge in edges]

    req_masks = []
    for current, _prio, _domain in replicas:
        mode, _reason, ordered = plan_transfer(current, target, chain)
        if mode == IXFR:
            mask = 0
            for edge in ordered:
                mask |= 1 << edge_id[edge]
            req_masks.append(mask)
        elif mode == UP_TO_DATE:
            req_masks.append(0)
        else:
            req_masks.append(None)

    domains = sorted({domain for _c, _p, domain in replicas})
    n = len(edges)
    best_key = None
    best_sequence = None
    for bits in range(1 << n):
        used = 0
        for i in range(n):
            if bits >> i & 1:
                used += edge_bytes[i]
        if used > budget:
            continue
        covered = _covered_masks(req_masks, bits)
        by_domain = []
        prio_total = 0
        count = 0
        for domain in domains:
            total = sum(
                p for cov, (_c, p, d) in zip(covered, replicas)
                if cov and d == domain
            )
            by_domain.append(total)
        for cov, (_c, p, _d) in zip(covered, replicas):
            if cov:
                prio_total += p
                count += 1
        sequence = [edges[i] for i in range(n) if bits >> i & 1]
        # 增量序列按定长 0/1 向量比较字典序（与序列列表字典序一致：
        # 低位边号更小，故取补位向量 -bits_padded）
        key = (tuple(sorted(by_domain)), prio_total, count, -used)
        if best_key is None or key > best_key or (
            key == best_key and sequence < best_sequence
        ):
            best_key = key
            best_sequence = sequence
    return best_sequence, best_key


class PlanDomainRetentionTests(unittest.TestCase):
    def test_single_replica_full_chain(self):
        retained, used, results, domains = _plan(
            4, [(1, 5, "dc-a")],
            [(1, 2, 3), (2, 3, 4), (3, 4, 5)], 12,
        )
        self.assertEqual(retained, [(1, 2), (2, 3), (3, 4)])
        self.assertEqual(used, 12)
        self.assertEqual(results, [(IXFR, None)])
        self.assertEqual(domains, [("dc-a", 5, 5)])

    def test_budget_insufficient_retains_nothing(self):
        retained, used, results, domains = _plan(
            4, [(1, 5, "dc-a")],
            [(1, 2, 3), (2, 3, 4), (3, 4, 5)], 11,
        )
        self.assertEqual(retained, [])
        self.assertEqual(used, 0)
        self.assertEqual(results, [(AXFR, BUDGET)])
        # 无覆盖：covered_priority 为 0，total_priority 仍统计全体副本
        self.assertEqual(domains, [("dc-a", 0, 5)])

    def test_domain_vector_lifts_minimum_first(self):
        # 预算 10，三条互不相干的单跳链：
        #   X：保留 dc-a 的链（10 字节）-> 仅 dc-a 覆盖 3，向量 (0, 0, 3)；
        #   Y：保留 dc-b、dc-c 的链（各 5 字节）-> 两域各覆盖 1，向量 (0, 1, 1)。
        # (0, 1, 1) 字典序更大：优先抬升覆盖最少的域，即使 Y 的全体
        # priority 总和（2）小于 X（3）。
        deltas = [(1, 100, 10), (2, 100, 5), (3, 100, 5)]
        replicas = [(1, 3, "dc-a"), (2, 1, "dc-b"), (3, 1, "dc-c")]
        retained, used, results, domains = _plan(100, replicas, deltas, 10)
        self.assertEqual(retained, [(2, 100), (3, 100)])
        self.assertEqual(used, 10)
        self.assertEqual(results[0], (AXFR, BUDGET))
        self.assertEqual(results[1], (IXFR, None))
        self.assertEqual(results[2], (IXFR, None))
        self.assertEqual(
            domains,
            [("dc-a", 0, 3), ("dc-b", 1, 1), ("dc-c", 1, 1)],
        )

    def test_vector_dominates_total_priority(self):
        # 向量 (0, 1, 3)（两域有覆盖，全体 priority 4）胜过
        # (0, 0, 5)（单域覆盖，全体 priority 5）：按域公平向量优先，
        # 即便全体覆盖 priority 总和更小。
        deltas = [(1, 100, 5), (2, 100, 5), (3, 100, 10)]
        replicas = [(1, 1, "dc-a"), (2, 3, "dc-b"), (3, 5, "dc-c")]
        retained, _used, results, domains = _plan(
            100, replicas, deltas, 10
        )
        self.assertEqual(retained, [(1, 100), (2, 100)])
        self.assertEqual(
            results, [(IXFR, None), (IXFR, None), (AXFR, BUDGET)]
        )
        self.assertEqual(
            domains, [("dc-a", 1, 1), ("dc-b", 3, 3), ("dc-c", 0, 5)]
        )

    def test_equal_vector_falls_through_to_count(self):
        # 向量同为 (0, 4)（两域中恰有一域覆盖 4）、全体 priority 同为 4，
        # 覆盖数同为 1，最终由字节数裁决：该场景确认按域向量平局时
        # 后续规则（此处为占用字节）正常接管。
        deltas = [(1, 100, 10), (2, 100, 8)]
        replicas = [(1, 4, "dc-a"), (2, 4, "dc-b")]
        retained, used, _results, domains = _plan(
            100, replicas, deltas, 10
        )
        self.assertEqual(retained, [(2, 100)])
        self.assertEqual(used, 8)
        self.assertEqual(domains, [("dc-a", 0, 4), ("dc-b", 4, 4)])

    def test_covered_count_tiebreak(self):
        # 各域升序向量、全体 priority 均相同，取覆盖副本数更多者：
        # 方案 X：dc-a 的 p=5 单副本；方案 Y：dc-b 的 5 个 p=1 副本。
        # 两种方案向量均为 (0, 5)、全体 priority 5，Y 覆盖 5 个副本胜出。
        deltas = [(1, 100, 10)] + [(10 + i, 100, 2) for i in range(5)]
        replicas = (
            [(1, 5, "dc-a")]
            + [(10 + i, 1, "dc-b") for i in range(5)]
        )
        retained, used, results, domains = _plan(
            100, replicas, deltas, 10
        )
        self.assertEqual(
            retained, sorted((10 + i, 100) for i in range(5))
        )
        self.assertEqual(used, 10)
        self.assertEqual(results[0], (AXFR, BUDGET))
        self.assertTrue(all(m == IXFR for m, _r in results[1:]))
        self.assertEqual(domains, [("dc-a", 0, 5), ("dc-b", 5, 5)])

    def test_fewer_bytes_tiebreak(self):
        # 向量、全体 priority、覆盖数相同，取占用字节更少者
        deltas = [(1, 100, 10), (2, 100, 8)]
        replicas = [(1, 5, "dc-a"), (2, 5, "dc-a")]
        retained, used, results, _domains = _plan(
            100, replicas, deltas, 10
        )
        self.assertEqual(retained, [(2, 100)])
        self.assertEqual(used, 8)
        self.assertEqual(
            results, [(AXFR, BUDGET), (IXFR, None)]
        )

    def test_lexicographic_sequence_tiebreak(self):
        # 所有目标键相同，取 (from_serial, to_serial) 序列字典序更小者
        deltas = [(10, 100, 10), (5, 100, 10)]
        replicas = [(10, 5, "dc-a"), (5, 5, "dc-a")]
        retained, _used, results, domains = _plan(
            100, replicas, deltas, 10
        )
        self.assertEqual(retained, [(5, 100)])
        self.assertEqual(
            results, [(AXFR, BUDGET), (IXFR, None)]
        )
        self.assertEqual(domains, [("dc-a", 5, 10)])

    def test_up_to_date_counts_in_domain_vector(self):
        # up_to_date 副本零成本恒覆盖，计入所在域的向量分量
        retained, used, results, domains = _plan(
            5,
            [(5, 100, "dc-a"), (1, 1, "dc-b")],
            [(1, 5, 10)], 0,
        )
        self.assertEqual(retained, [])
        self.assertEqual(used, 0)
        self.assertEqual(results, [(UP_TO_DATE, None), (AXFR, BUDGET)])
        self.assertEqual(
            domains, [("dc-a", 100, 100), ("dc-b", 0, 1)]
        )

    def test_shared_chain_bytes_counted_once_across_domains(self):
        # 不同域的副本共享链后缀：并集字节只计一次，两个域同时被覆盖
        deltas = [(1, 2, 6), (2, 3, 6), (3, 4, 6)]
        retained, used, results, domains = _plan(
            4, [(1, 3, "dc-a"), (2, 7, "dc-b")], deltas, 18
        )
        self.assertEqual(retained, [(1, 2), (2, 3), (3, 4)])
        self.assertEqual(used, 18)
        self.assertEqual(results, [(IXFR, None), (IXFR, None)])
        self.assertEqual(
            domains, [("dc-a", 3, 3), ("dc-b", 7, 7)]
        )

    def test_domains_sorted_lexicographically(self):
        # 汇总按 failure_domain 字典序，与副本输入顺序无关
        _retained, _used, _results, domains = _plan(
            4,
            [
                (4, 9, "dc-z"),
                (1, 3, "dc-a"),
                (4, 2, "dc-m"),
            ],
            [(1, 4, 100)], 0,
        )
        self.assertEqual(
            [d[0] for d in domains], ["dc-a", "dc-m", "dc-z"]
        )
        # dc-a 副本预算不足未覆盖；两个 up_to_date 域恒覆盖
        self.assertEqual(
            domains,
            [("dc-a", 0, 3), ("dc-m", 2, 2), ("dc-z", 9, 9)],
        )

    def test_same_domain_replicas_share_component(self):
        # 同域两个副本：只覆盖一个时该域分量为其 priority，覆盖两个时求和
        deltas = [(1, 100, 5), (2, 100, 5)]
        retained, _used, results, domains = _plan(
            100, [(1, 3, "dc-a"), (2, 4, "dc-a")], deltas, 5
        )
        self.assertEqual(retained, [(2, 100)])
        self.assertEqual(results, [(AXFR, BUDGET), (IXFR, None)])
        self.assertEqual(domains, [("dc-a", 4, 7)])

    def test_inherent_gap_loop_incomparable_reasons_kept(self):
        retained, _used, results, domains = _plan(
            5, [(7, 1, "dc-a")], [(1, 5, 1)], 0,
        )
        self.assertEqual(retained, [])
        self.assertEqual(results, [(AXFR, GAP)])
        self.assertEqual(domains, [("dc-a", 0, 1)])

        deltas = [
            (0, HALF - 1, 1),
            (HALF - 1, MAX_SERIAL - 1, 1),
            (MAX_SERIAL - 1, 0, 1),
        ]
        _r, _u, results, _d = _plan(5, [(0, 1, "dc-a")], deltas, 100)
        self.assertEqual(results, [(AXFR, LOOP)])

        _r, _u, results, _d = _plan(
            HALF, [(0, 1, "dc-a")], [(0, 1, 1), (1, HALF, 1)], 100
        )
        self.assertEqual(results, [(AXFR, TARGET_INCOMPARABLE)])

    def test_zero_byte_delta_retained_at_zero_budget(self):
        retained, used, results, domains = _plan(
            2, [(1, 7, "dc-a")], [(1, 2, 0)], 0
        )
        self.assertEqual(retained, [(1, 2)])
        self.assertEqual(used, 0)
        self.assertEqual(results, [(IXFR, None)])
        self.assertEqual(domains, [("dc-a", 7, 7)])

    def test_wraparound_chain_sorted_output(self):
        deltas = [
            (MAX_SERIAL - 1, MAX_SERIAL, 3),
            (MAX_SERIAL, 0, 3),
            (0, 1, 3),
        ]
        retained, used, results, domains = _plan(
            1, [(MAX_SERIAL - 1, 1, "dc-a")], deltas, 9
        )
        self.assertEqual(
            retained,
            [(0, 1), (MAX_SERIAL - 1, MAX_SERIAL), (MAX_SERIAL, 0)],
        )
        self.assertEqual(used, 9)
        self.assertEqual(results, [(IXFR, None)])
        self.assertEqual(domains, [("dc-a", 1, 1)])

    def test_empty_delta_set(self):
        retained, used, results, domains = _plan(
            2,
            [(1, 1, "dc-a"), (2, 2, "dc-b")],
            [], 10,
        )
        self.assertEqual(retained, [])
        self.assertEqual(used, 0)
        self.assertEqual(results, [(AXFR, GAP), (UP_TO_DATE, None)])
        self.assertEqual(
            domains, [("dc-a", 0, 1), ("dc-b", 2, 2)]
        )

    def test_stable_under_input_reordering(self):
        # 相同语义输入（副本 / 增量乱序）必须得到稳定一致的结果
        deltas = [
            (3, 4, 6), (1, 2, 3), (2, 3, 3),
            (10, 20, 4), (11, 20, 4),
        ]
        replicas = [
            (1, 10, "dc-a"), (3, 5, "dc-b"),
            (4, 100, "dc-c"), (7, 1, "dc-a"),
            (10, 2, "dc-b"), (11, 3, "dc-d"),
        ]
        r1 = _plan(4, replicas, deltas, 12)
        shuffled_replicas = list(replicas)
        shuffled_deltas = list(deltas)
        random.Random(20260921).shuffle(shuffled_replicas)
        random.Random(20260922).shuffle(shuffled_deltas)
        r2 = _plan(4, shuffled_replicas, shuffled_deltas, 12)
        # retained / used / domains 与输入顺序无关，必须完全一致
        self.assertEqual(r1[0], r2[0])
        self.assertEqual(r1[1], r2[1])
        self.assertEqual(r1[3], r2[3])
        # 逐副本结果按 (current, domain) 对齐后也应与输入顺序无关
        map1 = {
            (rep[0], rep[2]): res
            for rep, res in zip(replicas, r1[2])
        }
        map2 = {
            (rep[0], rep[2]): res
            for rep, res in zip(shuffled_replicas, r2[2])
        }
        self.assertEqual(map1, map2)


class PlanDomainRetentionBruteForceTests(unittest.TestCase):
    """小位宽 / 小增量空间下与暴力枚举逐案对照（含全部平局裁决层级）。"""

    def _run_case(self, target, replicas_raw, raw_edges, budget):
        """replicas_raw: [(current, priority, domain_idx)]；
        raw_edges: [(from, to, bytes)]，序列号落在小整数区间内。"""
        chain, sizes = build_sized_delta_chain(raw_edges)
        # 域编号只取实际出现过的（允许编号不连续），按编号生成稳定域名
        active_domains = sorted({d for _c, _p, d in replicas_raw})
        domain_names = [f"d{i}" for i in active_domains]
        name_of = {idx: f"d{idx}" for idx in active_domains}
        replicas = [
            (c, p, name_of[d]) for c, p, d in replicas_raw
        ]
        retained, used, results, domains = plan_domain_retention(
            target, replicas, chain, sizes, budget
        )
        brute_seq, brute_key = _brute_force(
            target, replicas, chain, sizes, budget
        )
        self.assertEqual(retained, brute_seq, msg=locals())
        self.assertEqual(used, sum(sizes[e] for e in brute_seq))
        # 由实现结果反算最优键并对照
        edges = sorted(sizes)
        edge_id = {e: i for i, e in enumerate(edges)}
        from app.serial import plan_transfer
        req_masks = []
        for current, _p, _d in replicas:
            mode, _r, ordered = plan_transfer(current, target, chain)
            if mode == IXFR:
                m = 0
                for edge in ordered:
                    m |= 1 << edge_id[edge]
                req_masks.append(m)
            elif mode == UP_TO_DATE:
                req_masks.append(0)
            else:
                req_masks.append(None)
        union = 0
        for edge in retained:
            union |= 1 << edge_id[edge]
        covered = _covered_masks(req_masks, union)
        domain_sums = sorted(
            sum(p for cov, (c, p, d) in zip(covered, replicas)
                if cov and d == name)
            for name in domain_names
        )
        key = (
            tuple(domain_sums),
            sum(p for cov, (_c, p, _d) in zip(covered, replicas) if cov),
            sum(1 for cov in covered if cov),
            -used,
        )
        self.assertEqual(key, brute_key)

    def test_matches_exhaustive_search(self):
        rng = random.Random(20260923)
        domain_count = 3
        n_nodes = 1 << 3  # 8 个序列号节点
        for _ in range(300):
            n_edges = rng.randint(0, 4)
            # 生成合法严格 newer 边：所有序列号都在小整数区间，
            # 不跨 2^32 回绕，故在 32 位 RFC 1982 空间同样严格 newer
            raw_edges = []
            seen_from = set()
            for _e in range(n_edges):
                frm = rng.randrange(n_nodes)
                dist = rng.randint(1, 3)
                to = frm + dist
                if frm in seen_from:
                    continue
                seen_from.add(frm)
                raw_edges.append((frm, to, rng.randint(0, 6)))
            n_reps = rng.randint(1, 5)
            replicas_raw = [
                (
                    rng.randrange(n_nodes),
                    rng.randint(0, 5),
                    rng.randrange(domain_count),
                )
                for _r in range(n_reps)
            ]
            target = rng.randrange(n_nodes)
            budget = rng.randint(0, 12)
            self._run_case(target, replicas_raw, raw_edges, budget)


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

    def _basic_payload(self, budget=12):
        return {
            "target": 4,
            "budget": budget,
            "replicas": [
                {"name": "ns-a", "current": 1, "priority": 10,
                 "failure_domain": "dc-east"},
                {"name": "ns-b", "current": 3, "priority": 5,
                 "failure_domain": "dc-east"},
                {"name": "ns-c", "current": 4, "priority": 100,
                 "failure_domain": "dc-west"},
                {"name": "ns-d", "current": 7, "priority": 1,
                 "failure_domain": "dc-west"},
            ],
            "deltas": [
                {"from_serial": 3, "to_serial": 4, "bytes": 6},
                {"from_serial": 1, "to_serial": 2, "bytes": 3},
                {"from_serial": 2, "to_serial": 3, "bytes": 3},
                {"from_serial": 1, "to_serial": 2, "bytes": 3},
            ],
        }

    def test_endpoint_basic_flow(self):
        resp = self._request(self._basic_payload())
        self.assertEqual(resp.target, 4)
        self.assertEqual(resp.budget, 12)
        self.assertEqual(resp.used_bytes, 12)
        self.assertEqual(
            [(d.from_serial, d.to_serial, d.bytes)
             for d in resp.retained_deltas],
            [(1, 2, 3), (2, 3, 3), (3, 4, 6)],
        )
        self.assertEqual(
            [r.name for r in resp.results],
            ["ns-a", "ns-b", "ns-c", "ns-d"],
        )
        a, b, c, d = resp.results
        self.assertEqual((a.mode, a.reason, a.failure_domain),
                         (IXFR, None, "dc-east"))
        self.assertEqual((b.mode, b.reason, b.failure_domain),
                         (IXFR, None, "dc-east"))
        self.assertEqual((c.mode, c.reason, c.failure_domain),
                         (UP_TO_DATE, None, "dc-west"))
        self.assertEqual((d.mode, d.reason, d.failure_domain),
                         (AXFR, GAP, "dc-west"))
        # 按域汇总（failure_domain 字典序）
        self.assertEqual(
            [(x.failure_domain, x.covered_priority, x.total_priority)
             for x in resp.domains],
            [("dc-east", 15, 15), ("dc-west", 100, 101)],
        )

    def test_endpoint_budget_shortfall_still_200(self):
        # 预算 11：只保留 (3,4) 保住 ns-b；up_to_date 的 ns-c 恒覆盖
        resp = self._request(self._basic_payload(budget=11))
        self.assertEqual(resp.used_bytes, 6)
        self.assertEqual(
            [(d.from_serial, d.to_serial) for d in resp.retained_deltas],
            [(3, 4)],
        )
        a, b, c, d = resp.results
        self.assertEqual((a.mode, a.reason), (AXFR, BUDGET))
        self.assertEqual((b.mode, b.reason), (IXFR, None))
        self.assertEqual((c.mode, c.reason), (UP_TO_DATE, None))
        self.assertEqual((d.mode, d.reason), (AXFR, GAP))
        self.assertEqual(
            [(x.failure_domain, x.covered_priority, x.total_priority)
             for x in resp.domains],
            [("dc-east", 5, 15), ("dc-west", 100, 101)],
        )

    def test_endpoint_domain_fairness_changes_choice(self):
        # 预算 10：保留 dc-a 的高 priority 单链 -> 向量 (0, 0, 99)；
        # 保留 dc-b / dc-c 两条廉价链 -> 向量 (0, 1, 1)（字典序更大）。
        # 按域公平性优先抬升覆盖最少的域，即便全体 priority 总和（2 < 99）。
        payload = {
            "target": 100,
            "budget": 10,
            "replicas": [
                {"name": "ns-hi", "current": 1, "priority": 99,
                 "failure_domain": "dc-a"},
                {"name": "ns-lo-b", "current": 2, "priority": 1,
                 "failure_domain": "dc-b"},
                {"name": "ns-lo-c", "current": 3, "priority": 1,
                 "failure_domain": "dc-c"},
            ],
            "deltas": [
                {"from_serial": 1, "to_serial": 100, "bytes": 10},
                {"from_serial": 2, "to_serial": 100, "bytes": 5},
                {"from_serial": 3, "to_serial": 100, "bytes": 5},
            ],
        }
        resp = self._request(payload)
        self.assertEqual(
            [(d.from_serial, d.to_serial) for d in resp.retained_deltas],
            [(2, 100), (3, 100)],
        )
        self.assertEqual(resp.used_bytes, 10)
        self.assertEqual(
            [(x.failure_domain, x.covered_priority, x.total_priority)
             for x in resp.domains],
            [
                ("dc-a", 0, 99),
                ("dc-b", 1, 1),
                ("dc-c", 1, 1),
            ],
        )
        modes = {r.name: (r.mode, r.reason) for r in resp.results}
        self.assertEqual(modes["ns-hi"], (AXFR, BUDGET))
        self.assertEqual(modes["ns-lo-b"], (IXFR, None))
        self.assertEqual(modes["ns-lo-c"], (IXFR, None))

    def test_endpoint_rejects_bytes_conflict_422(self):
        payload = {
            "target": 3, "budget": 100,
            "replicas": [
                {"name": "ns-a", "current": 1, "priority": 1,
                 "failure_domain": "dc-a"}
            ],
            "deltas": [
                {"from_serial": 1, "to_serial": 2, "bytes": 10},
                {"from_serial": 1, "to_serial": 2, "bytes": 20},
            ],
        }
        with self.assertRaises(_HTTPException) as ctx:
            self._request(payload)
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.detail["error"], "delta_bytes_conflict")

    def test_endpoint_rejects_fork_422(self):
        payload = {
            "target": 3, "budget": 100,
            "replicas": [
                {"name": "ns-a", "current": 1, "priority": 1,
                 "failure_domain": "dc-a"}
            ],
            "deltas": [
                {"from_serial": 1, "to_serial": 2, "bytes": 1},
                {"from_serial": 1, "to_serial": 3, "bytes": 1},
            ],
        }
        with self.assertRaises(_HTTPException) as ctx:
            self._request(payload)
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.detail["error"], "delta_fork")

    def test_endpoint_rejects_non_newer_delta_422(self):
        payload = {
            "target": 2, "budget": 100,
            "replicas": [
                {"name": "ns-a", "current": 0, "priority": 1,
                 "failure_domain": "dc-a"}
            ],
            "deltas": [
                {"from_serial": 0, "to_serial": HALF, "bytes": 1}
            ],
        }
        with self.assertRaises(_HTTPException) as ctx:
            self._request(payload)
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(
            ctx.exception.detail["error"], "delta_not_strictly_newer"
        )

    def test_endpoint_too_many_replicas_422(self):
        replicas = [
            {"name": f"ns-{i}", "current": i, "priority": 1,
             "failure_domain": "dc-a"}
            for i in range(13)
        ]
        payload = {
            "target": 100, "budget": 100,
            "replicas": replicas, "deltas": [],
        }
        with self.assertRaises(_HTTPException) as ctx:
            self._request(payload)
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
            {"target": 100, "budget": 0,
             "replicas": replicas, "deltas": []}
        )
        self.assertEqual(len(resp.results), 12)
        self.assertTrue(all(r.mode == UP_TO_DATE for r in resp.results))
        self.assertEqual(
            [x.failure_domain for x in resp.domains],
            ["dc-0", "dc-1", "dc-2"],
        )
        self.assertTrue(
            all(x.covered_priority == x.total_priority == 4
                for x in resp.domains)
        )

    def test_endpoint_duplicate_replica_name_422(self):
        payload = {
            "target": 2, "budget": 10,
            "replicas": [
                {"name": "ns-a", "current": 1, "priority": 1,
                 "failure_domain": "dc-a"},
                {"name": "ns-a", "current": 2, "priority": 1,
                 "failure_domain": "dc-b"},
            ],
            "deltas": [],
        }
        with self.assertRaises(_HTTPException) as ctx:
            self._request(payload)
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(
            ctx.exception.detail["error"], "duplicate_replica_name"
        )

    def test_endpoint_blank_failure_domain_rejected(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            DomainRetentionPlanRequest.model_validate(
                {
                    "target": 2, "budget": 10,
                    "replicas": [
                        {"name": "ns-a", "current": 1, "priority": 1,
                         "failure_domain": "   "}
                    ],
                    "deltas": [],
                }
            )

    def test_endpoint_missing_failure_domain_rejected(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            DomainRetentionPlanRequest.model_validate(
                {
                    "target": 2, "budget": 10,
                    "replicas": [
                        {"name": "ns-a", "current": 1, "priority": 1}
                    ],
                    "deltas": [],
                }
            )

    def test_endpoint_stable_across_reordering(self):
        p1 = self._basic_payload()
        p2 = self._basic_payload()
        p2["replicas"] = list(reversed(p1["replicas"]))
        p2["deltas"] = list(reversed(p1["deltas"]))
        r1 = self._request(p1)
        r2 = self._request(p2)
        self.assertEqual(
            [(d.from_serial, d.to_serial, d.bytes)
             for d in r1.retained_deltas],
            [(d.from_serial, d.to_serial, d.bytes)
             for d in r2.retained_deltas],
        )
        self.assertEqual(r1.used_bytes, r2.used_bytes)
        self.assertEqual(
            [(x.failure_domain, x.covered_priority, x.total_priority)
             for x in r1.domains],
            [(x.failure_domain, x.covered_priority, x.total_priority)
             for x in r2.domains],
        )
        # 按副本名对齐的逐副本模式一致
        m1 = {r.name: (r.mode, r.reason, r.failure_domain) for r in r1.results}
        m2 = {r.name: (r.mode, r.reason, r.failure_domain) for r in r2.results}
        self.assertEqual(m1, m2)


if __name__ == "__main__":
    unittest.main()
