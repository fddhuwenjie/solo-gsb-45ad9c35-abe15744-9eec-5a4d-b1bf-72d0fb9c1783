# 馆藏展品光照剂量核算 API

基于 **Python + FastAPI + SQLite** 的馆藏展品光照剂量核算服务：

- 登记展品的**累计光照上限**（lux·h）与**最大采样间隔**（秒）；
- 接收**乱序 / 补传**的照度读数，按时间顺序以**梯形法**累计 lux·h；
- 读数 ID **幂等**：重复上报不重复计入，同 ID 不同数据判为冲突；
- 超过最大采样间隔的缺测段**不插值**，窗口内未覆盖时长单列 `unknown_seconds`；
- 时段查询返回**窗口已知剂量**，以及按**截至窗口末全历史**核算的剩余额度与
  首次越线时刻（缩窄窗口不改变首次越线时刻），补传后查询即自动重算；
- 支持**展陈开放日历**：IANA 时区 + 每周多个开放时段 + 按本地日期替换周规则的
  例外时段，正确处理跨午夜与夏令时；剂量查询加 `calendar=display` 后仅在开放
  区间内裁剪积分，闭馆时长单列 `closed_seconds`；
- 支持**重复展品档案归并**：预演列出元数据差异与读数 ID 碰撞，逐条解决冲突后
  在单个事务内提交，来源 ID 归并后成为目标 ID 的**永久别名**，所有查询与上报
  自动解析到规范档案（归并保留目标档案的展陈日历）；
- 支持**照度采集质量诊断**：在规范档案的完整时间线上识别缺测（`gap`）、
  突变（`jump`）、卡死（`stuck`）与同一时刻读数冲突（`timestamp_conflict`），
  事件边界不随查询窗口缩窄而改变；诊断为纯只读操作。
- 支持**多来源读数仲裁**：上报读数可带 `source_id`，同一展品同一采样时刻的
  不同来源全部留存；按**带生效时刻的来源优先级版本**仲裁权威值——最高优先级
  来源胜出，最高级并列且照度不同的时刻保持冲突、只保留在权威时间线查询中；
  冲突时刻**不作为剂量积分端点**（两侧权威点绝不直接相连，普通剂量与
  `calendar=display` 开放时段都在冲突处断开），**也不作为诊断输入**
  （诊断连续链在两侧重置且不生成 `timestamp_conflict`）；配置更新须携带
  `expected_version`（过期返回 409），调整只从生效时刻起影响重算，历史收据与
  原始读数绝不改写；提供权威时间线查询，旧请求不带 `source_id` 时保持原行为。
- 支持 **RFID 馆藏盘点对账**：固定时间窗内按区域保留最高 RSSI，以最后一个
  有事件的窗口判定展品位置，多区域信号差不足阈值记为**位置不明**；稳定列出
  缺失 / 错位 / 意外出现 / 位置不明四类差异；盘点轮次与规范化请求摘要绑定，
  相同请求重放原结果、不同请求 409、并发提交只固化一份结果；对账不改写照度
  读数，也不触碰存证收据链。
- 支持**展品应急撤离编排**：给出计划起点、有向通道图、出口与每条通道的整数
  通行秒数、并发容量及半开封闭时段，按**优先级降序、规范 ID 升序**逐件做
  **时变最早到达搜索**选择出口与路径；通道占用按半开区间 `[进入, 离开)`
  计数，容量只看**同一时刻的实际并发**（多条占用彼此错峰、窗口内任一瞬间
  并发未满时仍可进入，而不是按与新窗口重叠的占用条数计数），加入新占用会
  超过容量或进入时刻落在封闭时段内时等待到**首个可行时刻**（每次因容量或
  封闭推进候选时刻后都重新检查两类约束，不会跳过更早空档）；到达时间相同
  按**完整路径字典序**决胜。展品 ID 先经别名解析为规范档案，重复项、非法
  图或无出口路径返回 422；编排为**纯只读计算**，不写读数、日历、盘点轮次
  或收据链。
- 每次批量上报在**写入读数的同一事务**内生成**存证收据**：按稳定规范序列化
  记录请求内容与每条 `inserted` / `duplicate` / `conflict` 结果，并以
  SHA-256 链哈希衔接上一张收据；并发上报形成唯一连续序号，失败事务不留
  收据；提供收据查询与整链校验，档案归并保留双方既有链、归并后新上报继续
  接入目标原链，旧哈希绝不重写。
- 支持**照度上报批次更正（作废）**：提交规范档案（可用别名）、收据
  `receipt_id`、`request_key` 与原因，只作废该收据中**实际插入**且仍归属
  该档案的读数；收据链、归并状态或读数归属已变化则整次 **409**；作废与
  更正记录在一个 SQLite 事务内幂等落库，重复 `request_key` 重放原结果、
  换目标 409；剂量查询、采集质量诊断与读数列表立即排除已作废读数并按剩余
  时间线重算，收据内容、序号与链哈希保持不变（查询收据时标出作废状态）。
- 支持**照度异常处置单**：创建时提交展品 ID（可用别名）、时间窗、诊断阈值
  与 `request_key`，服务在**一个 SQLite 事务**内按未作废权威时间线重算
  诊断事件与 `calendar=display` 剂量，连同**收据链末端哈希**固化快照并
  返回 `digest`；处置单只能从 `open` 结案为 `resolved` 或 `accepted`——
  提交 `expected_digest`、决定与说明，读数、作废、来源优先级、日历或收据链
  变化导致摘要不同则 **409 且不写入**；`resolved` 须给出涉及的收据或更正
  `request_key` 并证明重算后原事件消失、剂量未越限，`accepted` 保留事件并
  记录理由；相同 `request_key` 重放原结果，快照不可变。
- 支持**展厅开放批次合规核验**：提交 `request_key`、核验时间窗、盘点
  `round_id`、展品 ID 清单与诊断阈值，服务在**一个 SQLite 写事务**内解析
  别名、读取该轮次固化结果、逐件重算截至窗口末的 `calendar=display` 剂量
  与完整诊断事件并检查未结处置单；只有全部展品都定位到预期区域、窗口内无
  gap / jump / stuck、开放剂量未越限且无未结处置单时，才固化包含各项证据
  摘要的**合规凭证**；任一失败返回逐件原因且**不写入**；同一 `request_key`
  仅在规范化输入与当时摘要一致时重放，否则 **409**。
- 提供**可验证的 SQLite 在线快照**：`sqlite3.Connection.backup` 热备生成
  事务一致副本（临时文件 → SHA-256 + 清单 → 原子重命名发布），支持
  创建 / 列表 / 下载，并可在独立只读连接上核对哈希、执行
  `integrity_check` 与 `foreign_key_check`；并发创建只允许一个，其余 409，
  失败或未完成的文件绝不出现在列表中。

## 快速开始（Docker Compose 一键启动）

```bash
docker compose up --build
```

启动后：

| 入口 | 地址 |
|---|---|
| API 服务 | http://localhost:8000 |
| 交互式文档（Swagger UI） | http://localhost:8000/docs |
| 健康检查 | http://localhost:8000/healthz |

服务监听 **8000 端口**（宿主机映射 `8000:8000`，可在 `docker-compose.yml` 中修改）。
SQLite 数据文件存放在命名卷 `lightdose-data`（容器内 `/data/lightdose.db`），
快照存放在**独立命名卷** `lightdose-snapshots`（容器内 `/snapshots`），重启不丢数据。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/exhibits` | 登记展品（上限 + 最大采样间隔） |
| GET | `/exhibits` / `/exhibits/{id}` | 查询展品 |
| PUT | `/exhibits/{id}/calendar` | 整体设置展陈开放日历（全量替换） |
| GET | `/exhibits/{id}/calendar` | 查询展陈开放日历（未配置 → 404） |
| DELETE | `/exhibits/{id}/calendar` | 删除展陈开放日历（幂等，204） |
| POST | `/exhibits/{id}/readings` | 批量上报读数（幂等，支持乱序/补传与 `source_id` 多来源；同事务生成存证收据） |
| GET | `/exhibits/{id}/readings` | 按时间顺序列出读数 |
| PUT | `/exhibits/{id}/source-priority` | 发布来源优先级版本（带生效时刻；`expected_version` 乐观并发，过期 409） |
| GET | `/exhibits/{id}/source-priority` | 查询来源优先级配置（全部版本，按版本号升序） |
| GET | `/exhibits/{id}/timeline` | 权威时间线查询：每个采样时刻的仲裁结果（权威值 / 保持冲突） |
| GET | `/exhibits/{id}/receipts` | 列出存证收据链（按序号升序，别名自动解析） |
| GET | `/exhibits/{id}/receipts/{seq}` | 查询单张存证收据（已作废批次在 `voids` 标出） |
| POST | `/exhibits/{id}/receipts/corrections` | 照度上报批次更正：作废收据中实际插入的读数（幂等，request_key 重放/409） |
| GET | `/exhibits/{id}/receipts/verify` | 整链校验规范档案的收据链 |
| POST | `/exhibits/{id}/illumination-tickets` | 创建照度异常处置单：单事务内重算诊断事件 + `calendar=display` 剂量并固化快照，返回 digest（request_key 幂等） |
| GET | `/exhibits/{id}/illumination-tickets` | 列出规范档案的全部处置单（别名自动解析） |
| GET | `/illumination-tickets/{ticket_id}` | 查询单张处置单；open 状态附带实时重算的 `current_digest` |
| POST | `/illumination-tickets/{ticket_id}/close` | 结案：open → resolved / accepted（expected_digest 核对，不一致 409 不写入） |
| POST | `/compliance-checks` | 展厅开放批次合规核验：单事务核验，全部通过才固化合规凭证（request_key 幂等；失败逐件原因、不写入） |
| GET | `/compliance-checks/{request_key}` | 取回已固化的合规凭证（未固化 → 404） |
| GET | `/receipts/chains/{chain_id}` | 按链标识直接读取收据链（不解析别名） |
| GET | `/receipts/chains/{chain_id}/verify` | 按链标识整链校验（归并后来源链核查） |
| POST | `/exhibits/{id}/readings/diagnose` | 照度采集质量诊断（gap / jump / stuck / 时间冲突，只读） |
| GET | `/exhibits/{id}/dose?start=...&end=...` | 时段剂量核算（可加 `calendar=display`） |
| POST | `/merges/preview` | 重复档案归并预演（差异 + 碰撞 + 版本令牌） |
| POST | `/merges/commit` | 按令牌与裁决提交归并（单事务，令牌可重放） |
| POST | `/inventory-rounds/{round_id}/reconcile` | 执行 RFID 盘点对账并固化（摘要绑定，重放/409） |
| GET | `/inventory-rounds/{round_id}` | 取回已固化的盘点对账结果（未对账 → 404） |
| POST | `/exhibits/{id}/area-handovers` | 跨盘点轮次馆藏区域交接（单事务核验+固化；首次建链、只能续接，并发最多一笔生效） |
| GET | `/exhibits/{id}/area-handovers` | 当前区域 + 按轮次时间排序的交接历史（别名自动解析） |
| POST | `/evacuation-plan` | 展品应急撤离编排（时变最早到达、容量等待、封闭绕行；纯只读） |
| POST | `/snapshots` | 创建 SQLite 在线快照（事务一致；并发仅一个，其余 409） |
| GET | `/snapshots` | 列出已发布快照（失败 / 未完成文件不出现） |
| GET | `/snapshots/{seq}` | 查询单个快照清单（编号 / 时间 / 页数 / 字节数 / SHA-256） |
| GET | `/snapshots/{seq}/download` | 下载快照数据库文件（单一自包含 `.db`） |
| GET | `/snapshots/{seq}/verify` | 只读校验快照：哈希 + `integrity_check` + `foreign_key_check` |

> **请求 ID 与规范 ID**：所有档案相关响应都同时返回 `requested_id`（请求中
> 使用的 ID）与 `canonical_id`（实际档案 ID）。档案未归并时两者相同；归并后用
> 来源别名访问时 `requested_id` 为别名、`canonical_id` 为目标档案。

### 1. 登记展品

```bash
curl -s -X POST http://localhost:8000/exhibits \
  -H 'Content-Type: application/json' \
  -d '{"name": "唐代绢画", "dose_limit_luxh": 150, "max_interval_seconds": 3600}'
```

```json
{"requested_id": "9f2c...", "canonical_id": "9f2c...", "name": "唐代绢画",
 "dose_limit_luxh": 150.0, "max_interval_seconds": 3600.0,
 "created_at": "2026-09-20T08:00:00.000Z"}
```

### 2. 上报读数（乱序、可重传）

```bash
curl -s -X POST http://localhost:8000/exhibits/<id>/readings \
  -H 'Content-Type: application/json' \
  -d '{"readings": [
        {"id": "r2", "ts": "2026-09-20T09:00:00Z", "lux": 100},
        {"id": "r1", "ts": "2026-09-20T08:00:00Z", "lux": 100},
        {"id": "r3", "ts": "2026-09-20T10:00:00Z", "lux": 200}
      ]}'
```

```json
{"requested_id": "...", "canonical_id": "...", "inserted": 3,
 "duplicates": 0, "conflicts": 0,
 "results": [{"id": "r2", "status": "inserted"}, ...],
 "receipt": {"chain_id": "...", "seq": 1,
             "prev_hash": "000...000", "content_hash": "...", "chain_hash": "...",
             "content": {"request": {...}, "outcome": {...}}, ...}}
