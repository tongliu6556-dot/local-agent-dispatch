# Local Agent Dispatch Research Program Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and scientifically evaluate a local-first Agent Operating System that converts a human mission into a typed task graph, jointly schedules models, quota windows, hardware, storage, network routes, and human review, executes only authorized work durably, and learns from verifiable outcomes.

**Architecture:** Keep a modular monolith and SQLite source of truth through the research phase. Separate Mission/Human, World-State, Scheduler/Policy, Execution/Communication, Provenance/Learning, and Diagnosis/Safety boundaries; introduce distributed messaging or external workflow engines only after local transactional semantics and replay tests pass.

**Tech Stack:** Python 3.10+, stdlib-first core, JSON Schema, SQLite WAL, optional psutil/hwloc/node-exporter probes, SSH for bounded transport, optional vLLM/Ollama/llama.cpp runtimes, provider-free simulator and fixtures, OpenTelemetry/OpenLineage/PROV-compatible projections, unittest/pytest-compatible tests.

---

## 中文执行摘要

### 这项研究到底要解决什么

本计划研究的不是“怎样调用更多模型”，而是：在模型五小时/周额度、
不同机器和挂载盘、网络路径、任务依赖、用户审查时间与科学证据边界同时
变化时，系统能否比人工选择或静态规则，用更低的综合成本持续产生更多
**经过验证的有效进展**。

这里的“完成”必须由测试、编译、数值不变量、artifact hash、独立审查或
人工证据门确认；Agent 自己声称完成、出现一个 PID、目录里有一个文件，
都不能单独作为完成证据。

### 当前项目处于什么位置

项目已经有比较扎实的 provider-free 控制面骨架：本机优先扫描、Provider
目录和额度池、任务 DAG、P50/P90 估算、模型/主机联合规划、SQLite WAL、
lease/fence、远程 worker、180 秒监控和受审核波次闭环。

但距离 24 小时、多机器、多 Agent 的自适应系统还有六个核心缺口：

1. 自然语言任务还不能稳定编译成带权限、claim boundary 和 CPS 的规范；
2. 主机资源仍不是完整的 Host-Mount-Route-Runtime 数字孪生；
3. planner 的资源选择和 controller 的实际 claim 还没有共享同一事务 reservation；
4. quota reset、deadline、用户在线时间和强模型预留没有进入时序优化；

5. Provider 能力、额度端点和 CLI 行为会随版本变化，单靠本机目录扫描
   或盲目请求无法区分“可见、已认证、可达、真实接受、额度可读”。因此需要
   一个独立的 Evidence Discovery and Compatibility Resolver：先检索官方
   文档/源码/版本/issue，再进行有授权、有限制、可审计的只读探测，将结果
   作为带来源、版本、TTL、置信度和冲突标记的证据输入调度器。
5. 真实历史缺少统一事件、额度、资源、模型和 artifact 因果账本；
6. 用户仍需要阅读过多日志，关键节点、解释和决策队列没有进入控制环。

### 研究总体架构

计划把系统划成六个平面，但研究阶段仍保持模块化单体，不急于拆微服务：

- **Mission/Human Plane**：目标、非目标、科学边界、Git 权限、关键节点；
- **World-State Plane**：模型池、额度、主机、挂载盘、网络、运行时和用户时间；
- **Scheduler/Policy Plane**：任务分解、模型/Agent 数量、主机、路径和开始时间；
- **Execution/Communication Plane**：本机 CLI、SSH worker、本地模型与 artifact 数据面；
- **Provenance/Learning Plane**：事件、估算与实际值、模型结果和 CPS 效果；
- **Diagnosis/Safety Plane**：故障归因、stage gate、人工批准和安全降级。

SQLite/Postgres 继续承担事务真相；Graph、OpenTelemetry、OpenLineage 和
PROV-O 都只是可重建的查询/展示投影。

### 十个核心研究问题

| 编号 | 问题 | 预注册目标 |
| --- | --- | --- |
| RQ0 | 能否完整记录任务和副作用因果链？ | 100% 不可逆副作用可追溯；至少 99% attempt 有合法终态或删失状态 |
| RQ1 | 长提示词能否安全编译为 MissionSpec？ | 冻结语料中硬约束召回率 100%，不静默扩大权限或科学 claim |
| RQ2 | P50/P90 资源估算是否优于固定阈值？ | P90 覆盖率 85%-95%，灾难性低估低于 1% |
| RQ3 | 模型、主机、挂载盘和开始时间联合选择是否更好？ | 中位 makespan 至少降低 15%，额度违规为 0 |
| RQ4 | 动态模型路由是否真正节省额度？ | 验证成功率非劣界 -3 个百分点，归一化额度至少降低 20% |
| RQ5 | 动态 Agent 数量是否提高吞吐？ | 吞吐至少提高 20%，无不可恢复写冲突 |
| RQ6 | CPS 编译是否减少无效 context？ | context/token 至少降低 25%，成功率不越过非劣界 |
| RQ7 | 聊天、SSH、worker 或 controller 中断后能否安全续作？ | 不丢已接收任务，不重复不可逆副作用 |
| RQ8 | 关键节点审查是否降低信息负担？ | 人工时间或中断数降低 30%，关键错误发现不显著下降 |
| RQ9 | 历史 outcome 能否改善未来调度？ | 冻结未来窗口 regret 下降，学习策略先 shadow 再 canary |

这些阈值是研究目标，不是当前结果。provider-free pilot 估计方差后冻结，
不能看到真实结果后再移动门槛。

### 十二个工作包

| 工作包 | 核心产物 | 为什么先做/后做 |
| --- | --- | --- |
| WP0 | Research Protocol、benchmark、数据治理 | 先固定怎样测量，避免后验挑指标 |
| WP1 | Event & Provenance v2、legacy importer | 没有真相层就不能学习或归因 |
| WP2 | MissionSpec v2、ClaimContract | 把长提示词变成可审核任务规范 |
| WP3 | CPS Compiler、Agent 角色/审查拓扑 | 给不同子任务最小且可追溯的执行胶囊 |
| WP4 | Resource Digital Twin、RouteEvidence | 解决根目录误判、挂载盘、VRAM、网络路径问题 |
| WP5 | Temporal Scheduler、事务 reservation | 联合额度、硬件、时间、并发和人工容量 |
| WP6 | Model Router、Outcome Registry | 研究低成本 worker 到强模型审查的级联 |
| WP7 | Durable Outbox、Remote Continuity | 支持断网/断额/重启和服务器间协调 |
| WP8 | Incident Analysis、Human Control Surface | 降低用户信息负担并建立问题分析系统 |
| WP9 | Replay Simulator、fault injection、统计分析 | 先在可控世界里证伪算法 |
| WP10 | Shadow、canary、24h soak、科学案例 | 从建议模式逐级开放真实执行 |
| WP11 | License、scrub、跨平台 CI、GitHub alpha | 所有核心安全门通过后再公开发布 |

WP1、WP2、WP4、WP9 可以在 WP0 后并行；WP5 必须等待任务、资源、账本和
模拟器契约稳定；真实 Provider、SSH 与 24 小时运行都在 replay 和 shadow
之后，不作为第一轮研究入口。

### 基准与对照

任务分四层：确定性操作、单文件代码、多文件集成、研究/高风险任务。
每个任务都标注推理复杂度、领域性、context、工具复杂度、可验证性、
可逆性、破坏半径、claim 风险、DAG 宽深、资源需求、数据位置、deadline
和人工审查要求。

模型路由至少比较：人工选择、始终最强、始终最便宜、round-robin、当前
启发式、只看任务标签、状态感知规则、校准路由和 hindsight oracle。

并发/放置至少比较：串行、固定 N、只看 Provider、只看硬件、HEFT、
模型与硬件分开选择、联合时序调度和 hindsight oracle。

