"""RFC 1982 环形算术单元测试（仅依赖标准库）。

运行：python -m unittest discover -s tests -v
"""

import unittest

from app.serial import (
    HALF,
    MAX_SERIAL,
    MOD,
    compare_serial,
    forward_distance,
    propose_serial,
)


class ForwardDistanceTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(forward_distance(1, 2), 1)
        self.assertEqual(forward_distance(2, 1), MOD - 1)
        self.assertEqual(forward_distance(0, 0), 0)
        self.assertEqual(forward_distance(MAX_SERIAL, 0), 1)
        self.assertEqual(forward_distance(0, MAX_SERIAL), MAX_SERIAL)

    def test_antipode(self):
        self.assertEqual(forward_distance(0, HALF), HALF)
        self.assertEqual(forward_distance(HALF, 0), HALF)
        self.assertEqual(forward_distance(10, 10 + HALF), HALF)
        self.assertEqual(forward_distance(10 + HALF, 10), HALF)


class CompareSerialTests(unittest.TestCase):
    def test_equal(self):
        self.assertEqual(compare_serial(0, 0), "equal")
        self.assertEqual(compare_serial(MAX_SERIAL, MAX_SERIAL), "equal")
        self.assertEqual(compare_serial(HALF, HALF), "equal")

    def test_newer(self):
        self.assertEqual(compare_serial(0, 1), "newer")
        self.assertEqual(compare_serial(100, 200), "newer")
        self.assertEqual(compare_serial(MAX_SERIAL, 0), "newer")  # 回绕
        self.assertEqual(compare_serial(5, 5 + HALF - 1), "newer")  # 恰好半环内
        self.assertEqual(compare_serial(HALF + 1, 0), "newer")  # 距离 HALF - 1

    def test_ambiguous_exactly_half(self):
        # 差值正好 2^31，必须为 ambiguous（两个方向等价）
        self.assertEqual(compare_serial(0, HALF), "ambiguous")
        self.assertEqual(compare_serial(HALF, 0), "ambiguous")
        self.assertEqual(compare_serial(12345, (12345 + HALF) % MOD), "ambiguous")
        self.assertEqual(compare_serial((12345 + HALF) % MOD, 12345), "ambiguous")

    def test_older(self):
        self.assertEqual(compare_serial(1, 0), "older")
        self.assertEqual(compare_serial(0, MAX_SERIAL), "older")
        self.assertEqual(compare_serial(5, 5 + HALF + 1), "older")
        self.assertEqual(compare_serial(0, HALF + 1), "older")

    def test_boundaries_around_half(self):
        # 半环两侧相邻点：一侧 newer，一侧 older，正点 ambiguous
        self.assertEqual(compare_serial(0, HALF - 1), "newer")
        self.assertEqual(compare_serial(0, HALF), "ambiguous")
        self.assertEqual(compare_serial(0, HALF + 1), "older")


class ProposeSerialTests(unittest.TestCase):
    def test_single_replica_zero(self):
        self.assertEqual(propose_serial([0]), (1, 1))

    def test_single_replica_wrap(self):
        # 最近的严格更新是回绕到 0
        self.assertEqual(propose_serial([MAX_SERIAL]), (0, 1))

    def test_all_equal(self):
        self.assertEqual(propose_serial([10, 10, 10]), (11, 1))

    def test_two_replicas(self):
        # 候选 3（距离 3,1，最大 3）优于候选 5（距离 5,3，最大 3 但数值大）
        # 实际：可行域 [3, 2147483647]，最优为 3
        self.assertEqual(propose_serial([0, 2]), (3, 3))

    def test_three_replicas_within_half(self):
        # {0, 100, 1000}：候选 1001，距离 1001/901/1，最大 1001
        self.assertEqual(propose_serial([0, 100, 1000]), (1001, 1001))

    def test_wrap_around_feasible(self):
        # {MAX_SERIAL, 0, 1}：都在半环内且跨 0，候选 2
        proposed, max_dist = propose_serial([MAX_SERIAL, 0, 1])
        self.assertEqual(proposed, 2)
        self.assertEqual(max_dist, 3)  # MAX_SERIAL -> 2 距离 3

    def test_antipodal_no_proposal(self):
        # 正对点：任何候选都不可能同时严格推进两者
        self.assertIsNone(propose_serial([0, HALF]))
        self.assertIsNone(propose_serial([12345, (12345 + HALF) % MOD]))

    def test_half_minus_two_pair_feasible(self):
        # {0, H-2}：交集恰为 {H-1}；距离分别 H-1 与 1
        proposed, max_dist = propose_serial([0, HALF - 2])
        self.assertEqual(proposed, HALF - 1)
        self.assertEqual(max_dist, HALF - 1)

    def test_antipode_plus_other(self):
        self.assertIsNone(propose_serial([0, HALF, 5]))

    def test_spread_over_half(self):
        # 0, H-1, H 同样无法共同推进
        self.assertIsNone(propose_serial([0, HALF - 1, HALF + 5]))

    def test_tie_prefers_smaller_numeric_value(self):
        # {10, 12}：可行弧 [13, 10 + H - 1]，最优候选 13
        proposed, _ = propose_serial([10, 12])
        self.assertEqual(proposed, 13)

    def test_all_proposed_are_newer(self):
        # 随机式抽检若干可行配置，候选对所有当前值都必须是 newer
        currents = [5, 500, 5_000_000, 2_000_000_000]
        outcome = propose_serial(currents)
        self.assertIsNotNone(outcome)
        proposed, _ = outcome
        for c in currents:
            d = forward_distance(c, proposed)
            self.assertGreaterEqual(d, 1)
            self.assertLessEqual(d, HALF - 1)
            self.assertEqual(compare_serial(c, proposed), "newer")


if __name__ == "__main__":
    unittest.main()


def _brute_propose(currents, bits):
    """小位宽空间暴力求解，作为对照真值。"""
    mod = 1 << bits
    half = 1 << (bits - 1)
    best = None
    for x in range(mod):
        distances = [(x - c) % mod for c in currents]
        if all(1 <= d <= half - 1 for d in distances):
            key = (max(distances), x)
            if best is None or key < best:
                best = key
    return best


def _fast_propose(currents, bits):
    """与 app.serial.propose_serial 相同的算法，参数化位宽。"""
    mod = 1 << bits
    half = 1 << (bits - 1)
    feasible = []
    for candidate in {(c + 1) % mod for c in currents}:
        distances = [(candidate - c) % mod for c in set(currents)]
        if all(1 <= d <= half - 1 for d in distances):
            feasible.append((max(distances), candidate))
    if not feasible:
        return None
    return min(feasible)


class ProposeBruteForceTests(unittest.TestCase):
    def test_matches_exhaustive_search(self):
        import itertools
        import random

        rng = random.Random(20260921)
        for bits in (3, 4, 6, 8):
            mod = 1 << bits
            # 随机组合
            for _ in range(400):
                n = rng.randint(1, 5)
                currents = [rng.randrange(mod) for _ in range(n)]
                self.assertEqual(
                    _fast_propose(currents, bits),
                    _brute_propose(currents, bits),
                    msg=f"bits={bits} currents={currents}",
                )
            # 穷举小位宽下的全部 1~3 副本组合
            if bits <= 4:
                for n in (1, 2, 3):
                    for currents in itertools.product(range(mod), repeat=n):
                        self.assertEqual(
                            _fast_propose(list(currents), bits),
                            _brute_propose(list(currents), bits),
                        )


if __name__ == "__main__":
    unittest.main()