```

幂等规则（读数 ID 为幂等键，按展品隔离）：

- 同 ID **同数据**重传 → `duplicate`，忽略，不重复计入；
- 同 ID **不同数据** → `conflict`，拒绝写入并在结果中标出；
- 时间缺省时区按 **UTC** 处理。

每条读数可带可选的 `source_id` 声明采集来源；`source_id` 属于读数数据
的一部分（同 ID 但来源不同同样判为 `conflict`）。同一采样时刻不同来源的
读数以不同读数 ID 全部留存，由来源优先级版本在查询时仲裁权威值，详见
下文「多来源读数仲裁」；不带 `source_id` 的旧式请求行为完全不变。

每次上报都会在写入读数的**同一事务**内生成一张存证收据（响应中的
`receipt` 字段），详见下文「照度上报存证（收据链）」。需要把整批读数
作废时见「照度上报批次更正（作废）」。

### 3. 时段剂量查询

```bash
curl -s 'http://localhost:8000/exhibits/<id>/dose?start=2026-09-20T08:00:00Z&end=2026-09-20T10:00:00Z'
```

```json
{
  "requested_id": "...",
  "canonical_id": "...",
  "start": "2026-09-20T08:00:00Z",
  "end": "2026-09-20T10:00:00Z",
  "dose_limit_luxh": 150.0,
  "known_dose_luxh": 250.0,
  "remaining_luxh": -100.0,
  "exceeded": true,
  "first_exceeded_at": "2026-09-20T09:24:51Z",
  "unknown_seconds": 0.0,
  "covered_seconds": 7200.0,
  "reading_count": 3
}
```

## 照度采集质量诊断

`POST /exhibits/{id}/readings/diagnose` 针对读数**时间线**做采集质量诊断，
帮助发现缺测、传感器突变、卡死与同一时刻的重复上报冲突。诊断是**纯只读**
操作：不写库，也不影响剂量核算结果；别名 ID 同样自动解析到规范档案。

请求体给出查询窗口与诊断参数：

| 字段 | 含义 |
|---|---|
| `start` / `end` | 查询窗口（含端点，ISO 8601，缺省时区按 UTC；`end` 必须晚于 `start`） |
| `max_rate_lux_per_second` | 照度最大允许变化速率（lux/秒，必须 > 0）：间隔合规但 `|Δlux|/Δt` 超限记为 `jump` |
| `stuck_tolerance_lux` | 卡死容差（lux，≥ 0）：相邻读数照度差不超过它才可能并入同一段卡死链 |
| `min_stuck_seconds` | 卡死最短持续时长（秒，必须 > 0）：平稳链首末累计时长达到它才记为 `stuck` |

事件类型与判定（在**规范档案的完整时间线**上形成，与窗口无关）：

- **同时刻合并 / 冲突**：同一时刻照度相同的多条读数先**合并**为时间线上的
  一个点（读数 ID 全部保留）；同一时刻出现**不同照度**（未配置来源优先级时的
  原始读数冲突）则产生 `timestamp_conflict`，该时刻有歧义、不落为任何链上的点，
  并从两侧中断连续链。**已配置来源优先级时**，最高级并列且照度不同的时刻属于
  **仲裁冲突**：它不进入诊断输入，连续链（gap / jump / stuck）只在其两侧静默
  重置，**不产生任何诊断事件**（尤其不产生 `timestamp_conflict`）——仲裁冲突
  状态只保留在权威时间线查询（`GET /timeline`）中；
- **`gap`（缺测）**：两个相邻无歧义读数的间隔**超过**展品登记的
  `max_interval_seconds`（恰好等于不算），缺测段两侧各自成链；
- **`jump`（突变）**：间隔合规，但变化速率 `|Δlux|/Δt` **超过**
  `max_rate_lux_per_second`（恰好等于不算）。间隔超限时只记 `gap`，
  不重复记 `jump`；
- **`stuck`（卡死）**：相邻照度差均不超过 `stuck_tolerance_lux`（恰好等于
  算容差内）的**最大**连续段，且首末读数累计时长 ≥ `min_stuck_seconds`。
  `gap`、`jump`、照度差超过容差的相邻对以及 `timestamp_conflict` 都会
  截断卡死链；不足最短时长的平稳段不产生事件。

### 4. 诊断请求与响应

```bash
curl -s -X POST http://localhost:8000/exhibits/<id>/readings/diagnose \
  -H 'Content-Type: application/json' \
  -d '{
    "start": "2026-09-20T08:00:00Z",
    "end":   "2026-09-20T18:00:00Z",
    "max_rate_lux_per_second": 1.0,
    "stuck_tolerance_lux": 2.0,
    "min_stuck_seconds": 1800
  }'
```

```json
{
  "requested_id": "...",
  "canonical_id": "...",
  "start": "2026-09-20T08:00:00Z",
  "end": "2026-09-20T18:00:00Z",
  "max_interval_seconds": 3600.0,
  "max_rate_lux_per_second": 1.0,
  "stuck_tolerance_lux": 2.0,
  "min_stuck_seconds": 1800.0,
  "events": [
    {"type": "stuck",
     "start": "2026-09-20T08:00:00Z", "end": "2026-09-20T11:00:00Z",
     "reading_ids": ["r1", "r2", "r3", "r4"],
     "duration_seconds": 10800.0, "lux_span": 2.0},
    {"type": "gap",
     "start": "2026-09-20T11:00:00Z", "end": "2026-09-20T13:00:00Z",
     "reading_ids": ["r4", "r5"], "interval_seconds": 7200.0},
    {"type": "jump",
     "start": "2026-09-20T13:00:00Z", "end": "2026-09-20T13:10:00Z",
     "reading_ids": ["r5", "r6"],
     "interval_seconds": 600.0, "rate_lux_per_second": 1.5},
    {"type": "timestamp_conflict",
     "start": "2026-09-20T14:00:00Z", "end": "2026-09-20T14:00:00Z",
     "reading_ids": ["r7", "r8"], "lux_span": 80.0}
  ]
}
```

事件字段：

| 字段 | 出现于 | 含义 |
|---|---|---|
| `start` / `end` | 全部 | 事件边界（ISO 8601）；`timestamp_conflict` 起点终点相同 |
| `reading_ids` | 全部 | 相关读数 ID：`gap`/`jump` 为两侧边界读数，`stuck` 为整段全部读数（含同时刻合并的重复读数），`timestamp_conflict` 为同时刻的全部冲突读数 |
| `interval_seconds` | `gap` / `jump` | 两条相邻读数的间隔（秒） |
| `rate_lux_per_second` | `jump` | 照度变化速率 `|Δlux|/Δt`（lux/秒） |
| `duration_seconds` | `stuck` | 首末读数累计持续时长（秒） |
| `lux_span` | `stuck` / `timestamp_conflict` | `stuck`：整段照度最大值减最小值；冲突：同时刻不同读数的照度跨度 |

**完整时间线 + 窗口相交筛选**：事件先基于全部已存读数形成，再只返回与
`[start, end]` **相交**（含端点相接）的事件。窗口只决定哪些事件被返回，
**缩窄窗口不会改变任何事件的原始边界、时长或相关读数 ID**——例如一个
跨越窗口边界的长时间卡死事件，在更窄的窗口中仍以完整边界返回。响应按
`(start, 类型)` 稳定排序（起点相同时依次为 `gap`、`jump`、`stuck`、
`timestamp_conflict`）。

## 多来源读数仲裁

同一件展品可能有多个采集来源（主传感器、备份传感器、巡检手持终端等）。
读数上报时可携带 `source_id` 声明来源；**同一展品同一采样时刻的不同来源
读数以不同读数 ID 全部留存**（读数 ID 仍是幂等键，`source_id` 是读数属性）。
查询时按**来源优先级版本**逐时刻仲裁出权威值：

- **来源优先级版本**：每次发布一版 `priorities`（来源标识按优先级从高到低
  排列）并携带**生效时刻** `effective_at`；版本只增不改。对采样时刻 t 仲裁时
  取 `effective_at <= t` 的**最新版本**（版本号最大者），因此**调整优先级只从
  生效时刻起影响重算**——历史存证收据与原始读数行绝不改写，仲裁在查询时
  实时进行；
- **权威值选取**：在生效版本下取优先级最高的来源集合。集合内照度唯一 →
  该值为该时刻权威值；**最高级并列且照度不同 → 该时刻保持冲突**。冲突时刻
  只保留给权威时间线查询，**不作为剂量积分端点**：其前后相邻权威点即使间隔
  合规也绝不直接相连——普通剂量与 `calendar=display` 的开放时段都在冲突处
  断开，缺测（含开放时段内缺测）、截至窗口末的累计剂量、首次越线与
  closed/unknown/covered 统计均按断点后的各段分别处理；诊断连续链同样在
  两侧重置且**不生成 `timestamp_conflict`**（原始读数异值、未经优先级仲裁的
  平权冲突仍会生成该事件）；
- **并列的情形**：未列入 `priorities` 的来源（含未声明来源的旧式读数）
  **并列最低**；同一来源在同一时刻的多条读数天然同级。并列集合照度相同
  则合并为一个权威点（读数 ID 全部保留）；
- **未配置任何版本**：全部读数平权（同时刻同值合并、异值冲突），与多来源
  功能上线前的行为完全一致；旧请求不带 `source_id` 时上报、幂等、收据、
  剂量、诊断行为均不变；
- **配置更新乐观并发**：发布新版本必须携带 `expected_version`（当前最新
  版本号，尚无版本时为 0），与服务器不一致返回 **409**；并发更新只有一个
  成功；
- **别名与归并**：配置与时间线接口都先解析别名、命中规范档案。档案归并时
  来源配置随档案合并：仅来源有才整体搬迁到目标；双方逐版本一致保留目标的
  一份；**双方都有且不一致 → 归并 409 拒绝，冲突绝不静默覆盖**（预演响应
  以 `source_priority_conflict` 与 `metadata.source_priority` 差异明示，
  配置指纹也纳入归并版本令牌，预演后任一侧发布新版本同样使令牌失效）。

### 4a. 上报带来源的读数

```bash
curl -s -X POST http://localhost:8000/exhibits/<id>/readings \
  -H 'Content-Type: application/json' \
  -d '{"readings": [
        {"id": "a-0815", "ts": "2026-09-20T08:00:00Z", "lux": 100, "source_id": "sensor-a"},
        {"id": "b-0801", "ts": "2026-09-20T08:00:00Z", "lux": 500, "source_id": "sensor-b"},
        {"id": "a-0816", "ts": "2026-09-20T09:00:00Z", "lux": 100, "source_id": "sensor-a"}
      ]}'
```

同一时刻的两条读数全部留存；存证收据的 `request.readings` 中同样记录
`source_id`（未声明来源的旧式请求收据内容保持原样、不含该键）。读数 ID
幂等规则不变：同 ID 且采样时刻、照度、来源标识完全一致 → `duplicate`，
任一不同（含来源不同）→ `conflict`。

### 4b. 发布来源优先级版本

```bash
# 首次发布：expected_version 为 0
curl -s -X PUT http://localhost:8000/exhibits/<id>/source-priority \
  -H 'Content-Type: application/json' \
  -d '{"effective_at": "2026-09-20T00:00:00Z",
       "priorities": ["sensor-a", "sensor-b"],
       "expected_version": 0}'
```

```json
{"requested_id": "...", "canonical_id": "...", "version": 1,
 "effective_at": "2026-09-20T00:00:00Z",
 "priorities": ["sensor-a", "sensor-b"],
 "created_at": "2026-09-21T01:23:45.678Z"}
```

```bash
# 调整优先级：自新的生效时刻起影响重算；expected_version 过期 → 409
curl -i -X PUT http://localhost:8000/exhibits/<id>/source-priority \
  -H 'Content-Type: application/json' \
  -d '{"effective_at": "2026-09-21T00:00:00Z",
       "priorities": ["sensor-b", "sensor-a"],
       "expected_version": 0}'
# HTTP/1.1 409 Conflict
# {"detail": "来源优先级配置已过期：当前最新版本为 1，请求携带 expected_version=0，请重新读取配置后重试"}