CPS 至少比较：原始提示词、全量 context、通用 Agent、当前 Skill、人工
CPS、自动 CPS、自动 CPS 加独立 reviewer。

### 证据晋级路线

```text
协议与 schema
  -> provider-free replay
  -> shadow mode（只建议不执行）
  -> 低风险可逆 canary
  -> 故障注入
  -> 2 小时 / 8 小时 / 三次 24 小时 soak
  -> 限定用户 alpha
  -> GitHub public alpha
```

任何阶段出现隐私泄露、未授权副作用、重复不可逆操作、科学 claim 扩大、
资源/额度硬约束违规，都退回 shadow，不能靠更多重试掩盖。

### 第一轮研究冲刺

第一轮只生成六个设计产物，不消耗真实模型额度：

1. `docs/research/protocol-v1.md`
2. `docs/research/benchmark-taxonomy-v1.md`
3. `schemas/mission_spec.schema.json`
4. `schemas/world_state.schema.json`
5. `schemas/provenance_event.schema.json`
6. `docs/research/fault-injection-matrix-v1.md`

用户提出的 FEM/MPB/PWE 示例作为第一条 golden mission：只编译出目标、
claim envelope、DAG、CPS、资源/路由未知项、Git 权限和人工 gate，不启动
Provider、SSH、下载、提交或科学计算。

### 示例任务的结构化编译：FEM/MPB/PWE

这段示例提示词不是最终执行协议，而是一个很好的 `MissionSpec` 编译
样本。它同时表达了目标、模型偏好、并行意图、服务器偏好、代码修改权限、
科学结果边界和后续研究方向；如果直接把它当作一个 prompt，调度器无法
判断哪些工作能并行、哪些文件可以同时写、什么时候必须升级模型、以及
什么结果可以被称为“完成”。

| 自然语言片段 | 编译后的字段 | 不能默认猜测的内容 |
| --- | --- | --- |
| 调用 Antigravity Gemini、OpenCode Go DeepSeek Flash | `provider_policy`, `model_policy`, `pool_policy` | 精确模型 ID、variant、实时目录、共享池剩余额度 |
| 智能程度选最高 | `quality_floor=highest_advertised` | 不得把“最高”伪装成未验证的 `ultra` 或任意 suffix |
| 可并行的工作交给 DeepSeek Flash | `parallelism_hint`, `allowed_model_override` | DeepSeek 必须是用户对 OpenCode Go 的显式例外，不得变成独立 DeepSeek 后端 |
| 该提交代码就提交代码 | `git_authority`, `commit_gate` | 分支、提交范围、是否允许 push/merge 必须由用户或 MissionSpec 明确 |
| 尽量去服务器 | `placement_preference=server_first` | 服务器是否有正确挂载盘、运行时、路由、许可证和写权限 |
| 接入 FEM/MPB/PWE、LDL/inertia、localizer index | `deliverables`, `task_graph`, `validators` | 具体接口、数值不变量、输入输出 schema、测试 fixture |
| 当前不声称连续 Maxwell、全 BZ 或 Chern 数 | `claim_envelope`, `non_goals`, `promotion_gate` | 任何 Agent 都不能在后续总结中扩大这些 claim |

建议把它编译成下面的任务图，而不是一次性开一组无边界 Agent：

```text
S0  system/provider/host/mount/route preflight
 |
 +--> S1a FEM output adapter      --+
 +--> S1b MPB output adapter      --+--> S2 interface/schema integration
 +--> S1c PWE output adapter      --+        |
 +--> S1d LDL/inertia prototype   --+        +--> S3 localizer index
                                          |
                                          +--> S4 deterministic tests + numerical checks
                                                   |
                                                   +--> S5 independent review
                                                            |
                                                            +--> S6 human commit gate
```

其中 S1a-S1d 只有在输入、输出和写域互不相交时才并行；S2、S4、S6 是
串行或受控 fan-in。每个节点必须携带 `input_digest`、`output_paths`、
`write_scope`、`validator`、`model/pool`、`execution_host`、
`workload_host`、`resource_estimate` 和 `claim_effect`。服务器只承接
数据、数值计算、测试或批量任务；桌面认证 Provider CLI 仍在本机运行，
不能因为 `server_first` 就把桌面凭据带到服务器。

这条 golden mission 的最小完成条件是：接口适配器、schema、测试、
LDL/inertia 与 localizer index 的实现和验证证据齐全；所有结果均标注为
toy、public-data、simulator 或 bounded numerical evidence；明确列出尚未
覆盖的连续 Maxwell、全 BZ、Chern 数和真实物理结论。任何需要新数据、模型、
环境或长时间计算的节点必须先经过资源/路线 gate，不能由 Agent 在执行中
自行下载或扩大工作范围。

### 面向不同任务的泛化规则

以后每个自然语言请求都先经过同一编译器，至少产出：

1. `MissionSpec`：目标、非目标、交付物、难度向量、deadline、预算和权限；
2. `TaskGraph`：依赖、可并行波次、关键路径、写域和回滚边界；
3. `CPSBundle`：每个角色的 context、prompt contract、Skill、工具白名单、
   输出 schema、停止条件和审查角色；
4. `WorldState`：模型池/额度窗口、主机/挂载盘/VRAM、运行时、路由、
   外部消费者和用户在线时间；
5. `ClaimContract`：允许声称、暂不声称、需要哪类验证才能晋级；
6. `ExecutionPlan`：模型、Agent 数量、并发、时间窗、服务器、传输方式、
   reservation、monitor interval 和 fallback；
7. `EvidenceLedger`：每次决策、调用、资源观测、artifact、validator、
   人工审批和失败归因的可追溯记录。

这样“任务难度”不会只映射成一个模型名，而是映射成质量下限、验证强度、
Agent 拓扑、上下文大小、并行宽度、服务器资源和人类审查预算的联合约束。

## 1. Program thesis

The research target is the complete control loop, not a single model router:

```text
Observe -> Compile -> Plan -> Reserve -> Execute
        -> Validate -> Review -> Learn -> Replan
```

The central research question is:

> Under multiple model quota windows, heterogeneous machines, changing network
> routes, task dependencies, human attention limits, and scientific evidence
> boundaries, can local-agent-dispatch produce more validated progress per unit
> of quota, time, money, and compute than manual selection or static rules?

`Validated progress` means an artifact accepted by a deterministic validator,
test suite, numerical invariant, bounded independent review, or explicit human
evidence gate. An agent response, running PID, catalog entry, or nonempty file is
not sufficient completion evidence.

## 2. Starting point and evidence boundary

The current checkout is a pre-alpha control-plane prototype:

- P3 provider-free planning is the strongest area: task capture, conservative
  DAGs, P50/P90 estimates, shared quota pools, model/host placement, rolling
  horizon, writable-mount selection, monitor and reviewable replan.
- SQLite WAL, fencing, leases, retry backoff, process-group timeout, remote spool,
  artifact hashing, and fake closed-loop tests provide useful reliability
  primitives.
- Real historical runs are dominated by legacy JSON layouts rather than the new
  SQLite path, and their event/model/quota/resource attribution is incomplete.
- Real-provider automatic replan, resource reservations shared by planner and
  controller, reset-aware 24-hour scheduling, complete resource topology,
  CPS compilation, outcome learning, and a low-load human control surface are
  not yet established.
- The directory is not currently a Git repository. Logical commit messages in
  this plan become executable only after the user approves repository
  initialization and license selection.

The 2026-08-12 metadata-only history audit establishes the migration baseline:

