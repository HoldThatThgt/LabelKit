## 3.17 M17 统一执行运行时 execution-runtime

### 3.17.1 职责与边界

**做：**为普通标记与 sequence 生成提供同一个进程内异步执行面：按 ResourceKey 独立有界接纳、
`asyncio.TaskGroup` 结构化任务寿命、全 execution domain 取消、输入序结果、profile 与 HTTP origin 资源许可、
ContextVar 隔离及 runtime 运行观测。

**不做：**不实现业务阶段、retry、dedup、CrossView、retained-content、工件提交或通用任务图；不提供公开
`submit`、`wait`、`cancel`、TaskHandle 或依赖边；不引入线程池、broker、数据库、守护进程、远程 worker、
checkpoint、新生产依赖或 `[runtime]` 配置。

唯一生产路径为：

```text
labelkit/orchestration/application.py
→ labelkit/runtime/{scheduler.py,resources.py}
→ labelkit/common/contracts/execution.py
```

`common` 不导入 runtime、operators 或 orchestration；operators 只依赖
`common/contracts/execution.py`，不导入 runtime；runtime 只导入 common；orchestration 是唯一同时看见 runtime 与
operators 的层。`runtime` 只表示本模块；推理能力只位于 `common/inference/`。

### 3.17.2 冻结跨层契约

```python
T = TypeVar("T")
ResourceKey = tuple[Literal["llm", "embedding"], str]
HttpOrigin = tuple[str, str, int]


@dataclass(frozen=True)
class TaskSpec(Generic[T]):
    task_id: str
    declaration_key: tuple[int, ...]
    stage: str
    resource_key: ResourceKey
    operation: Callable[[], Awaitable[T]] = field(repr=False, compare=False)


@dataclass(frozen=True)
class TaskGroupRequest(Generic[T]):
    tasks: tuple[TaskSpec[T], ...]


class TaskExecutor(Protocol):
    async def run_group(self, request: TaskGroupRequest[T]) -> tuple[T, ...]: ...


class ResourceLimiter(Protocol):
    def resource_limit(self, resource_key: ResourceKey) -> AsyncContextManager[None]: ...
    def origin_for(self, resource_key: ResourceKey) -> HttpOrigin: ...
    def origin_limit(self, origin: HttpOrigin) -> AsyncContextManager[None]: ...

    @property
    def http_connection_capacity(self) -> int: ...
```

实现使用 Python 3.11 的 `TypeVar` / `Generic` 写法。`operation` 不进入 repr、日志、trace、report、哈希或序列化。
空请求直接返回空 tuple，不创建 task 或 HTTP client。结果严格按输入序返回；业务可恢复失败必须是普通有类型
outcome，逃逸异常只表示 fatal、control 或 internal。

`task_id` 在 execution domain 内全局唯一，只含 run、batch/slot、attempt、stage 与 ordinal。重复 task ID、未知
ResourceKey 或不可比较 declaration key 在创建任何 leaf 前 fail closed。`declaration_key` 只用于多个同时 fatal 的
稳定选择，不改变结果归并序。普通 task namespace 从 run/batch/stage 派生；sequence 从
run/phase/slot/attempt/stage 派生。

`RunContext` 显式增加 `tasks: TaskExecutor` 与 `task_namespace: str`；`RunServices` 和 `GenerationServices` 显式增加
同一 `tasks` 对象。Application 每次 live run 只构造一个 ExecutionRuntime；所有 RunContext、派生 attempt context 与
GenerationServices 保持该对象身份，不提供默认空 executor、隐式 ContextVar 身份或替代构造入口。

sequence 的跨模块冻结载体只使用以下字段及顺序；具体类型定义与完整字段注释由 `docs/CONTRACTS.md` 冻结：

```python
PrimaryCandidateReconcileRequest(
    program, plan, run_id, slot, projection_witnesses, sequences,
    replay_layouts, replays, retained_content_bytes,
)
NoiseCandidateReconcileRequest(
    program, run_id, noise_slot, payload_digest, row, retained_content_bytes,
)
DedupReservation(capability_id, epoch, record_digests, exact_cluster_keys)
PreparedCandidate(
    slot, attempt_index, projection_witnesses, sequences, replays, reservation,
    dataset_counters, retained_content_bytes, digest,
)
PreparedNoiseCandidate(
    noise_slot, attempt_index, payload_digest, row, similarity_signature,
    dataset_counters, retained_content_bytes, digest,
)
ResourceInterval(resource, start_us, end_us, event_id, source_key)
CrossViewDelta(phase, ordinal, event_ids, timestamps_us, source_keys, resource_intervals)
```