# 查询全部版本（GET 不解析失败：未配置时 current_version 为 0、versions 为空）
curl -s http://localhost:8000/exhibits/<id>/source-priority
```

### 4c. 权威时间线查询

```bash
curl -s 'http://localhost:8000/exhibits/<id>/timeline?start=2026-09-20T08:00:00Z&end=2026-09-20T09:00:00Z'
```

```json
{
  "requested_id": "...", "canonical_id": "...",
  "start": "2026-09-20T08:00:00Z", "end": "2026-09-20T09:00:00Z",
  "arbitrated": true,
  "entries": [
    {"ts": "2026-09-20T08:00:00Z", "status": "ok", "lux": 100.0,
     "reading_ids": ["a-0815"], "source_ids": ["sensor-a"],
     "priority_version": 1},
    {"ts": "2026-09-20T09:00:00Z", "status": "ok", "lux": 100.0,
     "reading_ids": ["a-0816"], "source_ids": ["sensor-a"],
     "priority_version": 1}
  ]
}
```

- `status=ok`：该时刻仲裁成功，`lux` 为权威照度，`reading_ids` /
  `source_ids` 为胜出集合（被仲裁丢弃的低优先级读数不进入时间线）；
- `status=conflict`：最高级并列且照度不同，该时刻保持冲突（给出并列
  读数与 `lux_span`）；冲突状态只在此接口保留——不进入剂量积分（普通剂量
  与 `calendar=display` 都在此断开），诊断链在两侧重置且不产生
  `timestamp_conflict` 事件；
- `priority_version` 为该时刻生效的优先级版本号（无生效版本、平权仲裁
  时为 `null`）；`arbitrated=false` 表示展品未配置任何版本，全部读数平权。

配置优先级后，剂量查询（含 `calendar=display`）与采集质量诊断都基于权威
时间线：上例中 08:00 时刻按 `sensor-a` 的 100 lux 计入，`sensor-b` 的
500 lux 被仲裁丢弃；剂量响应的 `reading_count` 为窗口内权威点数量。若某时刻
最高级并列且照度不同，该时刻在时间线上保留为 `conflict`，但剂量积分在冲突
处断开（两侧权威点不直接相连，缺测与统计按各段分别处理），诊断也不把它记为
`timestamp_conflict`。

## 展陈开放日历

每件展品可配置一份展陈开放日历，剂量查询据此只在开放时段内累计剂量：
- **时区**：IANA 时区名（如 `Asia/Shanghai`、`America/New_York`），所有本地时刻
  都按该时区展开为绝对时间；
- **每周开放时段**（`weekly`）：按本地星期几（ISO 8601，`1`=周一 … `7`=周日）
  给出 `start`/`end`（`HH:MM` 或 `HH:MM:SS`），可配置多段；
- **例外时段**（`exceptions`）：按本地日期**整体替换**当日每周规则；
  `windows` 为空表示当日闭馆（如节假日），同一日期只能配置一次；
- **跨午夜**：时段内 `end` 不晚于 `start` 表示开放到次日同一本地时刻
  （`22:00`–`02:00` 即夜间开放；`00:00`–`00:00` 表示全天 24 小时开放）；
- **重叠合并**：展开为绝对时间后，重叠或首尾相接的区间自动合并，覆盖时长
  不会重复累计；
- **夏令时**：
  - 本地边界时刻不存在（春季拨快的空档）→ **顺延到首个有效时刻**；
  - 本地边界时刻重复（秋季回拨）→ **开始边界取较早瞬间、结束边界取较晚
    瞬间**，重复的一小时被完整计入开放时长。

### 5. 设置 / 查询日历

```bash
curl -s -X PUT http://localhost:8000/exhibits/<id>/calendar \
  -H 'Content-Type: application/json' \
  -d '{
    "timezone": "Asia/Shanghai",
    "weekly": [
      {"weekday": 1, "start": "09:00", "end": "17:00"},
      {"weekday": 2, "start": "09:00", "end": "17:00"},
      {"weekday": 6, "start": "10:00", "end": "22:00"}
    ],
    "exceptions": [
      {"date": "2026-10-01", "windows": []},
      {"date": "2026-10-02", "windows": [{"start": "10:00", "end": "16:00"}]}
    ]
  }'
```

`PUT` 为全量替换语义；`GET /exhibits/{id}/calendar` 查询（未配置 → 404），
`DELETE /exhibits/{id}/calendar` 删除（幂等，204）。别名 ID 同样可用，
自动解析到规范档案。

### 6. 按开放日历核算剂量（`calendar=display`）

```bash
curl -s 'http://localhost:8000/exhibits/<id>/dose?start=2026-09-20T00:00:00Z&end=2026-09-21T00:00:00Z&calendar=display'
```

```json
{
  "requested_id": "...",
  "canonical_id": "...",
  "start": "2026-09-20T00:00:00Z",
  "end": "2026-09-21T00:00:00Z",
  "dose_limit_luxh": 150.0,
  "known_dose_luxh": 800.0,
  "remaining_luxh": -650.0,
  "exceeded": true,
  "first_exceeded_at": "2026-09-20T08:30:00Z",
  "unknown_seconds": 0.0,
  "covered_seconds": 28800.0,
  "closed_seconds": 57600.0,
  "reading_count": 25
}
```

- 梯形积分**仅在开放区间内裁剪**：闭馆时段不参与积分，窗口内闭馆时长单列
  `closed_seconds`；`covered_seconds`、`unknown_seconds`、`closed_seconds`
  三者之和等于窗口时长（开放但缺测的部分仍计入 `unknown_seconds`）；
- `remaining_luxh` / `exceeded` / `first_exceeded_at` 也按**截至窗口末的开放
  历史**累计，闭馆时段的读数不进入历史累计；
- 展品**未配置日历**时带 `calendar=display` 查询返回 **409**；
- 不带 `calendar` 参数时响应保持原有口径（不含 `closed_seconds` 字段）。

## 重复档案归并

当同一件展品被重复建档时，可把一个**来源档案**（`source_id`）合入**目标档案**
（`target_id`）。规则：

- 目标档案的**名称与剂量配置**（`dose_limit_luxh`、`max_interval_seconds`）保持不变，
  归并后剂量核算继续沿用原有梯形法算法与目标档案参数；
- 目标档案的**展陈开放日历**保持不变；来源档案的日历随档案删除一并清除，
  归并后来源别名访问日历与 `calendar=display` 核算都命中目标档案的日历；
- 来源档案的**来源优先级配置**随归并合并：仅来源有才整体搬迁到目标
  （版本号保持）；双方逐版本一致时保留目标的一份；双方都有且不一致时
  **归并 409 拒绝，冲突绝不静默覆盖**——预演响应以
  `source_priority_conflict=true` 与 `metadata.source_priority` 差异明示，
  且配置指纹纳入版本令牌，预演后任一侧发布新版本同样使令牌失效；
- 仅来源存在的读数整体搬迁；同读数 ID 且**时间、照度一致**的碰撞自动去重；
  同 ID 但**内容不同**的碰撞必须逐条选择 `keep_target`（保留目标）、
  `keep_source`（用来源覆盖目标）或 `rename_source`（为来源读数指定新 ID），
  **未全部解决不得提交**；
- 预演返回的 `token` 绑定两边元数据与读数集合版本；预演后任一档案被写入/修改/删除，
  提交返回 **409** 且不产生任何部分变更，需要重新预演；
- 提交在**单个 SQLite 事务**内完成；重复使用同一令牌返回首次提交的原结果
  （`already_committed: true`）；
- 归并后来源 ID 成为目标 ID 的**永久别名**（不可删、不可重新建档），
  现有查询与读数上报都自动解析到目标档案；多次归并时旧别名会跟随改指向最终目标，
  别名表始终只有一跳，**禁止别名环**；
- 归并**保留来源与目标的既有存证收据链**（只增不改，旧哈希绝不重写），
  提交前分别校验两条链，任一断链或内容不符即 **409** 拒绝归并；归并后的
  新上报（含经来源别名）继续接入**目标原链**；响应的 `receipt_chains`
  列出保留的链标识，可分别用 `GET /receipts/chains/{chain_id}/verify` 核查；
- 跨轮区域交接链：单侧已有交接链时归并把整链**改挂**到目标（证据行不变，
  归并后用目标 ID 或来源别名查到同一条链，响应 `handover_chain_carried=true`）；
  归并后下一笔交接**沿用归并前固化的接收轮**作为来源轮，服务在写事务内按当前
  别名关系解释目标 ID 与全部来源别名在两轮中的固化证据（同轮多个身份须全部
  定位且区域一致，任一不合格或冲突都 409、不择一放行），原盘点摘要与旧交接
  记录不改写；**两侧都有交接链时 409 拒绝归并、不改动任何记录**——预演以
  `area_handover_conflict=true`、`source_handover_count` /
  `target_handover_count` 明示，交接链指纹也纳入版本令牌，预演后新增交接
  同样使令牌失效（详见下文“跨盘点轮次的馆藏区域交接”）。

### 7. 预演冲突

```bash
curl -s -X POST http://localhost:8000/merges/preview \
  -H 'Content-Type: application/json' \
  -d '{"source_id": "DUP-042", "target_id": "EX-042"}'
```

```json
{
  "token": "8f941a6033714b948a4bf86fbf1d8001",
  "requested_source_id": "DUP-042", "requested_target_id": "EX-042",
  "source_id": "DUP-042", "target_id": "EX-042",
  "metadata": {
    "name": {"field": "name", "source": "绢画(草稿登记)", "target": "唐代绢画"},
    "dose_limit_luxh": {"field": "dose_limit_luxh", "source": 100.0, "target": 150.0}
  },
  "source_reading_count": 3, "target_reading_count": 2,
  "collisions": [
    {"reading_id": "r1", "kind": "duplicate",
     "source_ts": "2026-09-20T08:00:00Z", "source_lux": 50.0,
     "target_ts": "2026-09-20T08:00:00Z", "target_lux": 50.0},
    {"reading_id": "r2", "kind": "conflict",
     "source_ts": "2026-09-20T09:00:00Z", "source_lux": 999.0,
     "target_ts": "2026-09-20T09:00:00Z", "target_lux": 120.0}
  ],
  "auto_duplicate_count": 1, "conflict_count": 1,
  "source_only_count": 1, "ready_to_commit": false
}
```

- `metadata` 只列出**不同**的字段；没有差异的字段（如 `max_interval_seconds`）不出现。
- `kind=duplicate` 的碰撞无需处理；`kind=conflict` 时 `ready_to_commit=false`，
  必须在提交时逐条给出裁决。

### 8. 解决冲突并提交

```bash
curl -s -X POST http://localhost:8000/merges/commit \
  -H 'Content-Type: application/json' \
  -d '{
    "token": "8f941a6033714b948a4bf86fbf1d8001",
    "resolutions": [
      {"reading_id": "r2", "action": "keep_target"}
    ]
  }'
```

三种裁决（每个 `conflict` 碰撞恰好一条）：

| action | 含义 |
|---|---|
| `keep_target` | 保留目标读数，丢弃来源副本 |
| `keep_source` | 用来源读数的时间/照度覆盖目标读数 |
| `rename_source` | 保留来源读数，但以 `new_id` 迁入（`new_id` 不得与归并后的任何读数 ID 冲突） |

```json
{
  "token": "8f941a6033714b948a4bf86fbf1d8001",
  "source_id": "DUP-042", "target_id": "EX-042",
  "name": "唐代绢画", "dose_limit_luxh": 150.0, "max_interval_seconds": 3600.0,
  "moved_count": 1, "auto_deduplicated_count": 1,
  "resolutions_applied": [{"reading_id": "r2", "action": "keep_target", "new_id": null}],
  "final_reading_count": 3,
  "aliases": ["DUP-042"],
  "receipt_chains": ["DUP-042", "EX-042"],
  "already_committed": false
}
```

提交后来源 ID 立即成为别名：

```bash
# 用来源 ID 查询/上报都会落到目标档案
curl -s http://localhost:8000/exhibits/DUP-042
# {"requested_id": "DUP-042", "canonical_id": "EX-042", "name": "唐代绢画", ...}

curl -s -X POST http://localhost:8000/exhibits/DUP-042/readings \
  -H 'Content-Type: application/json' \
  -d '{"readings": [{"id": "r9", "ts": "2026-09-20T11:00:00Z", "lux": 80}]}'
# {"requested_id": "DUP-042", "canonical_id": "EX-042", "inserted": 1, ...}
```

### 9. 令牌失效与重放

```bash
# 预演后任一侧发生写入/修改，再提交 → 409，无部分变更
curl -i -X POST http://localhost:8000/merges/commit \
  -H 'Content-Type: application/json' \
  -d '{"token": "<过期令牌>", "resolutions": [{"reading_id": "r2", "action": "keep_target"}]}'
# HTTP/1.1 409 Conflict
# {"detail": "归并预演已失效：预演后来源或目标档案发生写入或修改，预演结果已过期"}
# 此时应重新调用 /merges/preview 获取新令牌，再提交。

# 重复使用已成功提交的令牌 → 200，返回首次提交的原结果
curl -s -X POST http://localhost:8000/merges/commit \
  -H 'Content-Type: application/json' \
  -d '{"token": "8f941a6033714b948a4bf86fbf1d8001", "resolutions": []}'
# {"...": "...", "already_committed": true}
```

错误码小结：预演缺少档案 → 404；两个 ID 相同或已指向同一档案 → 422；
提交时令牌不存在 → 404，预演过期 → 409，冲突未全部解决 / `new_id` 冲突 /
缺少 `new_id` → 422；任一档案的存证收据链断链或内容不符 → 409。

## 照度上报存证（收据链）

每次批量上报读数，服务都会在**写入读数的同一事务**内生成一张**存证收据**：

- 收据内容按**稳定规范序列化**（JSON 键排序、紧凑分隔符、浮点一律
  `float.hex`、时间统一为 UTC `Z` 结尾）记录：链标识、序号、请求中的逐条
  读数，以及每条 `inserted` / `duplicate` / `conflict` 处理结果；
- `content_hash = SHA-256(规范序列化内容)`；
  `chain_hash = SHA-256(prev_hash + content_hash)`，与上一张收据首尾相接；
  每条链的首张收据 `prev_hash` 为 64 个 `0`（创世）；
- 链以写入时的**规范档案 ID** 为标识（`chain_id`），`seq` 从 1 连续递增：
  上报事务以 `BEGIN IMMEDIATE` 串行化，**并发上报也只形成唯一连续顺序**；
- **失败事务不留收据**：读数写入与收据生成同生共死，任一失败整体回滚；
- 收据**只增不改**：档案归并时来源与目标的既有链都原样保留（归并提交前
  分别校验，断链即拒绝），归并后的新上报继续接入**目标原链**，旧哈希绝不
  重写；归并后来源链可通过 `GET /receipts/chains/{chain_id}` 独立核查。

### 10. 上报响应中的收据

```bash
curl -s -X POST http://localhost:8000/exhibits/<id>/readings \
  -H 'Content-Type: application/json' \
  -d '{"readings": [{"id": "r1", "ts": "2026-09-20T08:00:00Z", "lux": 100}]}'