| Observation | Baseline |
| --- | --- |
| Runtime corpus | about 2.435 GB, 13,948 files, 91 run directories |
| Run coverage | 25 runs with state, 14 with events, 36 with execution plans |
| Durable backend evidence | no historical SQLite run database observed; legacy JSON dominates |
| State compatibility | 77 `state.json` files with 16 top-level shapes |
| Attempt closure | 59 unique starts, 44 matched terminal events, 15 without terminal events, 7 duplicate event rows |
| Liveness drift | 40 recorded PID files pointed to exited processes; stale running/queued states remained |
| Routing evidence | 28 of 63 starts lacked exact model; task type/difficulty appeared in 6 of 65 jobs; resource estimates in 4 of 65 |
| Quota attribution | saved monitor reports contained no usable per-attempt quota updates |
| Storage pressure | repeated Git packs and temporary clones dominate retained bytes |

These observations justify the ledger, reconciler, content-addressed artifact,
and retention work. They do not justify historical model-quality rankings.

Use the following evidence ladder throughout the program:

| Level | Evidence | Permitted claim |
| --- | --- | --- |
| E0 | schema/design review | coherent specification only |
| E1 | provider-free fixture/replay | control-plane behavior under modeled inputs |
| E2 | bounded real provider or SSH canary | observed behavior for that exact run and environment |
| E3 | repeated fault injection and 24-hour soak | bounded reliability evidence for tested scenarios |
| E4 | clean-clone, cross-platform, public reproducibility | public alpha engineering evidence |

No E1 result may be described as real-provider, real-GPU, scientific, or
production evidence. No model ranking may be inferred from old runs unless task,
model, effort, quota attribution, validation, and environment are comparable.

## 3. Program scope

### In scope

1. Natural-language mission compilation into a typed and reviewable contract.
2. Task DAG, roles, write ownership, integration nodes, and stage gates.
3. Context/Prompt/Skill capsule compilation and provenance.
4. Model capability, effort, shared quota, cost, and outcome calibration.
5. CPU/RAM/GPU/VRAM/storage/network/runtime resource digital twin.
6. Joint model, agent-count, host, mount, route, and start-time scheduling.
7. Durable local and SSH execution of already-authorized task packets.
8. Event sourcing, artifact lineage, incident diagnosis, and policy learning.
9. Human attention scheduling, milestone summaries, approve/edit/reject flow.
10. Provider-neutral, offline-first open-source packaging and evaluation.

### Explicitly out of scope for the first public alpha

- Kubernetes deployment as a requirement.
- Automatic cloud provisioning, billing activation, or paid-resource shutdown.
- Credential custody or account sharing.
- Unbounded autonomous intent generation after the originating chat stops.
- Reinforcement learning before stable outcome and action-probability logging.
- A graph database as the transactional execution source of truth.
- Automatic discovery of arbitrary network hosts, credentials, or private data.
- Public unauthenticated model endpoints.
- Scientific promotion of FEM/MPB/PWE, Maxwell, full-BZ, localizer, or Chern
  claims without the corresponding domain-specific validation program.

## 4. Target architecture

```text
Human mission and claim boundary
        |
        v
Mission Compiler -----> CPS Compiler -----> Task Graph + Stage Gates
        |                                       |
        v                                       v
Policy Registry -----------------------> Temporal Scheduler
                                                ^
Resource/Quota/Route Scout -> World State ------|
                                                |
                                                v
                                  Transactional Reservations
                                                |
                                                v
                         Local/SSH/Server Runtime Adapters
                                                |
                                                v
                    Validators + Artifact/Commit Manifests
                                                |
                                                v
                 Event/Provenance Ledger -> Diagnose/Learn
                           |                       |
                           v                       +-> Replan
                   Human Control Surface
```

### Architectural invariants

1. SQLite/Postgres event state is authoritative; graphs, dashboards, OTel, and
   OpenLineage are rebuildable projections.
2. `execution_host`, `workload_host`, storage placement, and network route are
   distinct fields.
3. Catalog, authentication, runtime acceptance, numeric quota, and historical
   usage are separate evidence types.
4. A model belongs to its actual shared quota pool; changing a model name does
   not bypass an exhausted pool.
5. Every irreversible or externally visible action has an idempotency key,
   bounded authority, validator, and provenance chain.
6. Unknown remains unknown. Pilot permission is explicit, bounded, and logged.
7. Agent messages communicate status; artifacts and validators establish truth.
8. Large files use a data plane, not prompts or the control-message channel.
9. Policy learning runs in shadow mode before canary promotion.
10. The user defines semantic milestones and claim boundaries; the system may
    automate mechanical decomposition only within those boundaries.

## 5. Research questions and preregistered hypotheses

Thresholds below are proposed preregistration targets. Freeze them after the
provider-free pilot estimates variance; do not move them after seeing canary
results.

| ID | Research question | Falsifiable hypothesis | Primary success gate |
| --- | --- | --- | --- |
| RQ0 | Can the system be measured reliably? | Event v2 reconstructs task, assignment, attempt, resource, quota, artifact, and side-effect chains. | 100% irreversible actions traceable; >=99% attempts have a terminal or legitimate censored state. |
| RQ1 | Can mission language be compiled safely? | MissionSpec v2 preserves all human-labeled hard constraints and identifies material ambiguity. | 100% hard-constraint recall on the frozen corpus; zero silent side-effect or claim-boundary expansion. |
| RQ2 | Can resource demand be estimated? | Calibrated P50/P90 intervals outperform fixed thresholds and root-volume checks. | P90 coverage 85-95%; catastrophic underestimation <1%; reservation waste 20% below static baseline. |
| RQ3 | Does joint placement help? | Joint model/host/mount/route/start-time placement beats sequential model-then-host selection. | >=15% lower median makespan; zero quota violations; no increase in catastrophic resource failure. |
| RQ4 | Does dynamic model routing save quota? | State-aware cascades preserve validated quality while reducing normalized quota burn. | Success-rate non-inferiority margin -3 percentage points; >=20% quota reduction. |
| RQ5 | Does dynamic concurrency help? | Concurrency derived from DAG, pools, resources, write scopes, and human capacity improves throughput safely. | >=20% throughput improvement; failure non-inferiority +2 points; zero unrecoverable write conflict. |
| RQ6 | Does CPS compilation help? | Task-specific capsules reduce context/tool overhead without lowering validated success. | >=25% context/token reduction; success non-inferiority -3 points; fewer unauthorized tool attempts. |
| RQ7 | Can the system run through interruption? | Fenced reservations, checkpoints, outbox, and reconciliation survive crash, disconnect, quota reset, and restart. | Zero accepted-task loss and duplicate irreversible effects; recovery within two lease periods plus one scan. |
| RQ8 | Can human attention be reduced safely? | Event-triggered stage gates and progressive disclosure reduce review time without missing critical errors. | >=30% fewer interruptions or review minutes; critical-error discovery non-inferiority -2 points. |
| RQ9 | Can online calibration improve future plans? | Outcome-based calibration lowers held-out routing regret without unsafe exploration. | Improvement on a frozen future-window set; zero policy escape; shadow before canary. |

If a confidence interval crosses a non-inferiority boundary, quota savings come
only from lower quality, or utilization gains materially increase failures, the
corresponding hypothesis fails.

## 6. Primary outcomes and measurement rules

### 6.1 Main outcome

Report the components separately, even if a scalar objective is used internally:

```text
Validated Utility
  = accepted task value
  - rework penalty
  - deadline penalty
  - quota and cost penalty
  - human-attention penalty
  - high-risk policy violation penalty
```

### 6.2 Quality and evidence

- validated success and first-pass success;
- compile/test/numerical-invariant results;
- artifact freshness and hash integrity;
- independent reviewer disagreement;
- claim-boundary or permission violations;
- regression and rework count.

### 6.3 Cost and quota

- exact pool/model/effort when attributable;
- five-hour/weekly/monthly before and after values with source and TTL;
- input/output tokens and USD when the provider exposes them;
- quota attribution: `exclusive`, `confounded`, or `unknown`;
- strong-model invocation and escalation rate;
- validated progress per quota-percent and per USD.