### 3.17.3 ExecutionRuntime

具体实现面为：

```python
class ExecutionRuntime(TaskExecutor):
    def __init__(self, resources: ResourceManager, metrics: MetricsSink): ...

    async def run(self, workflow: Callable[[], Awaitable[T]]) -> T: ...

    async def run_group(self, request: TaskGroupRequest[T]) -> tuple[T, ...]: ...
```

`run()` 是 Application 唯一 execution-domain 入口。域外 `run_group()`、叶任务内 `run_group()` 及嵌套 `run()` 均
fail closed。`run()` 返回前所有 coordinator、leaf、cleanup 与 admission 计数必须结束；静态 validate、dry-run 和
estimate 不调用它。

每个 ResourceKey 只有一个 runtime 生命周期级 admission counter，所有并发 `run_group()` 共享。请求在首次阻塞前
按资源分组，各资源生产路径独立；一个低容量 profile 的队头等待不得阻止其他 profile 接纳。只在取得 admission
后创建 leaf task，admission 在 leaf `finally` 释放。TaskSpec tuple 属于 operator 的有界业务计划，不计作已创建
leaf；普通计划量由单活动批约束，sequence 由候选缓冲约束。不同 `run_group()` 在同一资源上的 FIFO 或无饥饿不在
承诺内。

TaskSpec 的 ResourceKey 表示首轮模型调用的接纳通道。Schema repair 可以在 leaf 内切换 profile；每个实际主调用与
repair 调用仍分别取得 ResourceManager 的逻辑许可。repair 等待不占 repair admission，但绝不突破 repair profile
容量；leaf 数仍受首轮 admission 限制。

scheduler 在 `run_group()` 入口捕获一次 `contextvars.copy_context()`，每个 leaf 使用独立的
`captured_context.copy()`。同一 attempt 的 Metrics capture dict 可作为复制后 Context 中的共享对象引用；任何其他
ContextVar 修改不得泄漏到 sibling。禁止在长期 dispatcher 自身 context 中执行 operation；本版不创建固定 worker
或 dispatcher。

leaf wrapper 只捕获普通 `Exception`。TaskGroup 在首个逃逸异常后取消 siblings 并等待 cleanup；execution domain
收集同时发生的原异常，按全域 declaration key 选最小者原样重抛，不向调用方暴露 ExceptionGroup。
`CircuitBreakerTripped` 使用独立的 group-control wrapper：只取消并清理当前 `run_group()`
siblings，随后向工作流属主原样重抛，不进入 root fatal ledger。ProcessWorkflow 因此可以按既有
契约完成 partial-delivery 与 exit 4 报告；sequence workflow 仍可把同一 control 作为运行终态。
`KeyboardInterrupt`、`SystemExit` 与 `CancelledError` 不进入 fatal wrapper。叶任务直接抛出
`CancelledError` 或取消自身 asyncio task 都以 cancellation 终止 execution domain，不得转为
`InternalError`。外部取消停止接纳、取消所有已创建任务、等待 cleanup、归还全部
admission/resource/origin permit 后原样重抛 `CancelledError`。
通道 producer 在创建 leaf task 后把 admission permit 所有权转交给 task done callback；即使
取消发生在协程体首次执行之前，done callback 也必须归还 permit 并记录 cancellation。

### 3.17.4 ResourceManager 与 HTTP transport

```python
class ResourceManager(ResourceLimiter):
    def __init__(self, capacities: Mapping[ResourceKey, int],
                 origins: Mapping[ResourceKey, HttpOrigin],
                 metrics: MetricsSink | None): ...

    def admission_capacity(self, resource_key: ResourceKey) -> int: ...
    def resource_limit(self, resource_key: ResourceKey) -> AsyncContextManager[None]: ...
    def origin_for(self, resource_key: ResourceKey) -> HttpOrigin: ...
    def origin_limit(self, origin: HttpOrigin) -> AsyncContextManager[None]: ...

    @property
    def http_connection_capacity(self) -> int: ...
```

容量唯一来自活动 LLM/embedding profile 的 `max_concurrency`；同名的两种 kind 是不同 ResourceKey。逻辑许可覆盖
完整调用，包括 provider attempt、轮换、retry backoff、429 cooldown 与 parking。等待许可的时间进入
`resource_wait_ms`，不混入 provider latency；取消必须归还许可。
等待中取消的调用也必须把已经过时长计入 `resource_wait_ms` 或 `http_pool_wait_ms` 恰一次，
且不消费许可。