```

```json
{
  "requested_id": "...", "canonical_id": "...",
  "inserted": 1, "duplicates": 0, "conflicts": 0,
  "results": [{"id": "r1", "status": "inserted"}],
  "receipt": {
    "chain_id": "...", "seq": 1, "receipt_id": "9c4f...",
    "prev_hash": "0000000000000000000000000000000000000000000000000000000000000000",
    "content_hash": "7d3e...", "chain_hash": "b91a...",
    "created_at": "2026-09-21T01:23:45.678Z",
    "content": {
      "chain_id": "...", "seq": 1, "receipt_id": "9c4f...",
      "requested_id": "...", "created_at": "2026-09-21T01:23:45.678Z",
      "request": {"readings": [{"id": "r1", "ts": "2026-09-20T08:00:00Z", "lux": 100.0}]},
      "outcome": {"inserted": 1, "duplicates": 0, "conflicts": 0,
                  "results": [{"id": "r1", "status": "inserted"}]}
    }
  }
}
```

### 11. 查询收据

```bash
# 整条链（按 seq 升序；别名自动解析到规范档案）
curl -s http://localhost:8000/exhibits/<id>/receipts

# 单张收据
curl -s http://localhost:8000/exhibits/<id>/receipts/1

# 归并后来源档案的既有链：按链标识直接读取（不解析别名）
curl -s http://localhost:8000/receipts/chains/DUP-042
```

### 12. 整链校验

```bash
curl -s http://localhost:8000/exhibits/<id>/receipts/verify
```

```json
{"chain_id": "...", "ok": true, "checked": 3, "total": 3, "first_failure": null}
```

校验逐张重算：序号连续性、`prev_hash` 衔接、内容哈希与链哈希是否复现。
发现断链或内容不符时返回首个失败位置：

```json
{
  "chain_id": "...", "ok": false, "checked": 1, "total": 3,
  "first_failure": {"seq": 2, "reason": "content_hash_mismatch",
                    "detail": "收据内容与登记的内容哈希不符（内容被篡改）"}
}
```

| 字段 | 含义 |
|---|---|
| `chain_id` | 链标识（写入时的规范档案 ID） |
| `ok` | 整链是否通过 |
| `checked` | 已检查且通过的收据数量（失败时即首个失败位置之前的数量） |
| `total` | 链上收据总数 |
| `first_failure` | 首个断链/内容不符位置：`seq` + `reason` + `detail`；通过时为 `null` |

`reason` 取值：`seq_gap`（序号不连续）、`prev_hash_mismatch`（与上一张
链哈希不衔接）、`content_hash_mismatch`（内容与内容哈希不符）、
`chain_hash_mismatch`（链哈希重算不符）。

归并后来源链与目标链可分别校验（按链标识，不解析别名）：

```bash
curl -s http://localhost:8000/receipts/chains/DUP-042/verify
curl -s http://localhost:8000/receipts/chains/EX-042/verify
```

## 照度上报批次更正（作废）

传感器漂移、接线错误等情况下，可用批次更正把某张存证收据中**实际插入**的
读数整批作废。作废是**软删除**：读数行保留（仍可在数据库中核查），但立即退出
剂量核算、采集质量诊断与读数列表口径；收据本身**只增不改**，其内容、序号与
链哈希全部保持不变，作废状态在查询收据时单独标出。

### 12b. 提交一次批次更正

```bash
curl -s -X POST http://localhost:8000/exhibits/<id>/receipts/corrections \
  -H 'Content-Type: application/json' \
  -d '{
    "receipt_id": "9c4f1a2b…",
    "request_key": "fix-2026-09-21-shift-A",
    "reason": "A 组传感器 09:00–11:00 漂移，该批次读数作废"
  }'
```

请求字段：

| 字段 | 含义 |
|---|---|
| `receipt_id` | 被更正批次的存证收据 `receipt_id`（上报响应 `receipt.receipt_id`） |
| `request_key` | 更正幂等键：同键重放首次结果；换目标（档案 / 收据 / 原因）→ 409 |
| `reason` | 作废原因（必填） |

路径中的展品 ID 可用规范 ID，也可用归并后的**别名**（自动解析到规范档案）。

```json
{
  "requested_id": "...",
  "canonical_id": "...",
  "receipt_id": "9c4f1a2b…",
  "chain_id": "...",
  "seq": 3,
  "request_key": "fix-2026-09-21-shift-A",
  "reason": "A 组传感器 09:00–11:00 漂移，该批次读数作废",
  "voided_count": 2,
  "voided_readings": [
    {"id": "r2", "ts": "2026-09-21T09:00:00Z", "lux": 180.0},
    {"id": "r3", "ts": "2026-09-21T10:00:00Z", "lux": 190.0}
  ],
  "voided_at": "2026-09-21T12:00:00.000Z",
  "already_committed": false
}
```

- 只作废收据结果为 **`inserted`** 的读数；`duplicate` / `conflict` 的读数
  本就未由该收据写入，不在作废范围内（同批次请求内重复出现的插入 ID 去重）；
- 作废在写读数作废标记的**同一个 `BEGIN IMMEDIATE` 事务**内连同更正记录落库，
  任一前置校验失败整体回滚，**不做部分作废**。

### 12c. 前置状态变化：整次 409

服务在事务内复核全部前置条件，任一不满足整次返回 **409** 且不产生任何作废：

- **收据链变化**：收据不属于当前规范档案的链，或整链校验失败
  （序号 / `prev_hash` / 内容哈希 / 链哈希被篡改）；
- **归并状态变化**：例如档案已被归并，用来源别名更正**来源链**上的旧收据
  ——来源链原样保留但读数已搬到目标档案，链标识与当前规范档案不一致；
- **读数归属变化**：某条插入读数已不在该档案下（被归并去重 / 删除）、
  内容已被归并裁决覆盖（`keep_source` 改名 / 改值）、或已被另一次更正作废；
- 该收据此前已被**另一条更正**成功作废（一张收据至多成功更正一次）。

不存在的展品或 `receipt_id` 返回 **404**。

### 12d. 幂等重放与换目标冲突

`request_key` 与（规范档案、收据、原因）三元组一一绑定，模式与盘点轮次一致：

```bash
# 相同 request_key + 相同目标重放 → 200，回放首次结果
curl -s -X POST http://localhost:8000/exhibits/<id>/receipts/corrections \
  -H 'Content-Type: application/json' \
  -d '{"receipt_id":"9c4f…","request_key":"fix-2026-09-21-shift-A","reason":"同一原因"}'
# {"...": "...", "already_committed": true, "voided_count": 2, "voided_at": "<首次时刻>"}

# 同 request_key 换收据 / 改原因 / 指向另一档案 → 409
# HTTP/1.1 409 Conflict
# {"detail":"request_key fix-2026-09-21-shift-A 已绑定另一次更正（收据/档案/原因不同），不能换目标重复提交"}

# 归并后用来源别名重放同一 request_key（解析到同一规范档案）→ 200 回放
```

并发提交同一张收据的不同 `request_key`：恰好一个成功（200），其余 409；
并发提交相同 `request_key`：一份首次结果，竞争方全部回放
（`already_committed=true`）。

### 12e. 作废后的查询口径

- **剂量查询** `GET /exhibits/{id}/dose`：已作废读数立即排除，按剩余时间线
  实时重算——作废中间读数可能使相邻点间隔超过 `max_interval_seconds` 而产生
  新的缺测段，剩余额度与首次越线时刻同样按剩余历史重算；
- **采集质量诊断** `POST /exhibits/{id}/readings/diagnose`：在剩余读数的完整
  时间线上重新形成 gap / jump / stuck / timestamp_conflict 事件；
- **读数列表** `GET /exhibits/{id}/readings`：不再返回已作废读数；
- **存证收据**：内容、序号、`prev_hash` / `content_hash` / `chain_hash`
  全部不变，整链校验仍通过；查询收据（单张、档案链列表、按链标识直查）时
  在 content 之外的 **`voids`** 字段标发作废状态：

```json
{
  "chain_id": "...", "seq": 3, "receipt_id": "9c4f…",
  "content_hash": "…", "chain_hash": "…",
  "content": { "…": "（与作废前完全一致，哈希仍可复现）" },
  "voids": [
    {"request_key": "fix-2026-09-21-shift-A",
     "reason": "A 组传感器 09:00–11:00 漂移，该批次读数作废",
     "voided_count": 2,
     "voided_readings": [
       {"id": "r2", "ts": "2026-09-21T09:00:00Z", "lux": 180.0},
       {"id": "r3", "ts": "2026-09-21T10:00:00Z", "lux": 190.0}],
     "voided_at": "2026-09-21T12:00:00.000Z"}
  ]
}
```

未作废收据 `voids` 为空列表 `[]`。归并会把读数的作废标记随读数一起搬迁，
更正记录与来源收据链一样原样保留，可通过
`GET /receipts/chains/{chain_id}` 继续核查。

> **读数 ID 复用**：作废是软删除，读数 ID 仍是该档案下的既有 ID。用同 ID
> 同数据重新上报记为 `duplicate`（读数保持作废态，不会“复活”），同 ID
> 不同数据记为 `conflict`；需要以新读数替换时请使用新的读数 ID。

## 照度异常处置单

诊断发现照度异常（gap / jump / stuck / timestamp_conflict）后，可开立
**照度异常处置单**跟踪处置过程：

- **创建即固化快照**：`POST /exhibits/{id}/illumination-tickets` 提交展品 ID
  （可用归并别名）、处置时间窗、诊断阈值（与诊断接口同口径）与
  `request_key`。服务在**一个 SQLite 事务**（`BEGIN IMMEDIATE`）内按
  **未作废权威时间线**重算诊断事件（与 `POST /readings/diagnose` 同口径）
  与 `calendar=display` 剂量（与 `GET /dose?calendar=display` 同口径），
  连同**收据链末端哈希**、未作废读数指纹、来源优先级配置指纹与日历指纹
  一起固化快照（`snapshot`），并以其稳定规范序列化的 SHA-256 作为
  **`digest`** 返回。展品未配置展陈日历时无法核算 `calendar=display`
  剂量，创建直接 **409**；
- **快照不可变**：`snapshot` 与 `digest` 落库后绝不改写；相同
  `request_key` 且目标（规范档案 / 时间窗 / 诊断阈值）一致时重放首次创建
  的原件（`already_committed=true`），换目标 → **409**；
- **结案核对**：处置单只能从 `open` 结案为 `resolved` 或 `accepted`。
  结案提交 `expected_digest`、决定与说明；服务在写事务内**重算当前摘要**，
  读数、作废、来源优先级、日历或收据链的任何变化都会使摘要不同 →
  **409 且不写入**。结案前先 `GET /illumination-tickets/{ticket_id}` 取回
  实时重算的 `current_digest` 作为 `expected_digest`；
- **resolved 举证**：须给出涉及的存证收据（`receipt_ids`）或批次更正
  （`correction_request_keys`）至少一项（须属于本规范档案，否则 422），
  并证明重算后**原诊断事件全部消失**且 `calendar=display` 剂量**未越限**，
  否则 **409**；**accepted** 保留事件，只在 `note` 中记录理由（不接受
  证据引用）。已结案的处置单不能再次结案（409）。

### 21. 创建处置单

```bash
curl -s -X POST http://localhost:8000/exhibits/<id>/illumination-tickets \
  -H 'Content-Type: application/json' \
  -d '{
    "start": "2026-09-20T08:00:00Z",
    "end":   "2026-09-20T18:00:00Z",
    "max_rate_lux_per_second": 1.0,
    "stuck_tolerance_lux": 2.0,
    "min_stuck_seconds": 1800,
    "request_key": "ticket-2026-09-20-stuck-01"
  }'
```

```json
{
  "ticket_id": "7f3a…", "request_key": "ticket-2026-09-20-stuck-01",
  "requested_id": "...", "canonical_id": "...",
  "start": "2026-09-20T08:00:00Z", "end": "2026-09-20T18:00:00Z",
  "max_rate_lux_per_second": 1.0, "stuck_tolerance_lux": 2.0,
  "min_stuck_seconds": 1800.0,
  "status": "open",
  "digest": "b3c1…",
  "snapshot": {
    "canonical_id": "...",
    "window": {"start": "2026-09-20T08:00:00Z", "end": "2026-09-20T18:00:00Z"},
    "max_interval_seconds": 3600.0,
    "thresholds": {"max_rate_lux_per_second": 1.0,
                   "stuck_tolerance_lux": 2.0, "min_stuck_seconds": 1800.0},
    "events": [
      {"type": "stuck",
       "start": "2026-09-20T08:00:00Z", "end": "2026-09-20T11:00:00Z",
       "reading_ids": ["r1", "r2", "r3", "r4"],
       "duration_seconds": 10800.0, "lux_span": 2.0}
    ],
    "dose": {"dose_limit_luxh": 150.0, "known_dose_luxh": 300.0,
             "remaining_luxh": -150.0, "exceeded": true,
             "first_exceeded_at": "2026-09-20T09:30:00Z",
             "unknown_seconds": 25200.0, "covered_seconds": 10800.0,
             "closed_seconds": 0.0, "reading_count": 4},
    "receipt_chain": {"chain_id": "...", "seq": 3, "chain_hash": "b91a…"},
    "readings_fingerprint": "…", "source_priority_fingerprint": "…",
    "calendar_fingerprint": "…"
  },
  "created_at": "2026-09-21T02:00:00.000Z",
  "decision": null, "closed_at": null, "close_note": null,
  "evidence": null, "closed_digest": null, "current_digest": null,
  "already_committed": false
}
```

- 相同 `request_key` 重放（目标一致）→ 返回首次创建的原件
  （`already_committed=true`），即使库内读数已变化，快照仍是首次固化的
  内容；相同 `request_key` 换展品 / 时间窗 / 诊断阈值 → **409**；
- 并发创建相同 `request_key`：只固化一张处置单，竞争方全部回放。

### 22. 取回处置单与当前摘要

```bash
# open 状态的处置单附带 current_digest：按当前库内状态实时重算的摘要
curl -s http://localhost:8000/illumination-tickets/7f3a…
# {"ticket_id": "7f3a…", "status": "open", "digest": "b3c1…",
#  "current_digest": "b3c1…", ...}        # 无变化时与 digest 相同

