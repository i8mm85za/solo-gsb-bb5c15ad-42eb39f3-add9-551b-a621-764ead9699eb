# DNS SOA 序列号推进审计 API

基于 **Python + FastAPI**，按 [RFC 1982](https://www.rfc-editor.org/rfc/rfc1982) 序列号环形算术，
对多个 DNS 副本当前持有的 32 位无符号 SOA 序列号与拟发布值进行推进审计，
推导最优发布候选，规划各副本的 IXFR / AXFR 传输，
并在字节预算内选择要保留的增量集合。

## 一条命令启动（Docker Compose）

```bash
docker compose up --build -d
```

服务地址（端口 **8000**）：

- 健康检查：<http://localhost:8000/health>
- Swagger 交互文档：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>

停止服务：

```bash
docker compose down
```

> 无 Docker 时本地运行：`pip install -r requirements.txt && uvicorn app.main:app --host 0.0.0.0 --port 8000`

## RFC 1982 判定规则

序列号空间为模 2³² 的环。定义前向模距离：

```
distance = (proposed - current) mod 2^32
```

| 距离 | 关系 `relation` | 含义 |
|---|---|---|
| `0` | `equal` | 相等 |
| `1 ≤ distance < 2^31` | `newer` | 严格更新、可比较 |
| `distance = 2^31` | **`ambiguous`** | 正对点，RFC 1982 明确规定**不可比较** |
| `2^31 < distance < 2^32` | `older` | 回退（等价于 proposed 落后 current） |

`relation` 不是 `newer` 的副本都会列入 `blocking_replicas`（阻塞副本）。

## API

### `POST /audit` — 审计拟发布值

请求：

```json
{
  "proposed": 2024092101,
  "replicas": [
    {"name": "ns-a", "current": 2024092100},
    {"name": "ns-b", "current": 2024092050},
    {"name": "ns-c", "current": 2024092102}
  ]
}
```

响应（每个副本逐一给出关系、前向模距离、是否阻塞；并汇总阻塞副本名）：

```json
{
  "proposed": 2024092101,
  "results": [
    {"name": "ns-a", "current": 2024092100, "proposed": 2024092101,
     "relation": "newer", "forward_distance": 1, "blocking": false},
    {"name": "ns-b", "current": 2024092050, "proposed": 2024092101,
     "relation": "newer", "forward_distance": 51, "blocking": false},
    {"name": "ns-c", "current": 2024092102, "proposed": 2024092101,
     "relation": "older", "forward_distance": 4294967295, "blocking": true}
  ],
  "blocked": true,
  "blocking_replicas": ["ns-c"]
}
```

正对点示例（差值正好 2³¹，必为 `ambiguous`）：

```bash
curl -s http://localhost:8000/audit -H 'Content-Type: application/json' -d '{
  "proposed": 2147483648,
  "replicas": [{"name": "ns-a", "current": 0}]
}'
# relation: "ambiguous", forward_distance: 2147483648, blocking: true
```

回绕示例（`4294967295 -> 0` 是合法的严格推进）：

```bash
curl -s http://localhost:8000/audit -H 'Content-Type: application/json' -d '{
  "proposed": 0,
  "replicas": [{"name": "ns-a", "current": 4294967295}]
}'
# relation: "newer", forward_distance: 1
```

### `POST /propose` — 推导最优候选

在环形空间中寻找一个对**所有副本**都严格更新且可比较（对每个副本均为 `newer`）的候选值，
最小化 **最大前向模距离** `max_i (proposed - current_i) mod 2^32`；
若多个候选同分，取**数值最小**者。

```bash
curl -s http://localhost:8000/propose -H 'Content-Type: application/json' -d '{
  "replicas": [
    {"name": "ns-a", "current": 100},
    {"name": "ns-b", "current": 150}
  ]
}'
```

```json
{
  "proposed": 151,
  "max_forward_distance": 51,
  "results": [
    {"name": "ns-a", "current": 100, "relation": "newer", "forward_distance": 51},
    {"name": "ns-b", "current": 150, "relation": "newer", "forward_distance": 1}
  ]
}
```

当各副本当前序列号跨越半环、不存在任何单一值能同时严格推进时（例如存在相差正好 2³¹ 的正对点，
或最宽跨度达到 2³¹），返回 **409 Conflict**：

```json
{
  "detail": {
    "error": "no_valid_proposal",
    "message": "不存在能对所有副本都严格更新且可比较的候选值：副本当前序列号跨越半环（含相差 2^31 的正对点）"
  }
}
```

### `POST /transfer-plan` — 规划 IXFR / AXFR 传输

给定目标 SOA 序列号、各副本当前值与一组**无序** IXFR 增量（`from_serial`、`to_serial`），
先对增量集合做整体校验，再为每个副本沿增量链逐跳追踪，规划增量传输（IXFR）或全量传输（AXFR）。

**链路匹配遵循 RFC 1982 环形顺序**：只沿增量自身的 `from_serial -> to_serial` 边前进，
边的合法性以环形先后关系判定（`4294967295 -> 0` 是合法推进），**不按整数大小排序**。

- 完全相同的重复增量自动去重；
- 任一增量的 `to_serial` 不是 `from_serial` 的严格更新（`equal` / `older` / 正对点 `ambiguous`），
  返回 **422**（`error=delta_not_strictly_newer`）；
- 同一 `from_serial` 指向不同 `to_serial` 的**分叉**，返回 **422**（`error=delta_fork`）。

每个副本的结果 `mode`：

| `mode` | 条件 | `reason` |
|---|---|---|
| `up_to_date` | 当前值已等于目标 | `null` |
| `ixfr` | 存在从当前值到目标的唯一完整增量链（无序输入会被排序到 `ordered_deltas`） | `null` |
| `axfr` | 链路存在**缺口** `gap`（某序列号没有对应增量） | `"gap"` |
| `axfr` | 沿链行进重复经过某序列号，形成**环路** `loop` | `"loop"` |
| `axfr` | 当前值与目标在 RFC 1982 下**不可比较**（前向距离恰为 2³¹） | `"target_incomparable"` |

> 校验后每个 `from_serial` 至多有一个后继，因此任何完整链都是唯一的。
> 只要存在从副本出发能实际连通目标的完整链（即便链跨过 2³² 回绕点、目标在环上更旧），
> 即为 `ixfr`；连不通才按缺口 / 环路 / 不可比较降级为 `axfr`。

请求（注意增量是乱序的，且含一条重复）：

```bash
curl -s http://localhost:8000/transfer-plan \
  -H 'Content-Type: application/json' \
  -d '{
    "target": 5,
    "replicas": [
      {"name": "ns-a", "current": 5},
      {"name": "ns-b", "current": 1},
      {"name": "ns-c", "current": 7}
    ],
    "deltas": [
      {"from_serial": 4, "to_serial": 5},
      {"from_serial": 1, "to_serial": 2},
      {"from_serial": 3, "to_serial": 4},
      {"from_serial": 2, "to_serial": 3},
      {"from_serial": 2, "to_serial": 3}
    ]
  }'
```

响应（`results` 保持副本的输入顺序）：

```json
{
  "target": 5,
  "results": [
    {"name": "ns-a", "current": 5, "target": 5,
     "mode": "up_to_date", "reason": null, "ordered_deltas": []},
    {"name": "ns-b", "current": 1, "target": 5,
     "mode": "ixfr", "reason": null,
     "ordered_deltas": [
       {"from_serial": 1, "to_serial": 2},
       {"from_serial": 2, "to_serial": 3},
       {"from_serial": 3, "to_serial": 4},
       {"from_serial": 4, "to_serial": 5}
     ]},
    {"name": "ns-c", "current": 7, "target": 5,
     "mode": "axfr", "reason": "gap", "ordered_deltas": []}
  ]
}
```

跨 2³² 回绕的增量链同样按环形顺序排序（`4294967294 -> 4294967295 -> 0 -> 1` 是合法 IXFR 链）；
副本当前值与目标相差正好 2³¹ 时即使有可连通的边也返回 `axfr / target_incomparable`：

```bash
curl -s http://localhost:8000/transfer-plan \
  -H 'Content-Type: application/json' \
  -d '{
    "target": 2147483648,
    "replicas": [{"name": "ns-a", "current": 0}],
    "deltas": [
      {"from_serial": 0, "to_serial": 1},
      {"from_serial": 1, "to_serial": 2147483648}
    ]
  }'
# {"...","mode":"axfr","reason":"target_incomparable","ordered_deltas":[]}
```

### `POST /retention-plan` — 字节预算内的增量保留规划

给定目标 SOA 序列号、一组**带字节数**的无序 IXFR 增量（`from_serial`、`to_serial`、`bytes`）、
各副本当前值与非负 `priority`（**至多 12 个副本**），以及总字节预算 `budget`：
在 `used_bytes <= budget` 的约束下选择要**保留**的增量子集，使
**拥有到目标完整链的副本 priority 总和最大**，并依次按以下规则确定唯一结果：

1. 被覆盖副本（`ixfr` + `up_to_date`）的 **priority 总和最大**；
2. 被覆盖**副本数最多**；
3. **占用字节最少**；
4. 按 `(from_serial, to_serial)` 升序排列的**增量序列字典序最小**。

增量校验复用 `/transfer-plan` 的环形链路规则（非严格 newer、分叉均为 422），并新增：
同一 `(from_serial, to_serial)` 携带**不同 `bytes`** 时返回 **422**（`error=delta_bytes_conflict`）；
完全相同的重复增量去重。副本数超过 12 返回 **422**（`error=too_many_replicas`）。

每个副本的结果 `mode`（复用 `plan_transfer` 在**完整**增量集合上的判定）：

| `mode` | 条件 | `reason` |
|---|---|---|
| `up_to_date` | 当前值已等于目标（不占用预算，恒视为已覆盖） | `null` |
| `ixfr` | 到目标的完整链被保留集覆盖 | `null` |
| `axfr` | 完整集合下即存在**缺口**（保留任何子集都无法连通） | `"gap"` |
| `axfr` | 完整集合下即形成**环路** | `"loop"` |
| `axfr` | 当前值与目标在 RFC 1982 下**不可比较**（前向距离恰为 2³¹） | `"target_incomparable"` |
| `axfr` | 完整集合下存在完整链，但因**预算**未被保留 | `"budget"` |

> 保留集恰为被覆盖副本需求集（各自完整链上的增量）之并——共享链的副本只计一次字节，
> 对覆盖无贡献的增量不会被保留。**预算无法保留任何完整链时仍返回 200**
> （`retained_deltas` 为空、`used_bytes` 为 0，相关副本标记 `axfr / budget`）。

请求（预算 12 恰好保住 `ns-a` 的完整链，`ns-b` 因共享链后缀被一并覆盖）：

```bash
curl -s http://localhost:8000/retention-plan \
  -H 'Content-Type: application/json' \
  -d '{
    "target": 4,
    "budget": 12,
    "replicas": [
      {"name": "ns-a", "current": 1, "priority": 10},
      {"name": "ns-b", "current": 3, "priority": 5},
      {"name": "ns-c", "current": 4, "priority": 100},
      {"name": "ns-d", "current": 7, "priority": 1}
    ],
    "deltas": [
      {"from_serial": 3, "to_serial": 4, "bytes": 6},
      {"from_serial": 1, "to_serial": 2, "bytes": 3},
      {"from_serial": 2, "to_serial": 3, "bytes": 3},
      {"from_serial": 1, "to_serial": 2, "bytes": 3}
    ]
  }'
```

响应（`retained_deltas` 按 `(from_serial, to_serial)` 升序，`results` 保持副本输入顺序）：

```json
{
  "target": 4,
  "budget": 12,
  "used_bytes": 12,
  "retained_deltas": [
    {"from_serial": 1, "to_serial": 2, "bytes": 3},
    {"from_serial": 2, "to_serial": 3, "bytes": 3},
    {"from_serial": 3, "to_serial": 4, "bytes": 6}
  ],
  "results": [
    {"name": "ns-a", "current": 1, "target": 4, "mode": "ixfr", "reason": null},
    {"name": "ns-b", "current": 3, "target": 4, "mode": "ixfr", "reason": null},
    {"name": "ns-c", "current": 4, "target": 4, "mode": "up_to_date", "reason": null},
    {"name": "ns-d", "current": 7, "target": 4, "mode": "axfr", "reason": "gap"}
  ]
}
```

预算不足时（例如上例中 `budget` 改为 11）仍返回 200：

```json
{
  "target": 4,
  "budget": 11,
  "used_bytes": 6,
  "retained_deltas": [
    {"from_serial": 3, "to_serial": 4, "bytes": 6}
  ],
  "results": [
    {"name": "ns-a", "current": 1, "target": 4, "mode": "axfr", "reason": "budget"},
    {"name": "ns-b", "current": 3, "target": 4, "mode": "ixfr", "reason": null},
    {"name": "ns-c", "current": 4, "target": 4, "mode": "up_to_date", "reason": null},
    {"name": "ns-d", "current": 7, "target": 4, "mode": "axfr", "reason": "gap"}
  ]
}
```

### 输入校验（422）

以下情况返回 **422 Unprocessable Entity**：

- 序列号越界：必须为整数且在 `[0, 4294967295]`（即 `[0, 2^32-1]`）；
- 副本名重复（`/audit`、`/propose`、`/transfer-plan`、`/retention-plan` 均检查）；
- 副本名为空 / `replicas` 为空数组；
- `/transfer-plan` 额外检查：增量非严格 newer（`delta_not_strictly_newer`）、
  同一 `from_serial` 分叉（`delta_fork`）；
- `/retention-plan` 额外检查：副本数超过 12（`too_many_replicas`）、
  相同增量携带不同 `bytes`（`delta_bytes_conflict`）、
  `bytes` / `priority` / `budget` 为负数或非整数。

## 算法说明

`/propose` 中，每个当前值 `c` 的合法候选范围是开弧
`{c+1, …, c+2³¹−1}（mod 2³²）`；所有开弧的交集非空时必为一段连续弧，
最优解必位于弧的最小元素，即某个 `(c + 1) mod 2³²`。
因此只需枚举各副本当前值的“紧邻后继”，检查其对所有副本的前向距离均在 `[1, 2³¹−1]`，
再按 `(最大前向距离, 候选数值)` 取最小即可。

`/transfer-plan` 中，增量先经 `build_delta_chain` 校验去重为
`from_serial -> to_serial` 的单后继映射（故完整链唯一，不存在分叉分支选择）；
`plan_transfer` 从副本当前值出发逐跳取后继，**不做整数排序**：
相等即 `up_to_date`，两端前向距离恰为 2³¹ 即 `target_incomparable`；
否则沿边行进直到抵达目标（`ixfr`，记录沿途有序增量）、
找不到后继（`gap`）或重复经过已访问序列号（`loop`）。
注意严格 newer 的边构成的合法环路只能跨 2³² 回绕（至少 3 条边、各边前向距离之和恰为 2³²），
两条边无法在不违反严格 newer 的前提下成环。

`/retention-plan` 中，带字节数的增量先经 `build_sized_delta_chain` 校验去重为
`from_serial -> to_serial` 映射与 `(from_serial, to_serial) -> bytes` 映射；
`plan_retention` 先在**完整**集合上对每个副本跑 `plan_transfer`：
`up_to_date` 恒被覆盖（零成本），`ixfr` 副本的链上增量即其**需求集**，
`gap` / `loop` / `target_incomparable` 在完整集合下即不可达、原因保持。
由于链是单后继映射，副本被子集覆盖当且仅当其需求集被该子集包含，
因此最优保留集必为某组副本需求集之并；副本数不超过 12，
枚举全部副本子集（至多 2¹² = 4096 个，位掩码按位 OR 求并集）即可精确求得
`(priority 总和, 覆盖副本数, -占用字节)` 字典序最大的子集，
再在同分子集中取增量序列（按 `(from_serial, to_serial)` 升序）字典序最小者。
空子集恒可行（`budget >= 0`），故预算再小也能正常返回 200。

## 测试

核心环形算术不依赖 FastAPI，可直接用标准库运行（另含小位宽空间对暴力穷举的对照测试，
`/transfer-plan` 的去重、分叉、缺口、环路、回绕与正对点测试，
以及 `/retention-plan` 的字节数冲突、共享链并集、三级平局裁决、
`budget` 原因与零预算测试）：

```bash
python3 -m unittest discover -s tests -v
```

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI 路由与异常（/audit、/propose、/transfer-plan、/retention-plan）
│   ├── schemas.py     # 请求/响应模型与 422 校验
│   └── serial.py      # RFC 1982 环形算术、增量链规划与保留规划（纯函数，零第三方依赖）
├── tests/test_serial.py
├── tests/test_transfer_plan.py
├── tests/test_retention_plan.py
├── requirements.txt
├── Dockerfile
├── compose.yaml
└── README.md
```