### 6.4 Time and scheduling

- queue wait, time to first output, time to validated artifact;
- critical-path makespan and deadline hit rate;
- pool starvation, queue age, and reset-window idle time;
- scheduler decision latency and replan count.

### 6.5 Resource estimates

- P50/P90 pinball loss and empirical P90 coverage;
- interval width and severity of underestimation;
- CPU time, peak RSS, GPU utilization, peak/free VRAM;
- input/download/environment/temp/cache/output bytes;
- inode, quota, mount, path, and filesystem selection accuracy;
- OOM, ENOSPC, disk-pressure, route, and runtime-fit failures;
- reservation waste and utilization.

### 6.6 Reliability and communication

- accepted task loss, orphan duration, stale-state duration;
- duplicate attempt and duplicate side-effect count;
- mean recovery time, retry count, and poison-message quarantine;
- control-message delivery, ack, replay, and deduplication;
- server-to-server transfer integrity and route evidence.

### 6.7 Human factors

- review minutes, interruption count, and decision latency;
- number of items displayed before a decision;
- correction/reversal count;
- one-minute comprehension check: current goal, blocker, evidence level, and
  next decision;
- optional NASA-TLX-style workload rating for N-of-1 comparisons.

## 7. Planned repository structure

The research work should introduce focused files rather than extend the large
standalone scripts indefinitely:

```text
docs/research/
  protocol-v1.md
  benchmark-taxonomy-v1.md
  baseline-registry-v1.md
  fault-injection-matrix-v1.md
  promotion-checklist-v1.md
  data-governance-v1.md
  decision-log.md

research/
  README.md
  corpus/missions.jsonl
  corpus/task-labels.jsonl
  scenarios/quota-windows.json
  scenarios/resource-topologies.json
  scenarios/failure-injections.json
  simulator/fake_provider.py
  simulator/fake_clock.py
  simulator/fake_cluster.py
  replay/import_legacy_runs.py
  replay/materialize_observations.py
  analysis/metrics.py
  analysis/paired_report.py

schemas/
  mission_spec.schema.json
  cps_bundle.schema.json
  world_state.schema.json
  provenance_event.schema.json
  reservation.schema.json
  human_decision.schema.json

src/local_agent_dispatch/
  domain/mission.py
  domain/world_state.py
  domain/events.py
  intent/compiler.py
  intent/claim_contract.py
  cps/compiler.py
  cps/registry.py
  resources/topology.py
  resources/probes.py
  resources/reservations.py
  scheduler/requirements.py
  scheduler/heft.py
  scheduler/fairness.py
  scheduler/temporal.py
  scheduler/router.py
  ledger/store.py
  ledger/projections.py
  coordination/envelope.py
  coordination/outbox.py
  diagnosis/classifier.py
  diagnosis/incidents.py
  human/stage_gates.py
  human/briefs.py

tests/
  test_mission_compiler.py
  test_cps_compiler.py
  test_world_state.py
  test_resource_topology.py
  test_reservations.py
  test_temporal_scheduler.py
  test_model_router.py
  test_provenance_ledger.py
  test_coordination_outbox.py
  test_stage_gates.py
  test_research_replay.py
```

Production modules must remain stdlib-first. Optional solvers, telemetry
exporters, and distributed buses stay behind plugin interfaces.

## 8. Benchmark corpus

### 8.1 Task strata

Build at least four strata before real-provider comparison:

| Stratum | Examples | Verification |
| --- | --- | --- |
| S0 deterministic | system scan, schema transform, log classification | exact output/schema |
| S1 bounded code | one-file bug, parser, unit-test addition | compile and tests |
| S2 integration | multi-file refactor, adapter contract, worktree merge | integration tests and artifact manifest |
| S3 research/high-risk | architecture review, literature synthesis, FEM/MPB/PWE plan | evidence rubric, claim contract, independent review |

Each task label must include:

```text
reasoning_complexity
domain_specialization
context_size
tool_complexity
verifiability
reversibility
blast_radius
claim_risk
dag_width / dag_depth / critical_path
cpu / ram / gpu / vram / storage / network class
data_classification / data_location
deadline / human_review_requirement
```

### 8.2 Environment scenarios

The simulator must cover:

- quota healthy, near five-hour limit, weekly exhausted, imminent reset, and
  unknown remaining balance;
- host healthy, RAM pressure, VRAM pressure, root filesystem small but project
  mount large, mount disappearance, inode exhaustion, and cgroup restriction;
- normal network, high latency, low throughput, short disconnect, server-server
  direct path, proxy/relay ambiguity, and failed route verification;
- catalog-visible/runtime-rejected model, provider auth failure, rate limit,
  zero-progress stall, and partial artifact;
- human online, fixed review window, and extended absence.

### 8.3 Longitudinal cases

Use three end-to-end cases:

1. **Provider-free software case:** compile a mission, create a DAG, execute fake
   workers, validate artifacts, inject failures, recover, and replan.
2. **Bounded real coding canary:** small reversible worktree with exact model,
   quota snapshots, tests, and no publication/deployment permission.
3. **FEM/MPB/PWE research case:** first compile only MissionSpec, adapter DAG,
   CPS, resource needs, and claim envelope; later admit real outputs only through
   a separate scientific validation gate. Do not claim continuous Maxwell,
   full-BZ, localizer, or Chern results from the planning trial.

## 9. Baselines and ablations

### 9.1 Routing baselines

- user manual selection;
- strongest model for every task;
- cheapest/local model for every task;
- round-robin or remaining-percent proportional allocation;
- current static heuristic;
- task-label-only router;
- state-aware non-learning router;
- state-aware calibrated router;
- hindsight oracle as an unattainable upper bound.

### 9.2 Scheduling baselines

- serial execution;
- fixed concurrency N;
- provider-limit-only concurrency;
- hardware-only concurrency;
- HEFT static placement;
- separate model then compute placement;
- joint temporal scheduler;
- hindsight oracle.

### 9.3 CPS baselines

- raw user prompt;
- full repository/full history context;
- generic agent system prompt;
- current Skill injection;
- human-authored CPS;
- compiled CPS;
- compiled CPS plus independent reviewer.

### 9.4 Human-control baselines

- approval at every step;
- fixed interval reporting;
- final-only approval;
- raw logs/current JSON reports;
- event-triggered gates with progressive disclosure.

### 9.5 Required ablations

Remove one mechanism at a time:

- quota reset calendar;
- outcome history;
- verifiability and claim risk;
- model diversity and strong-model reserve;
- rolling horizon;
- reservation fencing;
- communication cost;
- mount/path selection;
- write-scope lock;
- P90 safety headroom;
- CPS output schema, stop condition, claim envelope, or provenance;
- human-event trigger, confidence display, or scheduling explanation.

## 10. Work packages

Each work package is a separately testable subproject. Create a detailed child
implementation plan before production coding begins.

### WP0: Freeze the research protocol and baseline

**Files:**

- Create: `docs/research/protocol-v1.md`
- Create: `docs/research/benchmark-taxonomy-v1.md`
- Create: `docs/research/baseline-registry-v1.md`
- Create: `docs/research/data-governance-v1.md`
- Create: `research/README.md`
- Test: `tests/test_research_replay.py`

**Research output:** Frozen RQs, non-inferiority margins, evidence levels,
failure taxonomy, data policy, and baseline commands.

- [ ] Write the six research documents using Sections 1-9 of this plan as the
  normative source.
- [ ] Record the current provider-free test, compile, shell, and Skill-validation
  commands without asserting old result counts.
- [ ] Define a machine-readable run manifest with policy version, seed, start
  commit or source digest, fixture digest, and result digest.
- [ ] Write a failing replay test that rejects a result without manifest,
  validator identity, or evidence level.