# 列出某件展品的全部处置单（别名自动解析到规范档案）
curl -s http://localhost:8000/exhibits/<id>/illumination-tickets
```

处置（如作废异常批次）会改变读数集合，`current_digest` 随之变化——结案
前必须重新 GET 取回最新的 `current_digest`；日历被删除等导致无法重算时
`current_digest` 为 `null`（结案同样 409）。

### 23. 结案为 resolved（举证）

```bash
# 1) 处置：作废产生 stuck 事件的批次（见「照度上报批次更正」）
curl -s -X POST http://localhost:8000/exhibits/<id>/receipts/corrections \
  -H 'Content-Type: application/json' \
  -d '{"receipt_id": "9c4f…", "request_key": "fix-2026-09-20-stuck",
       "reason": "传感器卡死批次作废"}'

# 2) 取回当前摘要（作废已改变读数集合，创建时的 digest 已失效）
curl -s http://localhost:8000/illumination-tickets/7f3a…
# {"...": "...", "current_digest": "e7d2…"}

# 3) 结案：引用涉及的更正 request_key（或收据 receipt_id），
#    服务重算证明原 stuck 事件消失且 calendar=display 剂量未越限
curl -s -X POST http://localhost:8000/illumination-tickets/7f3a…/close \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_digest": "e7d2…",
    "decision": "resolved",
    "note": "卡死批次已作废，时间线重算正常",
    "correction_request_keys": ["fix-2026-09-20-stuck"]
  }'
```

```json
{
  "ticket_id": "7f3a…", "status": "resolved", "decision": "resolved",
  "digest": "b3c1…", "closed_digest": "e7d2…",
  "close_note": "卡死批次已作废，时间线重算正常",
  "evidence": {"receipt_ids": [],
               "correction_request_keys": ["fix-2026-09-20-stuck"]},
  "events_cleared": true, "dose_exceeded": false,
  "closed_at": "2026-09-21T03:00:00.000Z",
  "snapshot": {"...": "（与创建时完全一致，原 stuck 事件仍固化其中）"}
}
```

`resolved` 举证规则：

- `receipt_ids` / `correction_request_keys` 至少一项，且必须属于本规范
  档案（收据在本档案链上、更正属于本档案），否则 **422**；
- 重算后**原诊断事件须全部消失**（快照 `events` 中的每个事件都不再出现
  于当前重算结果），且 `calendar=display` 剂量**未越限**，否则 **409**
  且不写入；响应的 `events_cleared` / `dose_exceeded` 即这两项证明。

### 24. 结案为 accepted（保留事件）

```bash
curl -s -X POST http://localhost:8000/illumination-tickets/7f3a…/close \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_digest": "b3c1…",
    "decision": "accepted",
    "note": "夜间红外补光导致照度平稳段，属展陈正常工况，保留事件"
  }'
# {"ticket_id": "7f3a…", "status": "accepted", "events_cleared": null,
#  "dose_exceeded": null, "evidence": null, ...}
```

`accepted` 保留事件、只在 `note` 中记录理由，不接受收据/更正引用
（携带 `receipt_ids` 或 `correction_request_keys` → **422**）。

### 25. 摘要核对失败与错误码

结案时服务在写事务内重算当前摘要并与 `expected_digest` 比对，**不一致则
409 且不写入**（处置单保持 `open`）：

```bash
# 创建后又上报了新读数（读数集合与收据链末端都变化）
curl -i -X POST http://localhost:8000/illumination-tickets/7f3a…/close \
  -H 'Content-Type: application/json' \
  -d '{"expected_digest": "b3c1…", "decision": "accepted", "note": "x"}'
# HTTP/1.1 409 Conflict
# {"detail": "处置单摘要核对失败：读数、作废、来源优先级、日历或收据链已变化
#  （期望 b3c1…，当前重算 e7d2…），结案未写入；请重新 GET 取回 current_digest 后重试"}
```

会使摘要变化的操作：新上报读数（读数集合 + 收据链末端）、批次更正作废
（读数集合）、发布来源优先级版本（配置指纹，可能改变权威时间线）、修改或
删除展陈日历（日历指纹 + `calendar=display` 剂量）、档案归并（链标识 /
读数归属 / 日历归属变化）。

错误码小结：展品不存在 → 404；处置单不存在 → 404；`end` 不晚于 `start`
或未配置展陈日历 → 422 / 409（创建）；`request_key` 换目标 → 409；已结案
再次结案 → 409；摘要核对失败 / resolved 证明失败 → 409；resolved 缺少证据
或证据不属于本档案、accepted 携带证据 → 422。

## 展厅开放批次合规核验

`POST /compliance-checks` 为一批展品做"展厅开放"合规核验：调用方提交
`request_key`、核验时间窗、盘点 `round_id`、展品 ID 清单与诊断阈值，服务在
**一个 SQLite 写事务**（`BEGIN IMMEDIATE`）内完成全部检查；只有所有展品都
通过，才固化包含各项证据摘要的**合规凭证**并返回 `digest`；任一项失败返回
逐件原因（`status=non_compliant`），**不写入任何记录**，`request_key`
不占用。

### 核验口径（全部在同一写事务内）

- 展品 ID 先经归并别名解析为规范档案（不存在 → 404；解析后同一规范档案
  出现多次 → 422）；
- 读取 `round_id` 对应轮次的**固化**盘点结果（轮次尚未对账 → 404）；
- 逐件读取展陈日历，按与 `GET /dose?calendar=display` 同口径重算**截至
  窗口末的开放剂量**；
- 按与 `POST /readings/diagnose` 同口径在完整未作废权威时间线上形成诊断
  事件，筛选与核验窗口相交的部分；
- 检查该规范档案当前**未结（open）的照度异常处置单**。

### 合规条件（四项全部满足才固化凭证）

1. 每件展品都在盘点轮次中**定位到预期区域**（缺失 / 错位 / 位置不明 /
   意外出现 / 轮次未涵盖均为不合规）；
2. 窗口内无 `gap` / `jump` / `stuck` 诊断事件（`timestamp_conflict`
   只记入证据，不阻断合规）；
3. 截至窗口末的 `calendar=display` 开放剂量**未越限**（展品未配置展陈
   日历视为无法核验、不合规）；
4. 没有未结（open）照度异常处置单。

### 26. 提交一次合规核验

```bash
curl -s -X POST http://localhost:8000/compliance-checks \
  -H 'Content-Type: application/json' \
  -d '{
    "request_key": "hall-open-2026-10-01-am",
    "round_id": "ROUND-2026-10-01-01",
    "start": "2026-10-01T01:00:00Z",
    "end":   "2026-10-01T09:00:00Z",
    "exhibit_ids": ["EX-101", "EX-102"],
    "max_rate_lux_per_second": 1.0,
    "stuck_tolerance_lux": 2.0,
    "min_stuck_seconds": 1800
  }'
```

全部通过时固化合规凭证（`digest` 为规范化输入 + 各项证据摘要的 SHA-256）：

```json
{
  "request_key": "hall-open-2026-10-01-am",
  "round_id": "ROUND-2026-10-01-01",
  "round_digest": "a3f1…",
  "start": "2026-10-01T01:00:00Z",
  "end": "2026-10-01T09:00:00Z",
  "status": "compliant",
  "credential_id": "9c4f…",
  "digest": "b3c1…",
  "exhibits": [
    {
      "requested_id": "EX-101", "canonical_id": "EX-101",
      "compliant": true, "reasons": [],
      "location": {"status": "located",
                   "expected_area": "A区展柜", "actual_area": "A区展柜"},
      "dose": {"dose_limit_luxh": 150.0, "known_dose_luxh": 120.0,
               "remaining_luxh": 30.0, "exceeded": false,
               "first_exceeded_at": null, "unknown_seconds": 0.0,
               "covered_seconds": 28800.0, "closed_seconds": 0.0,
               "reading_count": 17},
      "events": [],
      "open_ticket_ids": []
    },
    {"requested_id": "EX-102", "canonical_id": "EX-102", "…": "同口径证据"}
  ],
  "created_at": "2026-10-01T09:05:00.000Z",
  "already_committed": false
}
```

### 27. 不合规：逐件原因，不写入

任一件不合规即整体 `status=non_compliant`（HTTP 200），逐件给出全部原因，
**不落库**（随后 GET 仍为 404，同一 `request_key` 可在条件修复后重新核验）：

```json
{
  "request_key": "hall-open-2026-10-02-am",
  "round_id": "ROUND-2026-10-02-01",
  "round_digest": "c7d2…",
  "start": "2026-10-02T01:00:00Z", "end": "2026-10-02T09:00:00Z",
  "status": "non_compliant",
  "credential_id": null, "digest": null, "created_at": null,
  "exhibits": [
    {
      "requested_id": "EX-101", "canonical_id": "EX-101",
      "compliant": false,
      "reasons": [
        {"code": "inventory_misplaced",
         "detail": "盘点轮次中定位为错位：预期区域 A区展柜，实际定位 C区库房"},
        {"code": "open_tickets",
         "detail": "存在 1 张未结处置单: 7f3a…"}
      ],
      "location": {"status": "misplaced",
                   "expected_area": "A区展柜", "actual_area": "C区库房"},
      "dose": {"…": "…"}, "events": [],
      "open_ticket_ids": ["7f3a…"]
    }
  ],
  "already_committed": false
}
```

原因代码（一件可有多条）：

| code | 含义 |
|---|---|
| `inventory_missing` | 盘点缺失：预期区域全程无有效读头事件 |
| `inventory_misplaced` | 盘点错位：判定区域与预期区域不同 |
| `inventory_unknown_position` | 位置不明：最后窗口多区域信号差不足阈值 |
| `inventory_unexpected` | 不在轮次预期清单中但被读到（意外出现） |
| `inventory_not_covered` | 轮次既无预期也无观测记录，无法确认位置 |
| `calendar_missing` | 未配置展陈开放日历，无法核算开放剂量 |
| `dose_exceeded` | 截至窗口末的开放剂量已越限 |
| `diagnosis_events` | 窗口内存在 gap / jump / stuck 事件（证据 `events` 中给出明细） |
| `open_tickets` | 存在未结（open）照度异常处置单（`open_ticket_ids` 给出明细） |

### 28. request_key 重放、409 与凭证查询

`request_key` 与（**规范化输入** + **当时证据摘要**）一一绑定：

- 规范化口径：`start` / `end` 统一为 UTC `Z` 结尾；展品 ID 全部解析为
  **规范档案 ID** 并排序（清单顺序、归并别名写法、等价时区写法都不影响
  摘要）；诊断阈值按浮点稳定序列化参与哈希；
- 证据摘要：规范化输入 + 轮次 `request_digest` + 逐件证据（盘点定位结论、
  `calendar=display` 剂量、窗口内诊断事件、未结处置单 ID）的 SHA-256。

```bash
# 相同 request_key + 规范化输入相同 + 证据摘要未变 → 200，重放首次凭证
curl -s -X POST http://localhost:8000/compliance-checks \
  -H 'Content-Type: application/json' -d '{ ... 同一请求 ... }'
# {"...": "...", "already_committed": true}

# 同 request_key 换时间窗 / 轮次 / 展品清单 / 诊断阈值 → 409（规范化输入不同）
# 固化后读数、日历、诊断事件或处置单状态发生变化 → 409（证据摘要已变化），
# 需换用新的 request_key 重新核验