Application 使用 `httpx.URL` 把本轮所有引用 profile 的 base URL 规范化为
`(lowercase scheme, IDNA host, effective port)`。repair、probe、生成与 embedding profile 全部计入；
同一 profile 重复引用只计一次。显式 port 必须保留，只有 port 缺席时才取 scheme
默认值；非正 port 由 ResourceManager fail closed，不得折叠到默认 origin。相同 origin 的
profile capacity 求和形成一个 origin permit，不同 origin 独立。共享
AsyncClient 在第一次真实 HTTP 调用时延迟构造：

```python
httpx.Limits(
    max_connections=resources.http_connection_capacity,
    max_keepalive_connections=resources.http_connection_capacity,
)
```

每个 HTTP attempt 先取得 origin permit。`http_pool_wait_ms` 只表示该显式 origin admission 等待；provider latency
从取得 permit 后开始。取得 permit 后仍出现 `httpx.PoolTimeout` 表示内部容量不一致，必须先于宽泛 timeout 捕获转为
InternalError，不能 retry。零 origin 的静态路径不创建容量为零的 client。

LLMClient 根实例持有共享 AsyncClient；probe child 共享它与 ResourceManager，但没有关闭权。root `aclose()` 幂等且
实际关闭一次。正常路径 close failure 是 InternalError；已有主异常或外部取消时记录英文错误日志、等待关闭完成，
然后保留原主异常或 CancelledError。live run 与 `validate --probe` 都在创建 client 的同一事件循环完成关闭。

### 3.17.5 普通工作流的纯叶任务

普通工作流保持一个活动批和固定阶段屏障：同步计划 → TaskGroup 纯叶调用 → 输入序 reduce/commit。leaf 不得修改
PipelineItem、quality pool、claim table、DedupIndex 或 emitter。RNG 只在同步计划阶段按输入序消费，leaf 不共享
RNG。所有生产并发只经 TaskExecutor，不保留第二执行分支。

普通 ProviderFatalError 继续由 operator 转为既有记录级 outcome；CircuitBreaker 只结构化取消当前任务组并交回
工作流属主，CancelledError 终止 execution domain。
semantic dedup 先同步冻结全部 CPU 特征，再对静态 participating items 投机并发 embedding；归并屏障按输入序使用
最新正式索引执行 exact、MinHash、pHash、semantic 层级。未使用 vector 不改 item/index，但真实 usage、provider、
breaker、latency 与 embedding failure 证据保留。

### 3.17.6 Sequence 候选窗口与有序提交

SequenceWorkflow 在 execution domain 内拥有唯一 coordinator TaskGroup。候选窗口始终是从 `next_commit` 起的连续
声明序区间，容量是当前 phase 引用的不同 ResourceKey 容量之和并钳制到剩余 slot。permit 在创建 coordinator 前
取得，跨 attempt、preparing、PreparedCandidate/PreparedNoiseCandidate、recoverable outcome、等待提交和 retry
保留，只在该 ordinal commit 或终止 cleanup 后释放。高 ordinal 提前结束仍占原位置，不向窗口外补 tail。

primary slot 的昂贵路径可以跨槽并发：

```text
generation → evaluation → group_reserve → quality → annotate → verify
→ assemble/replay → PrimaryCandidateReconcileRequest → PreparedCandidate
```

`DedupReservation` 状态只允许 Reserved→Validated→Committed、Reserved→Discarded 或
Validated→Discarded。pending reservation 不互相淘汰；完整特征只在 DedupIndex registry 存一份，外部 carrier 只持
opaque capability、epoch、record digests 与 exact cluster keys。reservation 在 PreparedCandidate 成功深冻结并进入
缓冲前由 coordinator 唯一拥有，之后转移给缓冲/提交协调器；未转移终态的 coordinator 在 `finally` 恰好 discard
一次。

当前 primary head 的无 `await` 顺序固定为：

```text
group_revalidate → candidate digest → CrossViewFrontier check
→ retained prospective check → 预验证 dataset/DeliveryState/frontier delta
→ group_commit → frontier/dataset/rows/replay/retained commit
```

v1.20 的 `CrossViewDelta` 还冻结当前 source primary 与全部 replay 的 `ResourceInterval`。frontier check 在 formal
mutation 前验证 event start 全局唯一与同 resource 半开区间不重叠；source/replay 只有这一个 checked delta。
replay 构造、time binding、constant shift、interval 或 retained 任一失败都在 `group_commit` 前回滚，不能先提交
primary 再补 replay。`group_commit` 后的 frontier、dataset、rows、replay、retained state swaps 只消费同一 delta。

