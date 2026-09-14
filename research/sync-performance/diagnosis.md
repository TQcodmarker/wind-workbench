# 全市场基本信息同步性能诊断

测量时间：2026-09-12 23:53 至 2026-09-13 00:08（Asia/Shanghai）。本目录基准仅使用临时 SQLite 与生成数据，未请求 AKShare/Wind 外网，未写业务数据库。同步真实服务的恢复和观察由集成任务单独执行。

## 复现与假设

可失败的反馈命令：

```powershell
.\.venv\Scripts\python.exe -m scripts.benchmark_sync_throughput --output research/sync-performance/process-pool.json
```

脚本运行真实 `akshare_sync._details` 调度、校验、持久化与最终物化；只将 SDK 查询替换为每次固定等待 120ms 的可序列化替身。24 只券必须全部完成，且耗时不得超过预设 4.8 秒。旧流程两次均失败：7.2149 / 7.1712 秒；优化后两次均通过：3.9356 / 3.9264 秒。

改动前提出的可证伪假设及探针结果：

1. 若串行 I/O 是主要瓶颈，隔离两个 SDK 查询进程应该改善固定延迟吞吐。补充的 400ms 固定延迟对照，串行 13.7109 秒，双进程 5.3702 秒，约 2.55 倍。该补充测量使用 `--no-assert`，输出中的 4.8 秒默认预算仅适用于主基准，未用于该组判定。
2. 若响应后固定睡眠是主要等待来源，单独跳过这段睡眠应明显缩短时间。改动前仅跳过 150ms 响应后等待，120ms 基准由 7.21 秒降至 3.5423 秒。正式实现仍限速，改用跨两个工作进程的 150ms 请求起始间隔。
3. 若每条响应重新汇总整个名单带来附加开销，避免无人使用的进度汇总应减少本地读库。原 `_update_owned` 每次写 phase 后都会调用 `_progress`，包含两个分组计数和任务读取；现只返回已更新任务，API 查询和物化发布仍返回完整统计。未把这部分单独量化为端到端加速贡献。

已保存查询时序也支持同步串行等待的判断：只读最新 501 次详情查询起始时间，23:50:26 至 23:55:08，相邻平均 0.564 秒。时间戳仅精确到秒，包含网络、解析、等待和物化，不能当作纯网络 RTT。见 `saved-query-timing.json`。

## 实现边界

- 使用两个常驻 spawn 子进程隔离官方 SDK 的进程级 monkeypatch，每个进程独立使用 QueryRecorder。没有线程并发修改 SDK。
- 共享 ctypes Value、Lock、Event 控制请求起始节流及停止；没有 Manager 进程。父进程最多提交两个 in-flight 查询。
- 子进程显式绑定父进程传入的数据库绝对路径和运行模式，仅写原始查询证据。父进程在同一个 SQLite 事务内再次校验 generation、running 状态，才写详情与成功进度。
- 收到 401/403/429 即停止新查询；暂停/继续会丢弃旧 generation 的待接受结果。退出时停止、取消未开始工作并等待池关闭，保留可恢复状态。
- 继续使用官方 `bond_info_detail_cm(symbol=...)` 与已验证目录身份，保留 5/15 秒 TLS 请求超时、3 次暂时失败重试、250 只或 60 秒发布节奏、同日行情优先级及 Wind 数据隔离。
- 实际速度受上游延迟、拒绝请求和全量物化开销影响。以上比例是受控离线对照，不是外网站点的吞吐承诺。

## 回归

原有同步、合并、数据测试 40 项通过；新增 5 项真正 spawn 的测试通过：共享节流及显式 DB、在途暂停再继续、HTTP429 有界停止、最多 3 次重试、future 已返回但提交前 generation 改变。每项结束检查无新增 multiprocessing 子进程遗留。

另有 QueryRecorder gate 顺序与暂停前不发送请求的测试，以及原 3 项规范 SDK 适配测试通过。命令：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_akshare_sync tests.test_akshare_query_pool tests.test_sync_dataset_merge tests.test_akshare_provider tests.test_akshare_query_recorder -q
```

无新增调试日志；基准脚本保留为可重复运行的速度回归工具。