# 查询已固化的合规凭证（从未核验成功 → 404）
curl -s http://localhost:8000/compliance-checks/hall-open-2026-10-01-am
```

- 并发提交相同 `request_key`：只固化一份凭证，竞争方按输入与摘要异同得到
  回放或 409；
- 凭证**只增不改**：落库后绝不 UPDATE；核验不写照度读数、展陈日历、盘点
  轮次、处置单与存证收据链；
- 错误码小结：展品不存在 → 404；盘点轮次未对账 → 404；凭证未固化（GET）
  → 404；展品清单解析后重复 / `end` 不晚于 `start` → 422；`request_key`
  换目标或证据摘要已变化 → 409。

## 核算语义

- **梯形法**：相邻两条权威读数之间照度按线性变化处理，
  剂量 = `(lux_a + lux_b) / 2 × Δt(h)`，按时间顺序累计。
- **缺测段不插值**：相邻读数间隔**超过**最大采样间隔时，该段不参与积分；
  间隔恰好等于上限仍可积分。
- **仲裁冲突处断开**：配置来源优先级后，最高级并列且照度不同的时刻不作为
  积分端点，其前后相邻权威点即使间隔合规也不直接相连；普通剂量与
  `calendar=display` 的开放时段都在冲突处断开，缺测、截至窗口末累计剂量、
  首次越线及各统计量按断点后的各段分别处理（冲突状态仍可在权威时间线查询）。
- **窗口外相邻点参与裁剪**：查询时除窗口内读数外，还会读取窗口起点之前、
  终点之后最近的各一条读数。即使窗口内没有任何采样点，只要窗口完全落在
  两条间隔合规的相邻读数之间，窗口仍按线性照度裁剪为有效梯形段，
  不会被误判为零覆盖。
- **窗口内时间只有两种归属**：被有效积分段覆盖的部分计入
  `covered_seconds`，其余（窗口前/后无读数、跨过缺测段等）全部计入
  `unknown_seconds`，二者之和等于窗口时长。

### 返回字段语义

| 字段 | 口径 | 含义 |
|---|---|---|
| `known_dose_luxh` | **仅所查窗口** `[start, end]` | 窗口内有效梯形段积分得到的已知剂量（lux·h），缺测部分不计 |
| `unknown_seconds` | **仅所查窗口** | 窗口内未被有效段覆盖的时长（秒），不插值；含窗口前/后无读数、跨过缺测段或仲裁冲突断点等；`calendar=display` 时仅统计开放时段内的缺测 |
| `covered_seconds` | **仅所查窗口** | 窗口内被有效段覆盖的时长（秒），与 `unknown_seconds` 之和等于窗口时长；有效段绝不跨越仲裁冲突时刻 |
| `closed_seconds` | **仅所查窗口** | 仅 `calendar=display` 时返回：窗口内处于闭馆时段的时长（秒），此时三者之和等于窗口时长 |
| `remaining_luxh` | **截至窗口末的全历史** | `dose_limit_luxh − 全历史累计剂量`，越线时为负；`calendar=display` 时只累计开放历史 |
| `exceeded` | **截至窗口末的全历史** | 全历史累计剂量是否已达到上限 |
| `first_exceeded_at` | **全历史**（不随窗口变化） | 首次越线的精确时刻；只要该时刻不晚于窗口末就返回，**缩窄查询窗口不会改变它**；截至窗口末尚未越线为 `null` |
| `reading_count` | 窗口内（含端点） | 窗口内读数条数，不含为裁剪而读取的窗口外相邻点 |

- **首次越线时刻**：按全历史逐段累计，在越线段内由"照度线性变化"假设
  解二次方程求得精确时刻（`first_exceeded_at`）。
- **补传自动重算**：剂量不做固化缓存，查询时基于全部已存读数实时计算，
  乱序到达或补传的读数立即反映到结果中。

## RFID 馆藏盘点对账

`POST /inventory-rounds/{round_id}/reconcile` 用读头（RFID 天线）事件对一轮
馆藏盘点做对账，把展品归为**缺失、错位、意外出现、位置不明**四类；结果按
**规范档案 ID 稳定排序**并随轮次固化，可用 `GET /inventory-rounds/{round_id}`
取回。对账**只写盘点轮次表**：不改写照度读数、不生成也不改写存证收据链。

### 请求

| 字段 | 含义 |
|---|---|
| `start_at` | 盘点开始时刻（ISO 8601，缺省时区按 UTC）；固定窗口从该时刻起划分，早于它的事件忽略并计入 `events_before_start_count` |
| `window_seconds` | 固定时间窗长度（秒，必须 > 0，支持小数如 `0.1`）；窗口下标 `floor((ts - start_at) / window_seconds)`，按请求中的**十进制字面量精确分窗**（`0.1` 就是 1/10 秒，而非二进制浮点近似值），**恰好落在边界上的事件归入更高窗口，边界下方哪怕 1 微秒仍留在低窗** |
| `rssi_delta_threshold` | 信号差阈值（≥ 0）：最后窗口出现多个区域时，最强与次强 RSSI 之差**严格小于**阈值记为位置不明；恰好等于仍可定位 |
| `expected` | 展品预期区域清单（`exhibit_id` + `area`）；ID 先做**别名解析**，解析后同一规范档案出现多次 → **422**；不存在 → 404 |
| `events` | 读头事件列表（`exhibit_id`、`area`、`ts`、`rssi`）；展品 ID 同样解析为规范档案；顺序与重复事件不影响结果 |

### 定位口径

1. 从 `start_at` 起按 `window_seconds` 划分固定窗口，忽略早于开始时刻的事件。
   窗口为**半开区间** `[start_at + k·w, start_at + (k+1)·w)`：分窗把时刻换算成
   **整数纳秒 epoch**、把窗长按请求里的十进制字面量解释为**精确有理数**后做
   整数除法，不经过浮点 epoch 相减、也不设任何固定容差——因此
   **恰好落在边界的事件进入更高窗口**（例如 `window_seconds=0.1` 时
   `08:00:00.3` 进第 3 窗），而**边界下方任意小的真实偏移仍留在低窗**
   （如 `08:00:00.299999` 仍在第 2 窗，不会被“吸附”到边界上）；
2. 每件展品在**每个窗口、每个区域**只保留**最高 RSSI**；
3. 取该展品**最后一个有事件的窗口**判定位置：
   - 窗口内只有一个区域 → 直接定位；
   - 多个区域时比较最强与次强 RSSI：差值**不足阈值（严格小于）** → 位置不明；
     恰好等于阈值仍取最强区域；最强 RSSI 相同则按区域标识字典序决胜，
     保证结果确定（事件提交顺序不影响结果）。

### 差异分类（四组互斥，按规范 ID 升序）

- **`missing`（缺失）**：在预期清单中，但盘点时间范围内没有任何有效事件；
- **`misplaced`（错位）**：位置可判定，且判定区域与预期区域不同；
- **`unexpected`（意外出现）**：不在预期清单中、但被读到且位置可判定；
- **`unknown_position`（位置不明）**：最后窗口多区域且最强/次强信号差不足
  阈值；**不在预期清单中的歧义标签也列入本组**（`expected_area` 为 `null`）。
  有事件但位置不明时，本组判定优先于错位——即使最强信号恰好落在错误区域。

位置正确且可判定的展品不出现在任何列表中。

### 17. 提交一轮对账

```bash
curl -s -X POST http://localhost:8000/inventory-rounds/ROUND-2026-09-21-01/reconcile \
  -H 'Content-Type: application/json' \
  -d '{
    "start_at": "2026-09-21T08:00:00Z",
    "window_seconds": 60,
    "rssi_delta_threshold": 5,
    "expected": [
      {"exhibit_id": "EX-001", "area": "A区展柜"},
      {"exhibit_id": "EX-002", "area": "A区展柜"},
      {"exhibit_id": "EX-003", "area": "B区展柜"}
    ],
    "events": [
      {"exhibit_id": "EX-001", "area": "A区展柜", "ts": "2026-09-21T08:00:10Z", "rssi": -48},
      {"exhibit_id": "EX-002", "area": "C区库房", "ts": "2026-09-21T08:01:05Z", "rssi": -55},
      {"exhibit_id": "EX-999", "area": "A区展柜", "ts": "2026-09-21T08:00:30Z", "rssi": -60},
      {"exhibit_id": "EX-003", "area": "B区展柜", "ts": "2026-09-21T08:02:00Z", "rssi": -50},
      {"exhibit_id": "EX-003", "area": "C区库房", "ts": "2026-09-21T08:02:10Z", "rssi": -53}
    ]
  }'
```

```json
{
  "round_id": "ROUND-2026-09-21-01",
  "request_digest": "a3f1…",
  "start_at": "2026-09-21T08:00:00Z",
  "window_seconds": 60.0,
  "rssi_delta_threshold": 5.0,
  "expected_count": 3,
  "observed_count": 4,
  "event_count": 5,
  "events_used_count": 5,
  "events_before_start_count": 0,
  "expectations": [
    {"requested_id": "EX-001", "canonical_id": "EX-001", "expected_area": "A区展柜"},
    {"requested_id": "EX-002", "canonical_id": "EX-002", "expected_area": "A区展柜"},
    {"requested_id": "EX-003", "canonical_id": "EX-003", "expected_area": "B区展柜"}
  ],
  "missing": [],
  "misplaced": [
    {"canonical_id": "EX-002", "expected_area": "A区展柜", "actual_area": "C区库房"}
  ],
  "unexpected": [
    {"canonical_id": "EX-999", "actual_area": "A区展柜"}
  ],
  "unknown_position": [
    {"canonical_id": "EX-003", "expected_area": "B区展柜"}
  ],
  "reconciled_at": "2026-09-21T08:05:00.000Z",
  "already_committed": false
}
```

（示例中 `EX-003` 最后窗口同时在 B 区（-50）与 C 区（-53）出现，差 3 < 阈值 5，
故为位置不明而非错位。）

### 18. 轮次绑定：重放、409 与并发

`round_id` 与请求的**规范化摘要**（`request_digest`，SHA-256）一一绑定。
规范化口径：`start_at` 统一为 UTC `Z` 结尾；展品 ID 全部解析为**规范档案 ID**
（归并别名等价）；预期清单与事件按内容排序、去重（事件乱序、重复事件、
等价时区写法不改变摘要）；浮点经稳定序列化（`float.hex`）参与哈希。

```bash
# 相同请求（或规范化后等价的请求）重放 → 200，回放首次结果
curl -s -X POST http://localhost:8000/inventory-rounds/ROUND-2026-09-21-01/reconcile \
  -H 'Content-Type: application/json' -d '{ ... 同一请求 ... }'
# {"round_id": "...", "request_digest": "a3f1…", ..., "already_committed": true}

# 不同请求（改阈值/窗口/开始时刻/事件/预期等任一规范化字段）→ 409
# HTTP/1.1 409 Conflict
# {"detail": "盘点轮次 ROUND-2026-09-21-01 已绑定另一份对账请求，不能用不同请求重复提交"}

# 取回已固化结果（未对账的轮次 → 404）
curl -s http://localhost:8000/inventory-rounds/ROUND-2026-09-21-01
```

- 首次固化在 `BEGIN IMMEDIATE` 写事务内完成，`round_id` 为主键；**并发提交
  同一轮次**时相同请求只有一份首次结果（竞争方回放，`already_committed=true`），
  不同请求恰好一个成功、其余 **409**；
- 校验失败（404 展品不存在 / 422 预期重复配置或参数非法）整体回滚，
  **不留轮次记录**（随后 GET 仍为 404）；
- 别名在归并后解析到规范档案：预期清单与读头事件中混用同一档案的不同别名
  视为同一展品；`expectations` 同时给出 `requested_id` 与 `canonical_id`；
- 对账不写 `readings`、不写 `ingest_receipts`：照度读数与存证收据链
  （含哈希）在对账前后完全一致，对账也不需要/不触发收据链变化。

## 跨盘点轮次的馆藏区域交接

展品在两轮已固化盘点之间发生馆藏区域交接时，用
`POST /exhibits/{id}/area-handovers` 登记一笔。交接链**只增不改**：每件展品
一条链，链内序号从 1 连续递增，且按盘点轮次的起始时间向前续接。

### 交接核验口径（全部在同一 SQLite 写事务内）

- 请求体：`exhibit_id`（可用归并别名，须与路径 ID 解析到同一规范档案）、
  来源轮 `source_round_id` 与接收轮 `target_round_id`、两端区域
  `source_area` / `target_area`；
- 两轮都必须**已固化对账**（任一未对账 → 404），且接收轮起始时间
  **严格晚于**来源轮（相同或更早 → 422，不允许时间相等）；
- 展品必须**同时列在两轮的预期清单**中，并在各轮**明确定位到所报区域**：
  缺失（无有效事件）、错位（定位区域不同）、位置不明（最后窗口多区域信号差
  不足阈值）或不在预期清单（意外出现）均 **409 不得交接**；所报区域与该轮
  实际定位区域不一致同样 409；
- **跨归并证据解释**：盘点结果固化时按当时的别名关系解析展品 ID，固化后
  **不改写**。档案归并后同一规范档案可能拥有多个身份（自身 + 来源别名），
  提交时服务在同一写事务内把**当前别名关系下指向该规范档案的全部身份**放到
  两轮固化证据中一起解释——归并前固化、旧身份名下的接收轮，自动作为下一笔
  来源轮续接（用目标 ID 或来源别名提交等价）；若同轮固化前列有多个身份，
  须一起核对预期及缺失、错位、位置不明、意外出现信息：**任何一份证据不合格
  或不同身份定位区域冲突都 409，绝不择一放行**（不能只挑与所报区域一致的
  那份身份）；
- **首次交接以来源轮建链**；此后每一笔的来源轮/来源区域必须与上一笔的
  接收轮/接收区域**完全一致**才能续接。旧轮次不能回填、不能分叉、
  不能覆盖当前去向（违反 → 409）；
- 证据核验与写入在同一个 `BEGIN IMMEDIATE` 事务内完成：**并发交接最多一笔
  生效**（竞争方 409），任何失败整体回滚，**不留半笔记录**；
- 每笔交接固化两轮的请求摘要（`source_round_digest` /
  `target_round_digest`，即盘点轮次的 `request_digest`）作为证据。
- 交接只写交接表：原盘点结果、照度读数与存证收据链完全不变。

### 29. 提交一笔交接

先完成两轮盘点对账（见上文“RFID 馆藏盘点对账”），随后：

```bash
# 首次交接：展品在 r1 明确定位于 A 区，在更晚的 r2 明确定位于 B 区
curl -s -X POST http://localhost:8000/exhibits/vase-001/area-handovers \
  -H 'Content-Type: application/json' \
  -d '{
        "exhibit_id": "vase-001",
        "source_round_id": "ROUND-2026-09-01",
        "target_round_id": "ROUND-2026-10-01",
        "source_area": "A-常设展厅",
        "target_area": "B-巡回展厅"
      }'