- [ ] Add the minimal manifest validator and rerun the focused test.
- [ ] Run the full provider-free suite and store only the small aggregate report.

**Gate G0:** Clean provider-free baseline is reproducible twice from identical
fixtures; every result has a manifest and evidence level.

**Stop condition:** If baseline state cannot be reproduced or existing tests
depend on credentials/network, repair isolation before any comparative study.

**Logical commit:** `docs(research): freeze dispatch research protocol`

### WP1: Event and Provenance v2

**Files:**

- Create: `schemas/provenance_event.schema.json`
- Create: `src/local_agent_dispatch/domain/events.py`
- Create: `src/local_agent_dispatch/ledger/store.py`
- Create: `src/local_agent_dispatch/ledger/projections.py`
- Create: `research/replay/import_legacy_runs.py`
- Create: `research/replay/materialize_observations.py`
- Test: `tests/test_provenance_ledger.py`

**Research question:** Can the system reconstruct every decision, attempt,
artifact, resource observation, quota observation, and authorized side effect?

- [ ] Define stable IDs for mission, task, plan revision, assignment, attempt,
  event, observation, artifact, human decision, and policy version.
- [ ] Require causal parent, timestamp, source, confidence, privacy class, and
  schema version on every event.
- [ ] Define attempt events for queued, reserved, claimed, started, heartbeat,
  artifact observed, validation, completion, failure, abandonment, and review.
- [ ] Add fields for exact provider/pool/model/effort, CPS digest, execution host,
  workload host, mount, route, validator, Git/worktree, and idempotency key.
- [ ] Store prompt/context bodies by controlled reference and digest, not by
  default in the public ledger.
- [ ] Import legacy JSON as `evidence_quality=legacy_incomplete`; never fabricate
  missing model, quota, resource, or terminal state.
- [ ] Add a liveness reconciler that converts dead stale attempts to an explicit
  abandoned/review state while preserving original evidence.
- [ ] Materialize estimator observations only from records that meet attribution
  and validation requirements.

**Experiment:** Replay the existing metadata-only historical corpus and measure
event closure, orphan rate, duplicate events, missing models, and attribution
quality before and after import/reconciliation.

**Gate G1:** All new fake E2E attempts close causally; legacy gaps remain visible;
no prompt/secret enters public projections.

**Stop condition:** If state mutation and event append can diverge, do not add
learning or external telemetry; first unify the transaction.

**Logical commit:** `feat(ledger): add causal provenance v2`

### WP2: MissionSpec v2 and Claim Contract

**Files:**

- Create: `schemas/mission_spec.schema.json`
- Create: `src/local_agent_dispatch/domain/mission.py`
- Create: `src/local_agent_dispatch/intent/compiler.py`
- Create: `src/local_agent_dispatch/intent/claim_contract.py`
- Test: `tests/test_mission_compiler.py`
- Corpus: `research/corpus/missions.jsonl`
- Labels: `research/corpus/task-labels.jsonl`

**Research question:** Can natural-language mission orders be compiled without
losing objective, dependencies, model policy, placement, side-effect authority,
or scientific claim limits?

- [ ] Define `goal`, `non_goals`, `deliverables`, `acceptance_tests`,
  `claim_envelope`, data class, DAG hints, hard/soft policy, deadline, quota
  reserve, Git policy, CPS profiles, checkpoints, artifacts, validators, and
  stop conditions.
- [ ] Represent every extracted field as value, source span, confidence, and
  `hard|soft|unknown` status.
- [ ] Compile the user's FEM/MPB/PWE example into a golden MissionSpec fixture.
- [ ] Label at least 40 missions across S0-S3 with two independent passes and
  resolve disagreements in a recorded adjudication file.
- [ ] Write tests for missing claim envelope, ambiguous commit permission,
  unsupported model effort, policy-excluded model override, and local-agent plus
  remote-workload split placement.
- [ ] Make compilation provider-free and non-executing; output only a reviewable
  spec and material ambiguity list.

**Experiment:** Compare current keyword capture, raw free text, and MissionSpec
compiler on hard-constraint recall, false hard constraints, clarification count,
and silent authority expansion.

**Gate G2:** 100% recall for hard constraints on the frozen corpus and zero
silent expansion of Git, network, model, data, or scientific authority.

**Stop condition:** If a field materially affects cost, permissions, data
location, or claims and remains ambiguous, the compiler must request review
rather than guess.

**Logical commit:** `feat(intent): add mission and claim contracts`

### WP3: CPS Compiler and Agent Team Topology

**Files:**

- Create: `schemas/cps_bundle.schema.json`
- Create: `src/local_agent_dispatch/cps/compiler.py`
- Create: `src/local_agent_dispatch/cps/registry.py`
- Create: `src/local_agent_dispatch/human/stage_gates.py`
- Test: `tests/test_cps_compiler.py`

**Research question:** Which context, prompt contract, Skill set, role topology,
and review edges maximize validated outcomes under context and quota limits?

- [ ] Define immutable Context Pack, Prompt Contract, Skill Set, permission,
  retrieval policy, output schema, stop condition, and bundle digest.
- [ ] Define planner, scout, implementer, validator, critic, integrator, and
  monitor roles without assuming a specific provider.
- [ ] Assign one writer per write scope and explicit integration nodes.
- [ ] Add team templates for deterministic, bounded code, multi-file integration,
  research synthesis, and high-risk scientific review.
- [ ] Add critical-node rules based on verifiability, reversibility, claim risk,
  novelty, DAG centrality, and blast radius rather than difficulty alone.
- [ ] Record model-family diversity and reviewer independence.

**Experiment:** Run the CPS baselines and a 2x2x2 fractional-factorial study:
full/minimal context, free/typed prompt, global/allowlisted Skills. Measure
validated success, tokens, tool errors, file hallucination, and review effort.

**Gate G3:** Context reduction target is met without crossing the quality
non-inferiority margin; every bundle is reproducible from versioned references.

**Stop condition:** If automatic CPS underperforms human-authored CPS beyond the
margin, retain recommendations in shadow mode and improve the corpus.

**Logical commit:** `feat(cps): compile versioned execution capsules`

### WP4: Resource Digital Twin and Route Evidence

**Files:**

- Create: `schemas/world_state.schema.json`
- Create: `src/local_agent_dispatch/domain/world_state.py`
- Create: `src/local_agent_dispatch/resources/topology.py`
- Create: `src/local_agent_dispatch/resources/probes.py`
- Test: `tests/test_world_state.py`
- Test: `tests/test_resource_topology.py`
- Scenario: `research/scenarios/resource-topologies.json`

**Research question:** Can the system discover safe allocatable capacity and
choose the correct host, mount, runtime, and route without root-volume or stale
snapshot errors?

- [ ] Model Host, CPU topology, NUMA, RAM, GPU, VRAM, Mount, quota, inode,
  Runtime, Cache, DatasetLocation, NetworkRoute, and Observation.
- [ ] Distinguish capacity, allocatable, available-now, reserved, and
  safe-to-place values.
- [ ] Discover mounts with bounded `findmnt`, `/proc/self/mountinfo`, `statvfs`,
  or platform equivalents; keep fixed common paths only as compatibility hints.
- [ ] Probe the exact intended project/cache/temp/output paths for writability,
  free bytes, inodes, quota evidence, and filesystem type.
- [ ] Add cgroup/container limits, process attribution, load, GPU process/VRAM,
  CUDA/runtime compatibility, and observation TTL where available.
- [ ] Represent execution, control, artifact, and bulk-data routes separately.
- [ ] Record `direct|bastion|proxy|relay|unknown`, effective SSH configuration,
  peer evidence, RTT/throughput, and verified time.
- [ ] Never turn resource discovery into permission to install, download, or
  enroll an unknown host.

