"""FastAPI 应用入口：DNS SOA 序列号推进审计 API。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse

from .schemas import (
    AuditItem,
    AuditRequest,
    AuditResponse,
    OrderedDelta,
    ProposeItem,
    ProposeRequest,
    ProposeResponse,
    RetainedDelta,
    RetentionPlanItem,
    RetentionPlanRequest,
    RetentionPlanResponse,
    TransferPlanItem,
    TransferPlanRequest,
    TransferPlanResponse,
)
from .serial import (
    MAX_RETENTION_REPLICAS,
    build_delta_chain,
    build_sized_delta_chain,
    compare_serial,
    DeltaValidationError,
    forward_distance,
    plan_retention,
    plan_transfer,
    propose_serial,
)

app = FastAPI(
    title="DNS SOA 序列号推进审计 API",
    description=(
        "基于 RFC 1982 环形算术，对多个 DNS 副本的 SOA 序列号推进进行审计，"
        "推导对所有副本都严格更新的最佳拟发布值，规划增量 / 全量传输，"
        "并在字节预算内选择要保留的增量集合。"
    ),
    version="1.1.0",
)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _check_duplicate_names(names: list[str]) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise HTTPException(
                status_code=422,
                detail={"error": "duplicate_replica_name", "name": name},
            )
        seen.add(name)


@app.post("/audit", response_model=AuditResponse)
def audit(request: AuditRequest) -> AuditResponse:
    """逐一判定拟发布值相对各副本当前序列号的关系。

    * newer：严格更新且可比较（1 <= 前向距离 < 2^31）
    * equal：相等（距离 0）
    * older：回退（2^31 < 距离 < 2^32）
    * ambiguous：差值正好 2^31（正对点，RFC 1982 规定不可比较）

    非 newer 的副本均为阻塞副本。
    """
    _check_duplicate_names([r.name for r in request.replicas])

    results = []
    blocking_replicas = []
    for replica in request.replicas:
        relation = compare_serial(replica.current, request.proposed)
        distance = forward_distance(replica.current, request.proposed)
        blocking = relation != "newer"
        if blocking:
            blocking_replicas.append(replica.name)
        results.append(
            AuditItem(
                name=replica.name,
                current=replica.current,
                proposed=request.proposed,
                relation=relation,
                forward_distance=distance,
                blocking=blocking,
            )
        )

    return AuditResponse(
        proposed=request.proposed,
        results=results,
        blocked=bool(blocking_replicas),
        blocking_replicas=blocking_replicas,
    )


@app.post("/propose", response_model=ProposeResponse)
def propose(request: ProposeRequest) -> ProposeResponse:
    """推导对所有副本都严格更新且可比较的候选序列号。

    最小化最大前向距离；同分取数值最小者。
    若各副本当前序列号无法被任意单一值同时严格推进（例如存在相差 2^31
    的正对点，或序列跨越超过半环），返回 409。
    """
    _check_duplicate_names([r.name for r in request.replicas])

    outcome = propose_serial([r.current for r in request.replicas])
    if outcome is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "no_valid_proposal",
                "message": (
                    "不存在能对所有副本都严格更新且可比较的候选值："
                    "副本当前序列号跨越半环（含相差 2^31 的正对点）"
                ),
            },
        )

    proposed, max_distance = outcome
    results = [
        ProposeItem(
            name=replica.name,
            current=replica.current,
            relation=compare_serial(replica.current, proposed),
            forward_distance=forward_distance(replica.current, proposed),
        )
        for replica in request.replicas
    ]

    return ProposeResponse(
        proposed=proposed,
        max_forward_distance=max_distance,
        results=results,
    )


@app.post("/transfer-plan", response_model=TransferPlanResponse)
def transfer_plan(request: TransferPlanRequest) -> TransferPlanResponse:
    """规划各副本到目标 SOA 序列号的增量 / 全量传输。

    先对无序增量集合做整体校验（422）：

    * 每条增量的 to_serial 必须按 RFC 1982 严格新于 from_serial；
    * 同一 from_serial 指向不同 to_serial 的分叉增量被拒绝；
    * 完全相同的重复增量去重。

    再为每个副本沿增量链逐跳追踪（遵循 RFC 1982 环形顺序，不做整数排序）：

    * 当前值已等于目标：``up_to_date``；
    * 存在唯一完整链抵达目标：``ixfr``，并返回按序排列的 ordered_deltas；
    * 存在缺口（gap）、环路（loop）或目标与当前值不可比较
      （target_incomparable，前向距离恰为 2^31）：``axfr`` 并给出 reason。
    """
    _check_duplicate_names([r.name for r in request.replicas])

    raw_deltas = [(d.from_serial, d.to_serial) for d in request.deltas]
    try:
        chain = build_delta_chain(raw_deltas)
    except DeltaValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.detail)

    results = []
    for replica in request.replicas:
        mode, reason, ordered = plan_transfer(
            replica.current, request.target, chain
        )
        results.append(
            TransferPlanItem(
                name=replica.name,
                current=replica.current,
                target=request.target,
                mode=mode,
                reason=reason,
                ordered_deltas=[
                    OrderedDelta(from_serial=frm, to_serial=to)
                    for frm, to in ordered
                ],
            )
        )

    return TransferPlanResponse(target=request.target, results=results)


@app.post("/retention-plan", response_model=RetentionPlanResponse)
def retention_plan(request: RetentionPlanRequest) -> RetentionPlanResponse:
    """在字节预算内选择要保留的增量集合（副本数不超过 12）。

    先对带字节数的无序增量集合做整体校验（422）：

    * 每条增量的 to_serial 必须按 RFC 1982 严格新于 from_serial；
    * 完全相同的重复增量去重；
    * 相同增量（同一 from_serial、to_serial）携带不同 bytes 即冲突，拒绝；
    * 同一 from_serial 指向不同 to_serial 的分叉增量被拒绝。

    再按环形链路规则为每个副本求出到目标的完整链（需求集），在
    ``used_bytes <= budget`` 的约束下选择保留集，依次：

    1. 最大化拥有到目标完整链的副本 priority 总和；
    2. 最大化被覆盖副本数；
    3. 最小化占用字节数；
    4. 取增量序列（按 from_serial、to_serial 升序）字典序最小者。

    逐副本标记：完整链被保留为 ``ixfr``；当前值已等于目标为
    ``up_to_date``；否则 ``axfr`` 并给出 ``gap`` / ``loop`` /
    ``target_incomparable`` / ``budget``（完整集合下有完整链但因预算
    未被保留）原因。预算无法保留任何完整链时仍返回 200（保留集为空）。
    """
    _check_duplicate_names([r.name for r in request.replicas])
    if len(request.replicas) > MAX_RETENTION_REPLICAS:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "too_many_replicas",
                "message": f"副本数不得超过 {MAX_RETENTION_REPLICAS}",
                "limit": MAX_RETENTION_REPLICAS,
                "got": len(request.replicas),
            },
        )

    raw_deltas = [(d.from_serial, d.to_serial, d.bytes) for d in request.deltas]
    try:
        chain, sizes = build_sized_delta_chain(raw_deltas)
    except DeltaValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.detail)

    retained, used_bytes, modes = plan_retention(
        request.target,
        [(r.current, r.priority) for r in request.replicas],
        chain,
        sizes,
        request.budget,
    )

    results = [
        RetentionPlanItem(
            name=replica.name,
            current=replica.current,
            target=request.target,
            mode=mode,
            reason=reason,
        )
        for replica, (mode, reason) in zip(request.replicas, modes)
    ]

    return RetentionPlanResponse(
        target=request.target,
        budget=request.budget,
        used_bytes=used_bytes,
        retained_deltas=[
            RetainedDelta(from_serial=frm, to_serial=to, bytes=sizes[(frm, to)])
            for frm, to in retained
        ],
        results=results,
    )