```

```json
{
  "seq": 1,
  "requested_id": "vase-001",
  "canonical_id": "vase-001",
  "source_round_id": "ROUND-2026-09-01",
  "target_round_id": "ROUND-2026-10-01",
  "source_area": "A-常设展厅",
  "target_area": "B-巡回展厅",
  "source_round_start_at": "2026-09-01T08:00:00Z",
  "target_round_start_at": "2026-10-01T08:00:00Z",
  "source_round_digest": "a3f1…",
  "target_round_digest": "7c2e…",
  "created_at": "2026-10-04T12:00:00.000Z"
}
```

后续交接只能从上一笔的接收轮与区域续接（示例：从 `ROUND-2026-10-01` /
`B-巡回展厅` 继续转到 `ROUND-2026-11-01` 的 `C-库房`）：

```bash
curl -s -X POST http://localhost:8000/exhibits/vase-001/area-handovers \
  -H 'Content-Type: application/json' \
  -d '{
        "exhibit_id": "vase-001",
        "source_round_id": "ROUND-2026-10-01",
        "target_round_id": "ROUND-2026-11-01",
        "source_area": "B-巡回展厅",
        "target_area": "C-库房"
      }'
# {"seq": 2, ... }
```

错误情形（均不写入任何记录）：

```bash
# 接收轮起始时间不晚于来源轮 → 422
# 来源/接收轮未对账 → 404
# 展品在任一轮缺失/错位/位置不明/未列入预期，或所报区域与定位不符 → 409
# {"detail": "展品 vase-001 在接收轮 ROUND-2026-10-01 中缺失、错位、位置不明或未列入预期清单，不得交接"}

# 从上一笔接收轮之外的旧轮回填、或来源区域与当前去向不符 → 409
# {"detail": "交接链只能从上一笔的接收轮 ROUND-2026-10-01 续接，本次来源轮 … 不是当前去向所在轮；旧轮次不能回填覆盖当前去向"}
```

### 30. 查询当前区域与交接历史

```bash
curl -s http://localhost:8000/exhibits/vase-001/area-handovers
```

```json
{
  "requested_id": "vase-001",
  "canonical_id": "vase-001",
  "current_round_id": "ROUND-2026-11-01",
  "current_area": "C-库房",
  "history": [
    {"seq": 1, "source_round_id": "ROUND-2026-09-01", "target_round_id": "ROUND-2026-10-01",
     "source_area": "A-常设展厅", "target_area": "B-巡回展厅",
     "source_round_start_at": "2026-09-01T08:00:00Z",
     "target_round_start_at": "2026-10-01T08:00:00Z",
     "source_round_digest": "a3f1…", "target_round_digest": "7c2e…",
     "created_at": "2026-10-04T12:00:00.000Z"},
    {"seq": 2, "source_round_id": "ROUND-2026-10-01", "target_round_id": "ROUND-2026-11-01",
     "source_area": "B-巡回展厅", "target_area": "C-库房", "...": "..."}
  ]
}
```

`history` 按交接序号（即盘点轮次起始时间）升序；从未交接时
`current_area` / `current_round_id` 为 `null`、`history` 为空数组。
展品 ID 可用归并别名查询。

### 31. 与档案归并的联动

- **单侧已有交接链**时归并：来源链在归并事务内**整链改挂**到目标规范档案
  （序号与全部证据行不变），归并提交结果 `"handover_chain_carried": true`；
  归并后用**目标 ID 或来源别名**查询到的是同一条链、当前去向保持不变；
- **归并后续接沿用归并前固化的接收轮**：链尖那笔的来源/接收轮是在归并前固化
  的，盘点结果中登记的是**当时的来源规范 ID**。归并后下一笔交接的来源轮就是
  这一旧轮——服务在写事务内按**当前别名关系**（目标 ID + 全部来源别名）
  解释两轮固化证据，因此用目标 ID 或来源别名提交都能续接，不会把旧轮次里的
  固化 ID 与当前目标 ID 直接比较而误判 409；
- **同轮多个身份一起核对**：若某轮（在归并前固化时）同时列有归并双方——
  归并后它们成为同一规范档案的多个身份——则该轮证据必须**全部合格**：每份
  身份都明确定位、且定位区域一致并与所报区域相同；任一份缺失 / 错位 /
  位置不明 / 意外出现，或多份身份定位区域冲突，均 **409 不落库，不择一放行**；
- **两侧都有交接链**时拒绝归并：预演结果 `"area_handover_conflict": true`、
  `"ready_to_commit": false`，提交 **409 且不改动任何记录**
  （同时返回两侧笔数 `source_handover_count` / `target_handover_count`）；
- 预演后任一侧新增交接会使预演令牌过期，提交 409。
- 跨归并续接**只 INSERT 新交接行**：原盘点摘要（含旧身份名下的固化结果）与
  旧交接记录（轮次、区域、轮次摘要、固化时刻）一律不改写，归并改挂与查询
  行为保持不变。

```bash
# 场景：DUP-042 归并前已有一笔交接 r1(A)→r2(B)，随后归并进 EX-042（无链）
# 1) 归并前：来源档案先有交接链
curl -s -X POST http://localhost:8000/exhibits/DUP-042/area-handovers \
  -H 'Content-Type: application/json' \
  -d '{"exhibit_id":"DUP-042","source_round_id":"r1","target_round_id":"r2",
       "source_area":"A-常设展厅","target_area":"B-周转厅"}'
# {"seq": 1, "canonical_id": "DUP-042", ...}

# 2) 单侧有链归并：整链改挂到目标
curl -s -X POST http://localhost:8000/merges/commit \
  -H 'Content-Type: application/json' \
  -d '{"token":"<preview-token>","resolutions":[]}'
# {"handover_chain_carried": true, "target_id": "EX-042", "aliases": ["DUP-042"], ...}

# 3) 归并后新一轮盘点（用目标 ID）固化：EX-042 在 r3 明确定位于 C 区
# 4) 沿用归并前固化的接收轮 r2/B 作为来源轮续接——
#    用目标 ID 提交（来源别名 DUP-042 等价）：
curl -s -X POST http://localhost:8000/exhibits/EX-042/area-handovers \
  -H 'Content-Type: application/json' \
  -d '{"exhibit_id":"EX-042","source_round_id":"r2","target_round_id":"r3",
       "source_area":"B-周转厅","target_area":"C-库房"}'
# {"seq": 2, "requested_id": "EX-042", "canonical_id": "EX-042",
#  "source_round_id": "r2", "target_round_id": "r3",
#  "source_round_digest": "<归并前 r2 固化摘要，原样引用>", ...}

# 等价：用来源别名提交与查询
curl -s -X POST http://localhost:8000/exhibits/DUP-042/area-handovers \
  -H 'Content-Type: application/json' \
  -d '{"exhibit_id":"DUP-042","source_round_id":"r2","target_round_id":"r3",
       "source_area":"B-周转厅","target_area":"C-库房"}'
# {"seq": 2, "requested_id": "DUP-042", "canonical_id": "EX-042", ...}
```

同轮多个身份的证据冲突时（例：r2 在归并前把 DUP-042 定位 B、EX-042
定位 A），无论报 B 还是报 A 都 **409**，不会只挑选一致的那份放行：

```bash
curl -i -X POST http://localhost:8000/exhibits/EX-042/area-handovers \
  -H 'Content-Type: application/json' \
  -d '{"exhibit_id":"EX-042","source_round_id":"r2","target_round_id":"r3",
       "source_area":"B-周转厅","target_area":"C-库房"}'
# HTTP/1.1 409 Conflict
# {"detail": "展品 EX-042 在来源轮 r2 固化证据中的多个身份 ['DUP-042', 'EX-042']
#  分别明确定位到不同区域 ['A-常设展厅', 'B-周转厅']，定位区域冲突，不得交接"}
```

```bash
# 单侧有链：归并后别名与目标 ID 查询等价
curl -s http://localhost:8000/exhibits/OLD-SRC-ID/area-handovers   # 别名
curl -s http://localhost:8000/exhibits/NEW-TGT-ID/area-handovers   # 目标
# 两者 history 完全一致

# 双侧有链：
# POST /merges/commit → 409
# {"detail": "来源档案 OLD-SRC-ID 与目标档案 NEW-TGT-ID 都存在跨轮区域交接链（分别为 2 笔与 1 笔），归并被拒绝且不改动任何交接记录：…"}
```

## 展品应急撤离编排

`POST /evacuation-plan` 为一批展品编排应急撤离的**出口、路径与逐段时序**。
一次请求给出计划图与全部待撤离展品，服务按固定规则逐件搜索并直接返回结果
（**纯只读计算**：不写照度读数、不写日历、不写盘点轮次，也不生成或改写存证
收据链；重复调用同一请求结果一致）。

### 请求结构

| 字段 | 含义 |
|---|---|
| `start_at` | 计划起点（绝对时刻，ISO 8601，缺省时区按 UTC）；所有封闭时段都是相对它的整数秒偏移 |
| `nodes` | 全部节点标识（展品起点、通道端点、出口都必须在此声明），不可重复 |
| `edges` | 有向通道列表：`from` / `to` 为通道端点，`travel_seconds` 为**正整数**通行秒数，`capacity` 为**正整数**并发容量，`closures` 为半开封闭时段列表（见下）；同一有向 `(from,to)` 只能定义一次，反向通道是另一条合法通道 |
| `exits` | 出口节点列表（必须存在于 `nodes`，不可重复） |
| `exhibits` | 待撤离展品：`exhibit_id`（ID 或归并别名，先解析为规范档案）、`start_node`、`priority`（整数，越大越早编排） |

每条 `closures` 元素为 `{"start": s, "end": e}`，表示相对计划起点的**半开**
封闭时段 `[s, e)`（要求 `s < e`，均为非负整数）；重叠或首尾相接的时段自动
合并。

### 编排口径

1. **逐件顺序**：展品按**优先级降序、规范 ID 升序**排序后逐件编排；每件展品
   在已编排展品已确定的占用计划之上做一次**时变最早到达搜索**（边代价满足
   FIFO 的 Dijkstra），选出最早到达的出口与路径，随后把本件展品的各段占用
   提交，再编排下一件。
2. **半开占用**：展品在 `t` 进入通道、`t + travel_seconds` 离开，占用区间为
   **`[进入, 离开)`**：恰好在他人**离开整点**进入不冲突（不计入并发），恰好在
   自己离开整点由下一件占用也不冲突。
3. **等待规则**：到达通道起点后若不能立即进入，则等待到**首个可行进入时刻**：
   - **容量按同一时刻的实际并发判断**：对候选窗口 `[t, t+travel)` 内每个
     瞬间统计同时在通道内的已编排展品数，只有加入本件后某个瞬间会达到
     `capacity`（即旧占用的瞬时并发峰值已等于容量）才需等待；仅与窗口
     **重叠的占用条数**达到容量并不构成阻塞——多条占用彼此错峰时，窗口内
     任一瞬间的并发可能远低于条数（例如容量 3，窗口与 `[1,11)`、`[10,20)`、
     `[19,29)` 三条占用都重叠，但任一时刻最多 2 件在场，新展品仍可在 10
     整点进入）。确需等待时至少等到与窗口重叠的占用中最早一条的**离开整点**
     （造成饱和的那批占用在此刻之前都不会释放，故不会跳过更早空档）；
   - 进入时刻 `t` 落在封闭时段 `[cs, ce)` 内 → 至少等到该时段结束 `ce`
     （**右端点不含**：`ce` 整点即可进入）；
   - 两类约束同时存在时取较晚的候选时刻；**每次因容量或封闭推进候选时刻后，
     都从头重新检查容量与封闭两类约束**，迭代直到两者都满足，因此返回的是
     真正的首个可行时刻，不会越过中间更早的空档。
4. **封闭只约束“进入”**：在封闭开始之前进入通道、通行过程跨越封闭时段是
   允许的；封闭时段有限，因此无绕行路径时算法等待封闭结束后继续，不会
   误判为无路径。
5. **平局决胜**：多个出口/路径到达时刻相同的方案，按**完整节点路径的字典序**
   决胜（逐节点比较，不是比较段数）；结果与通道在请求中的排列顺序无关。
6. **无出口路径**：仅当从展品起点按有向连通性（忽略容量与封闭）无法到达任何
   出口时返回 **422**。

### 19. 提交一次撤离编排

```bash
curl -s -X POST http://localhost:8000/evacuation-plan \
  -H 'Content-Type: application/json' \
  -d '{
    "start_at": "2026-09-26T08:00:00Z",
    "nodes": ["A", "B", "C", "E"],
    "edges": [
      {"from": "A", "to": "B", "travel_seconds": 5, "capacity": 1,
       "closures": [{"start": 30, "end": 120}]},
      {"from": "B", "to": "E", "travel_seconds": 3, "capacity": 1},
      {"from": "A", "to": "C", "travel_seconds": 2, "capacity": 1},
      {"from": "C", "to": "E", "travel_seconds": 20, "capacity": 1}
    ],
    "exits": ["E"],
    "exhibits": [
      {"exhibit_id": "EX-001", "start_node": "A", "priority": 10},
      {"exhibit_id": "EX-002", "start_node": "A", "priority": 5}
    ]
  }'
