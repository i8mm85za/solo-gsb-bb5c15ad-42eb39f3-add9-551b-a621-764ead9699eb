"""RFC 1982 序列号算术（32 位无符号整数环形空间）。

序列号空间为模 2^32 的环。给定当前值 ``current`` 与拟发布值 ``proposed``，
定义前向（顺时针）模距离 ``d = (proposed - current) mod 2^32``，则：

* ``d == 0``          -> ``equal``      （相等）
* ``1 <= d < 2^31``   -> ``newer``      （严格更新，可比较）
* ``d == 2^31``       -> ``ambiguous``  （正对点，RFC 1982 规定不可比较）
* ``2^31 < d < 2^32`` -> ``older``      （回退，可比较）

参考：RFC 1982 <https://www.rfc-editor.org/rfc/rfc1982>，SERIAL-BOUND = 2^31。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

BITS = 32
MOD = 1 << BITS          # 2^32，序列号空间大小
HALF = 1 << (BITS - 1)   # 2^31，SERIAL-BOUND
MAX_SERIAL = MOD - 1     # 2^32 - 1，合法序列号上界

NEWER = "newer"
EQUAL = "equal"
OLDER = "older"
AMBIGUOUS = "ambiguous"


def forward_distance(current: int, proposed: int) -> int:
    """返回 *proposed* 相对 *current* 的前向模距离，取值 [0, 2^32 - 1]。"""
    return (proposed - current) % MOD


def compare_serial(current: int, proposed: int) -> str:
    """按 RFC 1982 判定 *proposed* 相对 *current* 的关系。

    返回 ``"newer"`` / ``"equal"`` / ``"older"`` / ``"ambiguous"``。
    """
    distance = forward_distance(current, proposed)
    if distance == 0:
        return EQUAL
    if distance == HALF:
        return AMBIGUOUS
    if distance < HALF:
        return NEWER
    return OLDER


def is_strictly_newer(current: int, proposed: int) -> bool:
    """*proposed* 是否对 *current* 严格更新且可比较（1 <= 距离 <= 2^31 - 1）。"""
    return compare_serial(current, proposed) == NEWER


def propose_serial(currents: List[int]) -> Optional[Tuple[int, int]]:
    """推导对所有副本都严格更新且可比较的最佳拟发布值。

    对每个副本 ``c_i``，候选 ``x`` 必须满足
    ``1 <= (x - c_i) mod 2^32 <= 2^31 - 1``。

    优化目标：最小化最大前向距离 ``max_i (x - c_i) mod 2^32``；
    若多个候选同分，取数值最小者。

    可行域是每个副本所允许的开弧 ``{c+1, ..., c+2^31-1}``（模 2^32）
    之交，非空时必为一段连续弧，其最小元素必是某个 ``(c + 1) mod 2^32``；
    而沿弧每前进一步，目标函数恰好增加 1，故只需枚举这些下界候选。

    返回 ``(proposed, max_forward_distance)``；不存在可行候选时返回 ``None``。
    """
    unique_current = set(currents)
    candidates = [(c + 1) % MOD for c in unique_current]

    feasible: List[Tuple[int, int]] = []
    for candidate in candidates:
        distances = [forward_distance(c, candidate) for c in unique_current]
        if all(1 <= d <= HALF - 1 for d in distances):
            feasible.append((max(distances), candidate))

    if not feasible:
        return None

    # 先按最大前向距离排序，同分再按候选数值排序
    max_distance, proposed = min(feasible, key=lambda item: (item[0], item[1]))
    return proposed, max_distance


# ---------------------------------------------------------------------------
# IXFR 增量链（POST /transfer-plan）
# ---------------------------------------------------------------------------

UP_TO_DATE = "up_to_date"
IXFR = "ixfr"
AXFR = "axfr"

# axfr 的 reason
GAP = "gap"
"""增量链存在缺口（某序列号没有对应的下一条增量）。"""
LOOP = "loop"
"""沿增量链行进时回到了已经过的序列号，形成环路。"""
TARGET_INCOMPARABLE = "target_incomparable"
"""副本当前值与目标序列号在 RFC 1982 下不可比较（前向距离恰为 2^31）。"""
BUDGET = "budget"
"""完整增量集合下存在到目标的完整链，但受字节预算限制未被保留。"""

MAX_RETENTION_REPLICAS = 12
"""POST /retention-plan 与 /domain-retention-plan 允许的副本数上限
（选择算法枚举副本子集，复杂度 2^n）。"""


class DeltaValidationError(ValueError):
    """增量集合非法：存在非严格 newer 的增量、同一 from_serial 分叉，
    或同一增量携带冲突的字节数。"""

    def __init__(self, detail: dict):
        super().__init__(str(detail))
        self.detail = detail


def _check_strictly_newer(deltas: List[Tuple[int, ...]]) -> None:
    """逐条拒绝 to_serial 非严格 newer 的增量（保留输入顺序，报错确定可复现）。

    每条增量取前两个元素为 ``(from_serial, to_serial)``，其余元素（如字节数）
    不参与判定。
    """
    for delta in deltas:
        from_serial, to_serial = delta[0], delta[1]
        relation = compare_serial(from_serial, to_serial)
        if relation != NEWER:
            raise DeltaValidationError(
                {
                    "error": "delta_not_strictly_newer",
                    "message": (
                        "增量的 to_serial 必须按 RFC 1982 严格新于 from_serial"
                        "（前向距离在 [1, 2^31-1]）"
                    ),
                    "from_serial": from_serial,
                    "to_serial": to_serial,
                    "relation": relation,
                }
            )


def build_delta_chain(
    deltas: List[Tuple[int, int]],
) -> dict:
    """校验并归一化一组无序 IXFR 增量，返回 ``from_serial -> to_serial`` 映射。

    处理顺序：

    1. 每条增量都必须满足 ``to_serial`` 按 RFC 1982 **严格新于**
       ``from_serial``（前向距离在 ``[1, 2^31-1]``），否则抛出
       ``DeltaValidationError``（error=delta_not_strictly_newer）；
    2. 完全相同的 ``(from_serial, to_serial)`` 重复增量去重；
    3. 同一 ``from_serial`` 指向不同 ``to_serial`` 即链分叉，抛出
       ``DeltaValidationError``（error=delta_fork）。

    校验后每个 from_serial 至多有一个后继，因此任何完整链都是唯一的。
    """
    # 第一步：逐条拒绝非严格 newer 的增量
    _check_strictly_newer(deltas)

    # 第二步 + 第三步：重复去重，分叉拒绝
    chain: dict = {}
    for from_serial, to_serial in deltas:
        if from_serial in chain:
            if chain[from_serial] == to_serial:
                continue  # 重复增量，去重
            raise _fork_error(from_serial, chain[from_serial], to_serial)
        chain[from_serial] = to_serial
    return chain


def _fork_error(from_serial: int, existing: int, new: int) -> DeltaValidationError:
    return DeltaValidationError(
        {
            "error": "delta_fork",
            "message": (
                f"同一 from_serial={from_serial} 指向不同的 to_serial，"
                "增量链不允许分叉"
            ),
            "from_serial": from_serial,
            "to_serials": [existing, new],
        }
    )


def build_sized_delta_chain(
    deltas: List[Tuple[int, int, int]],
) -> Tuple[dict, dict]:
    """校验并归一化一组带字节数的无序 IXFR 增量。

    返回 ``(chain, sizes)``：``chain`` 为 ``from_serial -> to_serial`` 映射，
    ``sizes`` 为 ``(from_serial, to_serial) -> bytes`` 映射。

    处理顺序（与 :func:`build_delta_chain` 一致，先验合法性再去重/查冲突）：

    1. 每条增量都必须严格 newer，否则抛出 ``DeltaValidationError``
       （error=delta_not_strictly_newer）；
    2. 完全相同的 ``(from_serial, to_serial, bytes)`` 重复增量去重；
    3. 按输入顺序逐条判定冲突：同一 ``(from_serial, to_serial)`` 携带不同
       bytes 即字节数冲突，抛出 ``DeltaValidationError``
       （error=delta_bytes_conflict）；同一 ``from_serial`` 指向不同
       ``to_serial`` 即链分叉，抛出 ``DeltaValidationError``
       （error=delta_fork）。
    """
    # 第一步：逐条拒绝非严格 newer 的增量
    _check_strictly_newer(deltas)

    # 第二步 + 第三步：重复去重，字节数冲突与分叉拒绝
    chain: dict = {}
    sizes: dict = {}
    for from_serial, to_serial, size in deltas:
        if from_serial in chain:
            if chain[from_serial] != to_serial:
                raise _fork_error(from_serial, chain[from_serial], to_serial)
            if sizes[(from_serial, to_serial)] != size:
                raise DeltaValidationError(
                    {
                        "error": "delta_bytes_conflict",
                        "message": (
                            f"增量 ({from_serial}, {to_serial}) 携带了"
                            "不同的字节数，相同增量的 bytes 必须一致"
                        ),
                        "from_serial": from_serial,
                        "to_serial": to_serial,
                        "bytes": [sizes[(from_serial, to_serial)], size],
                    }
                )
            continue  # 完全相同的重复增量，去重
        chain[from_serial] = to_serial
        sizes[(from_serial, to_serial)] = size
    return chain, sizes


def plan_transfer(
    current: int,
    target: int,
    chain: dict,
) -> Tuple[str, Optional[str], List[Tuple[int, int]]]:
    """为单个副本规划从 *current* 到 *target* 的传输方式。

    *chain* 为 :func:`build_delta_chain` 产出的 ``from_serial -> to_serial``
    映射，每条边都是 RFC 1982 严格推进。沿边逐跳追踪（**不做整数排序**，
    仅以增量自带的环形先后关系为准）：

    * ``current == target``：``(up_to_date, None, [])``；
    * 两端在 RFC 1982 下不可比较（前向距离恰为 2^31）：
      ``(axfr, target_incomparable, [])``，即使存在能连通的边也必须 AXFR；
    * 沿唯一增量链可抵达 *target*：``(ixfr, None, ordered_deltas)``；
    * 某一跳找不到后继：``(axfr, gap, [])``；
    * 行进中重复经过某序列号（环路）：``(axfr, loop, [])``。
    """
    if current == target:
        return UP_TO_DATE, None, []

    if compare_serial(current, target) == AMBIGUOUS:
        return AXFR, TARGET_INCOMPARABLE, []

    ordered: List[Tuple[int, int]] = []
    visited = {current}
    node = current
    while node != target:
        nxt = chain.get(node)
        if nxt is None:
            return AXFR, GAP, []
        if nxt in visited:
            return AXFR, LOOP, []
        ordered.append((node, nxt))
        visited.add(nxt)
        node = nxt
    return IXFR, None, ordered


# ---------------------------------------------------------------------------
# 增量保留规划（POST /retention-plan、POST /domain-retention-plan）
# ---------------------------------------------------------------------------


def _requirement_masks(
    target: int,
    replicas: List[Tuple[int, int]],
    chain: dict,
    edge_id: dict,
) -> Tuple[List[Optional[int]], List[Optional[str]]]:
    """在**完整**增量集合上为每个副本求需求集（增量位掩码）与固有原因。

    依次跑 :func:`plan_transfer`：

    * ``ixfr``：链上增量编号取位 OR，得到该副本的需求集掩码；
    * ``up_to_date``：需求集为 0（恒被覆盖，零成本），固有原因为 None；
    * ``axfr``（``gap`` / ``loop`` / ``target_incomparable``）：掩码为 None，
      保留其固有原因（任何保留子集都无法改变）。
    """
    req_masks: List[Optional[int]] = []
    inherent_reasons: List[Optional[str]] = []
    for current, _priority in replicas:
        mode, reason, ordered = plan_transfer(current, target, chain)
        if mode == IXFR:
            mask = 0
            for edge in ordered:
                mask |= 1 << edge_id[edge]
            req_masks.append(mask)
            inherent_reasons.append(None)
        elif mode == UP_TO_DATE:
            req_masks.append(0)
            inherent_reasons.append(None)
        else:
            req_masks.append(None)
            inherent_reasons.append(reason)
    return req_masks, inherent_reasons


def _mask_bytes(mask: int, edge_bytes: List[int]) -> int:
    """位掩码中选中增量的字节总数。"""
    used = 0
    bits = mask
    while bits:
        bit = bits & (-bits)
        used += edge_bytes[bit.bit_length() - 1]
        bits ^= bit
    return used


def _mask_sequence(
    mask: int, edges: List[Tuple[int, int]]
) -> List[Tuple[int, int]]:
    """位掩码展开为按 ``(from_serial, to_serial)`` 升序的增量序列。"""
    sequence: List[Tuple[int, int]] = []
    bits = mask
    while bits:
        bit = bits & (-bits)
        sequence.append(edges[bit.bit_length() - 1])
        bits ^= bit
    return sequence


def plan_retention(
    target: int,
    replicas: List[Tuple[int, int]],
    chain: dict,
    sizes: dict,
    budget: int,
) -> Tuple[List[Tuple[int, int]], int, List[Tuple[str, Optional[str]]]]:
    """在字节预算内选择要保留的增量子集。

    *replicas* 为 ``(current, priority)`` 二元组（至多
    ``MAX_RETENTION_REPLICAS`` 个）；*chain* / *sizes* 为
    :func:`build_sized_delta_chain` 的产物；*budget* 为非负总字节预算。

    先用 :func:`plan_transfer` 在**完整**增量集合上判定每个副本：

    * ``up_to_date``：当前值已等于目标，无需任何增量，恒视为已覆盖；
    * ``ixfr``：存在唯一完整链，链上增量即该副本的**需求集**——需求集
      被保留子集包含时标记 ``ixfr``，否则 ``axfr / budget``；
    * ``axfr``（``gap`` / ``loop`` / ``target_incomparable``）：完整集合
      下即不可达，保留任何子集都无法改变，原因保持。

    在所有满足 ``字节总数 <= budget`` 的保留子集中，依次：

    1. 最大化被覆盖副本（拥有到目标完整链）的 priority 总和；
    2. 最大化被覆盖副本数；
    3. 最小化占用字节数；
    4. 取按 ``(from_serial, to_serial)`` 升序排列的增量序列中字典序最小者。

    保留集必为被覆盖副本需求集之并（多保留对覆盖无贡献的增量只会浪费
    预算），因此枚举全部副本子集（至多 2^12）即可求得精确最优；
    预算无法保留任何完整链时，最优即空集（仍正常返回）。

    返回 ``(retained, used_bytes, results)``：``retained`` 为按
    ``(from_serial, to_serial)`` 升序的增量列表；``results`` 与输入副本
    同序，每项为 ``(mode, reason)``。
    """
    # 增量按 (from_serial, to_serial) 升序编号：按位展开即为有序序列
    edges = sorted(sizes)
    edge_id = {edge: i for i, edge in enumerate(edges)}
    edge_bytes = [sizes[edge] for edge in edges]

    # 第一遍：完整集合上判定，求出每个副本的需求集（增量位掩码）
    # None 表示完整集合下即不可达（gap / loop / target_incomparable）
    req_masks, inherent_reasons = _requirement_masks(
        target, replicas, chain, edge_id
    )

    # up_to_date 副本恒被覆盖，作为常量计入目标函数
    base_prio = sum(
        prio for (_, prio), mask in zip(replicas, req_masks) if mask == 0
    )
    base_count = sum(1 for mask in req_masks if mask == 0)

    # 只需枚举"完整集合下为 ixfr"的副本子集（至多 12 个 -> 至多 4096 个子集）
    enum_idx = [i for i, mask in enumerate(req_masks) if mask]
    reqs = [req_masks[i] for i in enum_idx]
    prios = [replicas[i][1] for i in enum_idx]
    k = len(enum_idx)

    union_of = [0] * (1 << k)
    best_key: Optional[Tuple[int, int, int]] = None
    best_unions: List[int] = []
    for subset in range(1 << k):
        lsb = subset & (-subset)
        if lsb:
            union = union_of[subset ^ lsb] | reqs[lsb.bit_length() - 1]
        else:
            union = 0
        union_of[subset] = union

        used = _mask_bytes(union, edge_bytes)
        if used > budget:
            continue

        prio = base_prio
        count = base_count
        for req, p in zip(reqs, prios):
            if req & ~union == 0:  # 需求集被保留集包含即被覆盖
                prio += p
                count += 1

        key = (prio, count, -used)
        if best_key is None or key > best_key:
            best_key = key
            best_unions = [union]
        elif key == best_key:
            best_unions.append(union)

    # 最后一级：增量序列（按 (from_serial, to_serial) 升序）字典序最小
    best_union = min(best_unions, key=lambda union: _mask_sequence(union, edges))
    retained = _mask_sequence(best_union, edges)

    # 空子集恒可行（budget >= 0），best_unions 必非空
    used_bytes = sum(sizes[edge] for edge in retained)

    results: List[Tuple[str, Optional[str]]] = []
    for mask, reason in zip(req_masks, inherent_reasons):
        if mask is None:
            results.append((AXFR, reason))
        elif mask == 0:
            results.append((UP_TO_DATE, None))
        elif mask & ~best_union == 0:
            results.append((IXFR, None))
        else:
            results.append((AXFR, BUDGET))
    return retained, used_bytes, results


def plan_domain_retention(
    target: int,
    replicas: List[Tuple[int, int, str]],
    chain: dict,
    sizes: dict,
    budget: int,
) -> Tuple[
    List[Tuple[int, int]],
    int,
    List[Tuple[str, Optional[str]]],
    List[Tuple[str, int, int]],
]:
    """在字节预算内按 failure_domain 维度选择要保留的增量子集。

    *replicas* 为 ``(current, priority, failure_domain)`` 三元组（至多
    ``MAX_RETENTION_REPLICAS`` 个，``failure_domain`` 非空）；*chain* /
    *sizes* 为 :func:`build_sized_delta_chain` 的产物；*budget* 为非负总字节
    预算。每个副本在完整集合下的可达性判定与 :func:`plan_retention` 完全一致
    （复用 :func:`plan_transfer`，``gap`` / ``loop`` /
    ``target_incomparable`` 原因保持）。

    对任一保留子集，先按 failure_domain 汇总该域已覆盖（``ixfr`` 与
    ``up_to_date``）副本的 priority 总和，再把各域总和按**升序**排列成向量。
    在所有满足 ``字节总数 <= budget`` 的保留子集中，依次：

    1. 各域已覆盖 priority 总和的**升序向量字典序最大**（逐域 maximin）；
    2. 全体已覆盖副本的 **priority 总和最大**；
    3. 已覆盖**副本数最多**；
    4. **占用字节最少**；
    5. 取按 ``(from_serial, to_serial)`` 升序排列的增量序列中字典序最小者。

    向量含输入中出现的**全部** failure_domain（按名称升序排列）：该域无任何
    已覆盖副本时对应分量为 0，从而域的排列不依赖于哪个子集被选中。

    与 :func:`plan_retention` 相同，保留集必为被覆盖 ixfr 副本需求集之并，
    枚举全部 ixfr 副本子集（至多 2^12）即可精确求解；空子集恒可行。

    返回 ``(retained, used_bytes, results, domain_summaries)``：``retained``
    为按 ``(from_serial, to_serial)`` 升序的增量列表；``results`` 与输入副本
    同序，每项为 ``(mode, reason)``；``domain_summaries`` 为按 failure_domain
    名称升序的 ``(failure_domain, covered_priority, total_priority)`` 列表，
    其中 ``total_priority`` 为该域**全部**副本的 priority 之和（不随子集变化）。
    """
    # 增量按 (from_serial, to_serial) 升序编号：按位展开即为有序序列
    edges = sorted(sizes)
    edge_id = {edge: i for i, edge in enumerate(edges)}
    edge_bytes = [sizes[edge] for edge in edges]

    # failure_domain 按名称升序固定编号，向量分量顺序与子集无关
    domains = sorted({domain for _c, _p, domain in replicas})
    domain_id = {domain: i for i, domain in enumerate(domains)}
    replica_domain = [domain_id[domain] for _c, _p, domain in replicas]
    domain_total = [0] * len(domains)
    for (_c, priority, _domain), di in zip(replicas, replica_domain):
        domain_total[di] += priority

    # 完整集合上判定，求出每个副本的需求集（增量位掩码）与固有原因
    replica_base = [(current, priority) for current, priority, _d in replicas]
    req_masks, inherent_reasons = _requirement_masks(
        target, replica_base, chain, edge_id
    )

    # up_to_date 副本恒被覆盖（零成本），其 priority 作为常量并入域向量
    base_vector = [0] * len(domains)
    base_prio = 0
    base_count = 0
    for (mask, (_c, priority, _d), di) in zip(
        req_masks, replicas, replica_domain
    ):
        if mask == 0:
            base_vector[di] += priority
            base_prio += priority
            base_count += 1

    # 只需枚举"完整集合下为 ixfr"的副本子集（至多 12 个 -> 至多 4096 个子集）
    enum_idx = [i for i, mask in enumerate(req_masks) if mask]
    reqs = [req_masks[i] for i in enum_idx]
    prios = [replicas[i][1] for i in enum_idx]
    req_domains = [replica_domain[i] for i in enum_idx]
    k = len(enum_idx)

    union_of = [0] * (1 << k)
    # key = (域向量, 全体 priority, 覆盖数, -字节数)；域向量升序字典序最大化。
    # 域向量须为 tuple（list 之间不支持 < / >，无法参与字典序比较）。
    best_key: Optional[Tuple[Tuple[int, ...], int, int, int]] = None
    best_unions: List[int] = []
    for subset in range(1 << k):
        lsb = subset & (-subset)
        if lsb:
            union = union_of[subset ^ lsb] | reqs[lsb.bit_length() - 1]
        else:
            union = 0
        union_of[subset] = union

        used = _mask_bytes(union, edge_bytes)
        if used > budget:
            continue

        covered = [0] * len(domains)
        prio = base_prio
        count = base_count
        for req, p, di in zip(reqs, prios, req_domains):
            if req & ~union == 0:  # 需求集被保留集包含即被覆盖
                covered[di] += p
                prio += p
                count += 1

        vector = tuple(
            sorted(base_vector[i] + covered[i] for i in range(len(domains)))
        )
        key = (vector, prio, count, -used)
        if best_key is None or key > best_key:
            best_key = key
            best_unions = [union]
        elif key == best_key:
            best_unions.append(union)

    # 最后一级：增量序列（按 (from_serial, to_serial) 升序）字典序最小
    best_union = min(best_unions, key=lambda union: _mask_sequence(union, edges))
    retained = _mask_sequence(best_union, edges)

    # 空子集恒可行（budget >= 0），best_unions 必非空
    used_bytes = sum(sizes[edge] for edge in retained)

    results: List[Tuple[str, Optional[str]]] = []
    domain_covered = [0] * len(domains)
    for mask, reason, (_, priority, _d), di in zip(
        req_masks, inherent_reasons, replicas, replica_domain
    ):
        if mask is None:
            results.append((AXFR, reason))
        elif mask == 0:
            results.append((UP_TO_DATE, None))
            domain_covered[di] += priority
        elif mask & ~best_union == 0:
            results.append((IXFR, None))
            domain_covered[di] += priority
        else:
            results.append((AXFR, BUDGET))

    domain_summaries = [
        (domain, domain_covered[domain_id[domain]], total)
        for domain, total in zip(domains, domain_total)
    ]
    return retained, used_bytes, results, domain_summaries