reservation 后的 downstream recoverable outcome 保留 reservation 到 head；此时仍先 revalidate，若最新低 ordinal
前缀产生 duplicate，则记录 dedup rejection，否则才记录已保存的 downstream failure 并 discard。group_commit 后
不得再出现普通 recoverable 分支。

noise 使用独立 `NoiseCandidateReconcileRequest` 与 `PreparedNoiseCandidate`。其 head 顺序为最新 primary/较低 noise
上的 similarity probe、frozen digest、CrossViewFrontier、retained 与 delta 预验证、similarity commit、frontier/row/
retained commit，全部无 `await`。Replay 不单独运行 coordinator，只从 source 的最终 SequenceRows 按 Planner
constant shift 重绑 business time，并随 source 候选校验、计费和提交。

candidate-local reconcile 不读取已提交前缀；CrossViewFrontier 每次只检查当前候选并产生不可失败的增量提交状态。
全部 primary/noise/replay 内存提交后，`reconcile_views()` 从 program、plan、main 与 stream 独立完整重建一次，
并按每个 resource 的 sort/sweep 复验全局区间。最终 full reconcile 失败是
InternalError、exit 4，不消费 attempt、不打开输出；不存在重建已提交前缀的第二接口。

当前 head 耗尽时停止接纳，取消并等待所有更高 coordinator，discard 全部 reservation/candidate/capture。高槽未轮到
的 recoverable outcome 不进入 attempts/rejections；usage、retry、Schema、trace 与 provider latency 仍是运行事实。

### 3.17.7 Metrics 与报告

MetricsSink 的 attempt count capture 使用 ContextVar。每个 collaborator capture 一个局部 dict 并在 `finally` 用
token reset；nested capture fail closed。同组 leaf 共享所属 metrics dict，但 sibling 的其他 ContextVar 绑定隔离。
dataset counters 只在 ordered commit 后 merge。`budget.*`、Schema、LLM/embedding usage、provider retry/breaker、
resource/origin wait、provider latency、runtime cancellation 与 trace call facts 实时记录，不随 attempt rollback。

成功 report 与 failed report 都含同形状顶层 `runtime` 块；静态 dry-run 为零值：

```json
{
  "queue_high_water": 0,
  "running_high_water": 0,
  "resource_wait_high_water": 0,
  "commit_waiting_high_water": 0,
  "candidate_bytes_high_water": 0,
  "cancelled_tasks": 0,
  "resource_wait_ms": 0,
  "http_pool_wait_ms": 0,
  "commit_ms": 0
}
```

`candidate_bytes_high_water` 是所有已完成且尚未提交候选 canonical bytes 的同时驻留总和，不是单候选最大值，也不
包含 provider response、AttemptTransaction、Python 对象开销、DedupIndex registry 或 HTTP buffer。运行时只承诺
每 ResourceKey admitted leaf 上界和 sequence 窗口槽位上界；retained-content 仍是最终 canonical output accounting，
不是物理 RSS 预留。

### 3.17.8 验收

离线反例必须覆盖 profile 隔离、跨 group 全局 admission、六百任务反向完成、ContextVar sibling 隔离、跨组同时
fatal 的稳定原异常、域外/嵌套提交、外部取消 cleanup、repair 切 profile、同/异 origin 聚合、PoolTimeout 内部错误、
所有 close 路径、普通各 stage 反序 reduce、semantic dedup first-writer、六百 sequence 反序提交、reservation 失败
优先级、低槽耗尽清理、candidate-local/frontier/full 等价与 manifest-last fault points。

scheduler 的六百并发测试使用受控 coroutine，不伪称 endpoint 支持六百请求。真实 E2E 另使用
`Qwen3.5-4B-Q6_K` + `llama-server` 的四个物理 slot，证明非 mock 请求重叠、runtime/profile high-water 大于一、完整
primary/noise/replay/downstream 交付、usage、manifest 与 checker。旧/新 checkout 用同一模型、server 命令、fixture、
profile 与调用形状各运行三次，分别报告 wall、RSS、calls、tokens、attempts、rejections、resource/origin/commit
等待的 median/range；形状不同不能声称 scheduler 加速。

本地 Qwen 4B 只是 v1.19 E2E/性能补充，不替代 DeepSeek sequence failure-injection 与 z.ai structured-output 的真实
发布门。全部变更生产函数满足函数覆盖率 100%、行覆盖率至少 85%、分支覆盖率至少 75%，并通过 Uncle Bob 独立
mutation review。任何本节行为都不得留 TODO、兼容入口、旧路径、migration、fallback 或第二执行分支。