```

高优先级的 `EX-001` 走 `A→B→E`（8 秒）并占用两段通道；`EX-002` 随后在
`A→B` 前等待 5 秒（半开释放），13 秒完成：

```json
{
  "start_at": "2026-09-26T08:00:00Z",
  "results": [
    {
      "requested_id": "EX-001", "canonical_id": "EX-001",
      "start_node": "A", "priority": 10,
      "exit_node": "E", "path": ["A", "B", "E"],
      "legs": [
        {"from": "A", "to": "B", "travel_seconds": 5,
         "enter_offset_seconds": 0, "leave_offset_seconds": 5,
         "enter_at": "2026-09-26T08:00:00Z",
         "leave_at": "2026-09-26T08:00:05Z", "wait_seconds": 0},
        {"from": "B", "to": "E", "travel_seconds": 3,
         "enter_offset_seconds": 5, "leave_offset_seconds": 8,
         "enter_at": "2026-09-26T08:00:05Z",
         "leave_at": "2026-09-26T08:00:08Z", "wait_seconds": 0}
      ],
      "wait_seconds": 0,
      "completion_offset_seconds": 8,
      "completed_at": "2026-09-26T08:00:08Z"
    },
    {
      "requested_id": "EX-002", "canonical_id": "EX-002",
      "start_node": "A", "priority": 5,
      "exit_node": "E", "path": ["A", "B", "E"],
      "legs": [
        {"from": "A", "to": "B", "travel_seconds": 5,
         "enter_offset_seconds": 5, "leave_offset_seconds": 10,
         "enter_at": "2026-09-26T08:00:05Z",
         "leave_at": "2026-09-26T08:00:10Z", "wait_seconds": 5},
        {"from": "B", "to": "E", "travel_seconds": 3,
         "enter_offset_seconds": 10, "leave_offset_seconds": 13,
         "enter_at": "2026-09-26T08:00:10Z",
         "leave_at": "2026-09-26T08:00:13Z", "wait_seconds": 0}
      ],
      "wait_seconds": 5,
      "completion_offset_seconds": 13,
      "completed_at": "2026-09-26T08:00:13Z"
    }
  ],
  "total_completion_offset_seconds": 13
}
```

- 每段 `legs` 同时给出相对秒（`enter_offset_seconds` /
  `leave_offset_seconds`）与绝对时刻（`enter_at` / `leave_at`，UTC `Z`
  结尾）；`wait_seconds` 是**进入该段之前**的等待秒数，展品级 `wait_seconds`
  为各段之和；
- 展品起点本身即出口时返回零段路径，`completion_offset_seconds = 0`；
- 顶层 `total_completion_offset_seconds` 为所有展品中最晚的完成时刻
  （最后一件到达出口的相对秒）。

### 20. 别名解析与错误码

- 请求中的展品 ID 与其他接口一样先做**归并别名解析**：响应逐项给出
  `requested_id` 与 `canonical_id`；同一件展品以规范 ID 与别名（或两个不同
  别名）重复出现，解析后同一规范档案出现多次 → **422**；
- 展品 ID 不存在 → **404**（先于重复项校验逐个解析，错误信息列出全部未知 ID）；
- 非法图 → **422**：节点重复、出口重复或不在 `nodes` 中、通道端点未知、
  同一有向通道重复定义、展品起点未知、`travel_seconds` / `capacity` 非正、
  封闭时段 `start >= end` 或为负；
- 某展品起点**不存在可达任何出口的有向路径** → **422**（错误信息注明展品与
  起点）；
- 编排不读不写任何业务数据表，`readings` / `exhibit_calendars` /
  `inventory_rounds` / `ingest_receipts` 在调用前后完全一致（编排接口本身
  也不创建任何新表）。

```bash
# 无出口路径（只有反向通道 B->A，从 A 无法到出口 B）→ 422
curl -i -X POST http://localhost:8000/evacuation-plan \
  -H 'Content-Type: application/json' \
  -d '{"start_at":"2026-09-26T08:00:00Z","nodes":["A","B"],
       "edges":[{"from":"B","to":"A","travel_seconds":1,"capacity":1}],
       "exits":["B"],
       "exhibits":[{"exhibit_id":"EX-001","start_node":"A","priority":1}]}'
# HTTP/1.1 422 Unprocessable Entity
# {"detail":"展品 EX-001（规范档案 EX-001）的起点 A 不存在可达任何出口的有向路径"}
```

## SQLite 在线快照（创建 / 列表 / 下载 / 校验）

服务可对线上 SQLite 数据库创建**事务一致的在线快照**，流程与保证如下：

- 创建时用 Python 标准库 `sqlite3.Connection.backup` **在线热备**，得到
  事务一致的数据库副本（备份期间线上库可继续读写）；副本先写入**临时文件**
  `N.db.tmp`，完成后流式计算 **SHA-256**，读取**页数**与**字节数**，
  生成包含**快照编号、创建时间、页数、字节数、SHA-256** 的清单 `N.json`；
  db 与清单都先写临时文件，再通过 `os.replace` **原子重命名发布**
  （清单重命名完成即为对外可见的发布点）；
- 快照编号从 1 起、按已发布快照连续递增；快照在发布前会把 journal 模式切回
  `DELETE`，因此每份快照都是**单一自包含 `.db` 文件**（无 `-wal`/`-shm`
  旁路文件），可直接下载、独立打开；
- **并发创建只允许一个请求执行**：同一时刻已有创建任务时，其余请求立即返回
  **409**，不会产生多个半成品；任何失败都会清理临时文件，**失败或未完成的
  文件绝不会出现在列表中**，编号也不会被占用；
- 校验在**独立的只读连接**（`file:...?mode=ro` + `PRAGMA query_only`）上对
  快照文件进行，依次核对文件 SHA-256、字节数、页数，并执行
  `PRAGMA integrity_check` 与 `PRAGMA foreign_key_check`，**逐项返回结果**；
  校验既不打开线上数据库、也不修改任何库文件。
- 快照目录使用**独立 Docker 命名卷** `lightdose-snapshots`
  （容器内 `/snapshots`），与数据库卷 `lightdose-data` 分离。

### 13. 创建快照

```bash
curl -s -X POST http://localhost:8000/snapshots
```

```json
{
  "seq": 1,
  "created_at": "2026-09-21T13:06:40.710Z",
  "sha256": "e1141e13804148e1037e3c90e6d08a2bebabc740d03dcb54c57ef42a65479f84",
  "pages": 23,
  "bytes": 94208,
  "download_url": "/snapshots/1/download"
}
```

已有创建任务进行中时返回 **409**：

```bash
curl -i -X POST http://localhost:8000/snapshots
# HTTP/1.1 409 Conflict
# {"detail":"已有快照创建任务正在执行，请稍后重试"}
```

### 14. 列出 / 查询快照

```bash
curl -s http://localhost:8000/snapshots
# {"snapshots":[{"seq":1,"created_at":"...","sha256":"...","pages":23,
#                "bytes":94208,"download_url":"/snapshots/1/download"}]}

curl -s http://localhost:8000/snapshots/1     # 单个清单；未发布 → 404
```

### 15. 下载快照

```bash
curl -s http://localhost:8000/snapshots/1/download -o lightdose-snapshot-1.db
sha256sum lightdose-snapshot-1.db
# e1141e13804148e1037e3c90e6d08a2bebabc740d03dcb54c57ef42a65479f84  lightdose-snapshot-1.db

# 下载文件是合法、自包含的 SQLite 数据库，可独立只读打开
sqlite3 'file:lightdose-snapshot-1.db?mode=ro' 'PRAGMA integrity_check;'
# ok
```

响应头携带 `Content-Type: application/vnd.sqlite3` 与
`Content-Disposition: attachment; filename="lightdose-snapshot-1.db"`；
不存在的编号返回 **404**。

### 16. 校验快照

```bash
curl -s http://localhost:8000/snapshots/1/verify
```

```json
{
  "seq": 1,
  "ok": true,
  "snapshot": {"seq": 1, "created_at": "...", "sha256": "...",
               "pages": 23, "bytes": 94208, "download_url": "/snapshots/1/download"},
  "checks": [
    {"name": "sha256", "ok": true,
     "detail": {"expected": "e114…9f84", "actual": "e114…9f84", "algorithm": "SHA-256"}},
    {"name": "bytes", "ok": true, "detail": {"expected": 94208, "actual": 94208}},
    {"name": "page_count", "ok": true, "detail": {"expected": 23, "actual": 23}},
    {"name": "integrity_check", "ok": true, "detail": {"result": ["ok"]}},
    {"name": "foreign_key_check", "ok": true, "detail": {"violations": []}}
  ]
}
```

- `ok` 为五项结果的合取；任一项不通过即为 `false`。
- `sha256` / `bytes` / `page_count` 的 `detail` 给出 `expected`（清单值）与
  `actual`（当前文件实测值），文件被篡改时哈希首先不符：

  ```json
  {"name": "sha256", "ok": false,
   "detail": {"expected": "e114…9f84", "actual": "9ab0…7d12", "algorithm": "SHA-256"}}
  ```
- `integrity_check` 不通过时 `detail.result` 列出全部问题（通过时为 `["ok"]`）；
- `foreign_key_check` 不通过时 `detail.violations` 逐条给出
  `table` / `rowid` / `parent` / `fkid`；通过时为空列表：

  ```json
  {"name": "foreign_key_check", "ok": false,
   "detail": {"violations": [{"table": "readings", "rowid": 4,
                              "parent": "exhibits", "fkid": 0}]}}
  ```

整个校验过程只读快照文件，**不会修改线上数据库**，也不会改动快照本身。

## 本地开发与测试

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q                      # 运行测试
uvicorn app.main:app --reload  # 本地起服务，默认 http://127.0.0.1:8000
```

本地运行时数据库默认写到 `./lightdose.db`，可用环境变量 `DB_PATH` 覆盖；
快照默认写到 `./snapshots`，可用环境变量 `SNAPSHOT_DIR` 覆盖（容器内为
`/snapshots`，挂载在独立命名卷 `lightdose-snapshots` 上）。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DB_PATH` | `./lightdose.db`（容器内 `/data/lightdose.db`） | SQLite 文件路径 |
| `SNAPSHOT_DIR` | `./snapshots`（容器内 `/snapshots`） | 快照目录（db + 清单），容器内挂载独立命名卷 |

## 目录结构

```
├── app/
│   ├── main.py      # FastAPI 路由：展品 / 读数 / 存证收据 / 剂量查询 / 采集质量诊断 / 展陈日历 / 档案归并 / 来源优先级 / 权威时间线
│   ├── dose.py      # 核算核心：梯形法、缺测段、首次越线求解、开放区间裁剪
│   ├── diagnose.py  # 采集质量诊断：同值合并 / 时间冲突 / gap / jump / 最大 stuck、完整时间线与窗口筛选
│   ├── arbitration.py # 多来源读数仲裁：来源优先级版本、逐时刻权威值选取、并列冲突、诊断输入展开
│   ├── calendar.py  # 展陈开放日历：每周规则 + 日期例外展开、跨午夜、夏令时边界解析
│   ├── merge.py     # 重复档案归并：预演、版本令牌、单事务提交、永久别名、归并前收据链校验、来源配置合并
│   ├── inventory.py # RFID 盘点对账：固定窗口最高 RSSI 定位、信号差阈值判不明、四类差异、请求摘要绑定与并发固化
│   ├── evacuation.py # 应急撤离编排：时变最早到达搜索、[进入,离开) 半开容量计数、封闭等待/绕行、路径字典序决胜（纯只读）
│   ├── receipts.py  # 照度上报存证：收据规范序列化、SHA-256 链哈希、整链校验
│   ├── corrections.py # 上报批次更正：作废收据中实际插入的读数、request_key 幂等、前置变化整次 409
│   ├── tickets.py   # 照度异常处置单：创建时单事务重算诊断/剂量/链尾并固化快照，结案重算核对（409 不写入）、resolved 举证
│   ├── compliance.py # 展厅开放批次合规核验：单事务核验（盘点定位/开放剂量/诊断事件/未结处置单），全过才固化凭证，request_key 幂等
│   ├── snapshots.py # SQLite 在线快照：backup 热备、临时文件 + SHA-256 清单 + 原子重命名、只读校验
│   ├── db.py        # SQLite 连接与建表（exhibits / readings / ingest_receipts / exhibit_aliases / merge_previews / 日历四表 / inventory_rounds / reading_corrections / source_priority_versions / illumination_tickets / compliance_credentials）
│   └── schemas.py   # Pydantic 请求/响应模型
├── tests/
│   ├── test_api.py       # 剂量核算与归并
│   ├── test_diagnose.py  # 照度采集质量诊断
│   ├── test_calendar.py  # 展陈开放日历
│   ├── test_arbitration.py # 多来源仲裁：source_id 留存、优先级版本/生效时刻、并列冲突、权威时间线、归并合并配置
│   ├── test_receipts.py  # 照度上报存证：收据链、整链校验、并发顺序、归并保留
│   ├── test_corrections.py # 上报批次更正：仅作废 inserted、重放/换目标 409、链/归并/归属变化 409、查询排除
│   ├── test_inventory.py # RFID 盘点对账：窗口定位、四类差异、别名解析、摘要重放/409、并发固化、隔离性
│   ├── test_evacuation.py # 应急撤离：容量竞争、半开封闭等待/绕行、路径字典序平局、别名重复 422、无出口 422、只读隔离
│   ├── test_tickets.py   # 照度异常处置单：快照固化与 digest、request_key 重放/换目标 409、结案核对 409、resolved 举证、并发
│   ├── test_compliance.py # 展厅开放批次合规核验：凭证固化、逐件失败原因、重放/换目标/证据变化 409、别名、并发
│   └── test_snapshots.py # 在线快照：事务一致副本、409 并发、原子发布、下载、只读校验与防篡改
├── requirements.txt         # 运行时依赖（含 tzdata，保证容器内 IANA 时区数据库可用）
├── requirements-dev.txt     # 测试依赖
├── Dockerfile
├── docker-compose.yml
└── README.md
```