**Experiment:** Compare root-only, fixed-directory, and mount-graph probes under
small-root/large-project-mount, read-only shared mount, user quota, inode
exhaustion, mount disappearance, and VRAM pressure fixtures.

**Gate G4:** Correct storage-domain selection on every frozen topology; no false
safe placement when quota, writability, route, or P90 headroom is unknown.

**Stop condition:** If a task's full model/data footprint or target path cannot
be bounded, emit pilot/review rather than a full placement.

**Logical commit:** `feat(resources): add host mount and route digital twin`

### WP5: Transactional Reservations and Temporal Scheduler

**Files:**

- Create: `schemas/reservation.schema.json`
- Create: `src/local_agent_dispatch/resources/reservations.py`
- Create: `src/local_agent_dispatch/scheduler/requirements.py`
- Create: `src/local_agent_dispatch/scheduler/heft.py`
- Create: `src/local_agent_dispatch/scheduler/fairness.py`
- Create: `src/local_agent_dispatch/scheduler/temporal.py`
- Modify: `scripts/sqlite_store.py`
- Modify: `scripts/sqlite_controller.py`
- Test: `tests/test_reservations.py`
- Test: `tests/test_temporal_scheduler.py`

**Research question:** Can one transaction enforce dependency, quota pool, host,
mount, write-scope, deadline, and human-review constraints for each claim?

- [ ] Express job and resource capabilities as ClassAd-like `requirements` and
  `rank` values with hard/soft separation.
- [ ] Implement HEFT-style upward rank using estimated compute and communication
  time for initial critical-path ordering.
- [ ] Implement weighted dominant-share fairness across CPU, RAM, GPU, VRAM,
  storage, network, quota windows, and human review capacity.
- [ ] Model release time, deadline, reset time, reserve, queue aging, backfill,
  and strong-model budget reservation.
- [ ] Compute dynamic concurrency from DAG frontier, pool slots, resource slots,
  route capacity, write scopes, quota burn, and pending review capacity.
- [ ] Store reservation, claim, heartbeat, release, and fence in one SQLite
  transaction; a controller must not claim an unreserved task.
- [ ] Keep agent-session concurrency separate from workload concurrency.
- [ ] Add a complexity guard that falls back to a deterministic heuristic when
  the exact horizon exceeds its bound.

**Experiment:** Replay identical DAGs under serial, fixed-N, HEFT-only,
sequential placement, and joint temporal policies. Measure makespan, validated
throughput, deadline misses, fairness, starvation, utilization, and violations.

**Gate G5:** Zero dependency, write-scope, quota, or resource reservation
violations across fault-injected replay; joint policy reaches the preregistered
makespan/throughput target without safety regression.

**Stop condition:** If policy quality is not better than the current heuristic
or solve latency exceeds two seconds for the target 6-10 job horizon, retain the
simpler planner and report the null result.

**Logical commit:** `feat(scheduler): add temporal fenced reservations`

### WP6: Model Router and Outcome Registry

**Files:**

- Create: `src/local_agent_dispatch/scheduler/router.py`
- Create: `research/simulator/fake_provider.py`
- Create: `research/analysis/metrics.py`
- Test: `tests/test_model_router.py`

**Research question:** When should cheap/local workers, strong providers,
independent reviewers, or escalation cascades be selected?

- [ ] Define outcome rows keyed by task family, risk, model, effort, CPS digest,
  host/runtime, validator, and policy version.
- [ ] Record success, rework, disagreement, latency, quota, token/USD, and
  attribution quality.
- [ ] Implement manual, strongest, cheapest, round-robin, current heuristic,
  state-aware, and hindsight-oracle baselines.
- [ ] Begin with calibrated priors and confidence intervals; do not train RL.
- [ ] Implement cheap-worker -> deterministic-validator -> strong-reviewer
  escalation with explicit stopping and pool reserve.
- [ ] Require action probabilities before any bandit or off-policy evaluation.
- [ ] Keep new routers in shadow mode until frozen-set calibration and policy
  checks pass.

**Experiment:** Paired task runs or simulator replay across quota windows and
task strata. Report cost-quality Pareto fronts, router regret, Brier score, ECE,
escalation success, and normalized quota burn.

**Gate G6:** Quality non-inferiority and quota-reduction targets both hold; no
scientific or permission gate is delegated to model self-confidence alone.

**Stop condition:** If savings derive from skipped validation or lower accepted
quality, reject the router policy.

**Logical commit:** `feat(router): add outcome-calibrated model cascades`

### WP7: Durable Coordination and Remote Continuity

**Files:**

- Create: `src/local_agent_dispatch/coordination/envelope.py`
- Create: `src/local_agent_dispatch/coordination/outbox.py`
- Modify: `scripts/remote_worker.py`
- Modify: `scripts/remote_worker_client.py`
- Test: `tests/test_coordination_outbox.py`
- Scenario: `research/scenarios/failure-injections.json`

**Research question:** Can pre-authorized work continue across chat loss,
controller crash, worker crash, SSH disconnect, quota reset, and replay without
duplicate side effects?

- [ ] Define a versioned command/event envelope with message ID, idempotency key,
  causal parent, task/attempt ID, TTL, ack state, payload digest, and privacy
  class.
- [ ] Implement a SQLite outbox/inbox and explicit ack/retry/dead-letter logic.
- [ ] Bind remote claims, heartbeats, artifact mutation, validator, and handoff
  to the current fence token.
- [ ] Add worktree/source manifest and artifact transfer contracts with counts,
  bytes, SHA-256, resume, data classification, and route evidence.
- [ ] Allow only previously authorized packets during chat absence; new intent
  remains blocked.
- [ ] Introduce an A2A-compatible external projection only after internal task
  semantics stabilize.
- [ ] Evaluate NATS JetStream only after two or more persistent remote workers
  demonstrate that SQLite point-to-point control is the bottleneck.

**Experiment:** Inject crash, lost ack, duplicate delivery, stale fence, partial
artifact, SSH disconnect, mount loss, and quota reset. Verify exactly-once
effective side effects through idempotency and validators, not delivery claims.

**Gate G7:** No accepted task loss or duplicate irreversible effect; every
disconnect produces a safe resume/review handoff.

**Stop condition:** Any stale owner able to mutate state or artifact blocks
remote autonomous promotion.

**Logical commit:** `feat(coordination): add fenced durable outbox`

### WP8: Incident Analysis and Human Control Surface

**Files:**

- Create: `schemas/human_decision.schema.json`
- Create: `src/local_agent_dispatch/diagnosis/classifier.py`
- Create: `src/local_agent_dispatch/diagnosis/incidents.py`
- Create: `src/local_agent_dispatch/human/briefs.py`
- Test: `tests/test_stage_gates.py`

**Research question:** Can the system interrupt the user only at high-leverage
points while making failures and decisions understandable?

- [ ] Separate provider, quota, capability, compute, runtime, storage, network,
  data, validation, policy, and human-origin failures.
- [ ] Represent incident hypotheses, evidence for/against, safe diagnostic,
  result, confidence, recovery, and prevention.
- [ ] Add stage-gate triggers for objective change, irreversible side effect,
  high quota/cost, evidence promotion, reviewer disagreement, unvalidated
  result, release/deploy, and low-confidence recovery.
- [ ] Generate L0 one-screen brief, L1 milestone board, L2 decision trace, and
  L3 raw-event references.
- [ ] Make every gate support approve, edit, reject, cancel, and bounded defer.
- [ ] Treat human review windows and pending-review capacity as scheduler input.

**Experiment:** N-of-1 crossover on matched task families comparing raw logs,
fixed reporting, approval-every-step, final-only, and event-triggered briefs.
Measure time, interruptions, comprehension, corrections, and critical errors.

**Gate G8:** Human-time target is met without crossing the critical-error
non-inferiority boundary; the L0 view never hides evidence level or blocker.

**Stop condition:** If the summary omits a material risk, ambiguity, or claim
boundary, fall back to a more detailed gate and revise the brief policy.

**Logical commit:** `feat(human): add stage gates and progressive briefs`

### WP9: Replay Simulator, Fault Injection, and Statistical Analysis

**Files:**

- Create: `research/simulator/fake_clock.py`
- Create: `research/simulator/fake_cluster.py`
- Create: `research/scenarios/quota-windows.json`
- Create: `research/analysis/paired_report.py`
- Create: `docs/research/fault-injection-matrix-v1.md`
- Create: `docs/research/promotion-checklist-v1.md`
- Test: `tests/test_research_replay.py`

- [ ] Add a deterministic fake clock so five-hour/weekly reset and 24-hour plans
  can be compressed without real waiting.
- [ ] Simulate provider quality, latency, quota cost, runtime rejection, pool
  sharing, and attribution noise.
- [ ] Simulate host/mount/resource state, reservations, network routes, worker
  liveness, and human availability.
- [ ] Store random seed, policy digest, corpus digest, and complete event trace.
- [ ] Implement paired summaries, median/IQR, cluster bootstrap 95% intervals,
  binary success intervals, calibration metrics, and survival-style
  time-to-valid-artifact handling for censored runs.
- [ ] Add Holm-corrected secondary comparisons; keep one preregistered primary
  outcome per research question.
- [ ] Add action-probability recording before IPS or doubly robust analysis.

**Gate G9:** Repeated replay with the same manifest is deterministic; fault
matrix covers every promotion-blocking failure class.

**Stop condition:** If simulator assumptions dominate outcomes, narrow claims
and promote only mechanisms that also pass shadow/canary evidence.

**Logical commit:** `test(research): add deterministic replay laboratory`

### WP10: Shadow, Canary, Soak, and Scientific Case Study

**Files:**

- Create: `docs/research/decision-log.md`
- Create: sanitized run manifests under `research/results/summary/`
- Modify: `docs/evidence-model.md`
- Modify: `docs/roadmap.md`

- [ ] Run the candidate scheduler in shadow mode beside current/manual decisions;
  do not execute its alternative actions.
- [ ] Check every shadow plan for resource, quota, permission, privacy, route,
  and claim-boundary violations.
- [ ] Admit only reversible, automatically validated, low-quota tasks to canary.
- [ ] Randomize within task family and quota window; keep start commit, inputs,
  validator, retry limit, authority, and time budget constant.
- [ ] Progress from accelerated replay to 2-hour, 8-hour, three 24-hour, then
  optional 72-hour soak only when the prior gate passes.
- [ ] Inject controller/worker crash, stale PID, SSH disconnect, rate limit,
  quota exhaustion/reset, mount disappearance, partial artifact, truncated
  event, and absent human review.
- [ ] Compile the FEM/MPB/PWE case first; execute only bounded adapters after
  separate authorization and preserve its scientific claim ceiling.

**Gate G10:** All lower gates pass, three 24-hour trials have no lost accepted
task or duplicate irreversible effect, and the evidence report distinguishes
engineering reliability from scientific validity.

**Stop condition:** Privacy leak, unauthorized side effect, duplicated
irreversible action, unexplained claim expansion, or a primary metric in the
harm region immediately returns the feature to shadow mode.

**Logical commit:** `docs(results): record bounded dispatch canary evidence`

### WP11: Public alpha engineering

**Files:**

- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `SECURITY.md`
- Modify: `CONTRIBUTING.md`
- Modify: `docs/release-checklist.md`
- Create: sanitized examples under `examples/`
- Create: platform fixtures under `tests/fixtures/platforms/`

- [ ] Obtain explicit user choice of open-source license before adding LICENSE.
- [ ] Initialize a Git repository only after approval and exclude runtime state,
  credentials, host endpoints, logs, prompts, caches, and build artifacts.
- [ ] Remove the external Antigravity Skill dependency or make it an optional
  audited plugin.
- [ ] Run secret, absolute-path, private-endpoint, generated-file, and artifact
  scrub checks.
- [ ] Test clean wheel install and `lad doctor/demo` without credentials/network.
- [ ] Add Linux CI, hosted macOS tests, and Windows scanner/planner tests; label
  unsupported remote execution paths honestly.
- [ ] Publish support, threat, migration, schema, evidence, and limitation docs.
- [ ] Require an explicit opt-in contract smoke for real providers; keep public
  CI provider-free.

**Gate G11:** Clean clone builds and runs offline demo on the supported matrix;
the repository contains no private runtime material; all limitations are public.

**Stop condition:** No public release without license, scrub, clean-clone test,
and a reviewed support matrix.

**Logical commit:** `chore(release): prepare local-agent-dispatch public alpha`

## 11. Execution order and parallelism

```text
WP0 protocol
  |
  +--> WP1 ledger ------------------------+
  |                                       |
  +--> WP2 MissionSpec --> WP3 CPS -------+--> WP5 scheduler --> WP6 router
  |                                       |          |
  +--> WP4 resource twin -----------------+          +--> WP7 coordination
  |                                                  |
  +--> WP9 replay laboratory ------------------------+
                                                     |
                                       WP8 human/incident
                                                     |
                                          WP10 shadow/canary/soak
                                                     |
                                             WP11 public alpha
```

Safe parallel work after WP0:

- WP1 ledger, WP2 MissionSpec, WP4 resource twin, and WP9 simulator may proceed
  in separate write scopes.
- WP3 requires the MissionSpec vocabulary.
- WP5 requires stable ledger, mission, resource, and simulator contracts.
- WP6 requires validated outcomes from WP1 and scheduler decisions from WP5.
- WP7 requires reservation/fence semantics from WP5.
- WP8 can prototype briefs early but cannot become authoritative before the
  ledger and stage-gate events are stable.
- WP10 and WP11 are promotion stages, not parallel feature-development lanes.

## 12. Reference cadence

Use gates, not dates, as the binding schedule. A reasonable 24-week research
cadence is:

| Weeks | Focus | Exit gate |
| --- | --- | --- |
| 1-2 | WP0 protocol and reproducible baseline | G0 |
| 3-5 | WP1 Event/Provenance v2 | G1 |
| 3-6 | WP2 MissionSpec and corpus | G2 |
| 4-8 | WP4 Resource Digital Twin | G4 |
| 6-9 | WP3 CPS and team topology | G3 |
| 7-10 | WP9 replay/fault laboratory | G9 |
| 10-14 | WP5 temporal reservations | G5 |
| 13-16 | WP6 router/outcome registry | G6 |
| 14-18 | WP7 continuity/coordination | G7 |
| 15-19 | WP8 human/incident surface | G8 |
| 19-22 | WP10 shadow/canary/soak | G10 |
| 22-24 | WP11 public alpha | G11 |

If a gate fails, keep the simpler current mechanism and publish the negative or
uncertain result. Do not compress the schedule by skipping replay or shadow.

## 13. Statistical protocol

1. Use paired comparisons from the same starting source digest/worktree where
   possible.
2. Stratify by task family, difficulty dimensions, quota window, host state,
   and human availability.
3. Use mixed-effects logistic models for binary validation success when sample
   size supports them.
4. Report median, IQR, cluster-bootstrap 95% intervals for heavy-tailed time and
   cost metrics.
5. Use survival analysis for time-to-valid-artifact with quota/human waits and
   interrupted jobs treated as censored rather than discarded.
6. Report effect sizes and intervals, not p-values alone.
7. Use Holm correction for predeclared secondary comparisons.
8. Set sample size after provider-free pilot variance and minimum effect size;
   do not choose an arbitrary run count.
9. Old observational history supports taxonomy and variance estimates only.
10. Long-soak success is bounded engineering evidence, not a population-level
    statistical claim.

## 14. Safety, privacy, and research stop rules

### Automatic execution stops

- unknown or unverified route for sensitive or bulk data;
- unauthorized Provider/host/data location;
- predicted quota, cost, deadline, RAM, VRAM, disk, or inode hard-limit breach;
- write-scope conflict or stale reservation owner;
- duplicate irreversible action;
- missing validator or artifact manifest;
- unclassified repeated failure;
- attempted scientific claim expansion;
- submit, merge, publish, delete, deploy, credential, or permission operation
  lacking explicit authority;
- telemetry so incomplete that safe execution cannot be determined.

### Research stops

- event completeness below G0/G1;
- primary interval clearly in the harm region;
- quota saving explained by lower accepted quality;
- privacy leak or unauthorized side effect;
- Provider/model/policy drift making groups incomparable;
- preregistered maximum sample reached without separation: report uncertainty;
- simulator result not replicated in shadow/canary where promotion requires it.

### Data governance

- classify data as `public`, `internal`, `sensitive`, or `forbidden_remote`;
- keep prompts, private code, SSH details, tokens, and user-behavior traces local;
- store hashes, references, and redacted summaries by default;
- publish only synthetic or manually scrubbed traces;
- set retention and deletion rules for behavioral and usage data;
- verify dataset/model/code licenses before remote copy or public release;
- never use the scheduler to bypass quotas, account rules, or provider terms;
- do not let an LLM judge alone promote scientific claims or irreversible work.

## 15. Risk register

| Risk | Early signal | Mitigation | Owner plane |
| --- | --- | --- | --- |
| Schema proliferation | new ad-hoc JSON shapes | schema v2, migrations, contract tests | Ledger |
| Planner/controller drift | claimed job lacks valid reservation | transactional claim gate | Scheduler |
| Quota misattribution | concurrent unknown consumers | attribution class; exclude from model labels | Ledger |
| Model/catalog drift | catalog visible but runtime rejected | exact tuple runtime evidence and TTL | Provider |
| Resource false confidence | root/path or stale probe mismatch | mount graph, exact-path probe, P90 headroom | World State |
| Local disk exhaustion | repeated clones/packs/logs | content-addressed artifacts, retention, GC | Execution |
| Agent over-parallelization | write conflict/review backlog | dynamic concurrency and single writer | Scheduler |
| Correlated model errors | same-family reviewers agree falsely | independent family/provider review | CPS |
| Network relay violation | bulk bytes traverse Mac/proxy | route evidence and server-side transfer gate | Communication |
| Duplicate side effects | redelivery/stale owner | idempotency key, fence, validator, receipt | Coordination |
| User information overload | long logs and frequent prompts | progressive disclosure and attention budget | Human |
| Unsafe online learning | exploration changes policy silently | shadow, canary, versioned rollback | Learning |
| Premature infrastructure | Temporal/NATS/K8s complexity | adoption trigger and interface-first design | Architecture |
| Scientific overclaim | planning/smoke treated as physics result | ClaimContract and evidence promotion gate | Human/Safety |

## 16. External mechanism study and adoption triggers

Use these as mechanism sources and comparison baselines, not automatic
dependencies:

- Temporal history/replay and LangGraph checkpoints/interrupts for durable
  workflow semantics.
- HTCondor ClassAds for `requirements/rank`; Ray placement groups for atomic
  resource bundles; Kueue/DRF for quota sharing; HEFT for initial DAG placement.
- RouteLLM, FrugalGPT, and AutoMix for strong/weak cascade research.
- A2A for external agent task/artifact interoperability; MCP for tools and
  resources; NATS JetStream only for a demonstrated multi-worker bus need.
- OpenTelemetry for signals, OpenLineage for run/data facets, PROV-O for long
  term ontology projection.
- MAPE-K for the closed-loop architecture and Human-AI guidelines for when to
  explain, interrupt, defer, and preserve user control.

Adoption triggers:

| External component | Adopt only when |
| --- | --- |
| Temporal | SQLite controller cannot meet multi-process history/replay needs and workflow volume justifies an external service. |
| LangGraph | task/HITL graph semantics materially reduce custom code without taking ownership of resource scheduling. |
| Ray | a selected host needs gang scheduling or multi-process compute execution beyond the current worker. |
| Kueue/Kubernetes | the project targets a real Kubernetes cluster rather than a few SSH hosts. |
| NATS JetStream | at least two persistent remote workers need durable pull, backpressure, and replay beyond point-to-point SSH. |
| Graph database | provenance queries cannot be served by rebuildable projections and never before transactional truth is stable. |
| Learned router | sufficient validated, attributable outcomes and action probabilities exist for a frozen evaluation. |

Primary references:

- Temporal: <https://docs.temporal.io/>
- LangGraph persistence: <https://docs.langchain.com/oss/python/langgraph/persistence>
- Ray placement groups: <https://docs.ray.io/en/latest/ray-core/scheduling/placement-group.html>
- HTCondor ClassAds: <https://htcondor.readthedocs.io/en/main/classads/classad-mechanism.html>
- DRF: <https://www.usenix.org/conference/nsdi11/dominant-resource-fairness-fair-allocation-multiple-resource-types>
- HEFT: <https://doi.org/10.1109/71.993206>
- RouteLLM: <https://proceedings.iclr.cc/paper_files/paper/2025/hash/5503a7c69d48a2f86fc00b3dc09de686-Abstract-Conference.html>
- AutoMix: <https://proceedings.neurips.cc/paper_files/paper/2024/hash/ecda225cb187b40ea8edc1f46b03ffda-Abstract-Conference.html>
- A2A: <https://github.com/a2aproject/A2A/blob/main/docs/specification.md>
- NATS JetStream: <https://docs.nats.io/nats-concepts/jetstream/consumers>
- OpenTelemetry: <https://opentelemetry.io/docs/concepts/semantic-conventions/>
- PROV-O: <https://www.w3.org/TR/prov-o/>
- Human-AI guidelines: <https://www.microsoft.com/en-us/research/articles/guidelines-for-human-ai-interaction-eighteen-best-practices-for-human-centered-ai-design/>

## 17. First research sprint

The first sprint should produce six design artifacts and no real provider spend:

1. `docs/research/protocol-v1.md`
2. `docs/research/benchmark-taxonomy-v1.md`
3. `schemas/mission_spec.schema.json`
4. `schemas/world_state.schema.json`
5. `schemas/provenance_event.schema.json`
6. `docs/research/fault-injection-matrix-v1.md`

Use the FEM/MPB/PWE mission as the first golden fixture. The sprint is complete
when the example has a reviewable MissionSpec, claim envelope, DAG, CPS outline,
resource/route unknowns, human gates, and expected events, while all provider,
SSH, download, Git-commit, and scientific-execution flags remain false.

## 18. Plan self-review checklist

- [x] Every user requirement maps to WP1-WP11.
- [x] Every research question has a baseline, metric, promotion gate, and stop
  condition.
- [x] Provider-free replay precedes shadow, canary, and soak.
- [x] Model quality, quota, resource, and human-attention claims remain separate.
- [x] `execution_host`, `workload_host`, mount, runtime, and route remain distinct.
- [x] No unknown quota/resource is converted to a fabricated numeric value.
- [x] Historical observational data is not treated as causal model-ranking data.
- [x] Graph/OTel/OpenLineage are projections, not execution truth.
- [x] Real scientific work has a separate evidence-promotion gate.
- [x] External infrastructure has explicit adoption triggers.
- [x] Open-source release remains blocked on license, scrub, and clean-clone CI.
- [x] The plan contains no credentials, private endpoints, prompts, or runtime
  artifacts.

## 19. Execution handoff

Execute WP0 first. After G0 passes, create separate child implementation plans
for WP1, WP2, WP4, and WP9 and run them in disjoint write scopes. Do not enqueue
real providers or remote workloads from this master plan.
