# 18,540 券批次发布基准

本目录使用临时 SQLite 和明确标记的合成数据，不请求 AKShare/Wind，不操作真实服务或业务数据库。

## 固定场景

- 18,540 条目录，覆盖应用的 37 个地区、两个发行日期组。
- 500 条符合官方响应结构的同日成交记录，包含有效零收益率。
- 初次发布只有目录；随后两批各新增 250 份完整结构的个券详情。
- 固定任务编号、时间和输入，运行真实 `sync.materialize`，计时包含读取、标准化、汇总、序列化、SQLite 及索引提交；数据生成和检查不计时。
- 每阶段验证目录无丢失、合格券数为 0 / 250 / 500、行情覆盖为 500，并将已保存汇总与完整逐券重算比较。

## 优化前后反馈

| 阶段 | 优化前 | 优化后 |
|---|---:|---:|
| 初始目录 | 2.191 秒 | 1.816 秒 |
| 新增第一批 250 份详情 | 3.914 秒 | 0.316 秒 |
| 再新增第二批 250 份详情 | 4.103 秒 | 0.380 秒 |

第二批必须低于 1.5 秒。基线命令保存完整报告后以状态码 1 退出，形成可失败的性能反馈。优化后第二批约为原耗时的 1/10.8，通过红线，状态码为 0。上述是当前机器的同场景离线对照，不代表外网站点速度。

```powershell
.venv\Scripts\python.exe -m scripts.benchmark_sync_publication --output research/sync-performance-v2/publication-baseline.json
```

优化后的同场景复测与结果对照：

```powershell
.venv\Scripts\python.exe -m scripts.benchmark_sync_publication --compare research/sync-performance-v2/publication-baseline.json --output research/sync-performance-v2/publication-after.json
```

JSON 报告记录运行环境、实现文件哈希、参数、每阶段耗时和全量语义哈希，以及最终 18,540 券的逐券哈希。`--compare` 比较各阶段完整个券、counts、cells、coverage、benchmarks，发生差异即失败，并列出最终差异券的数量及前 100 个代码。版本编号等与结果无关的随机元数据不纳入比较。

本次三个阶段的完整语义哈希全部相同；最终 18,540 券逐券哈希差异为 0。

## 存储正确性独立验证

`tests/test_akshare_indexed_storage.py` 五项回归全部通过：增量投影部分写入后故障回滚、旧版本 CAS 拒绝、索引行或版本缺失时拒绝不完整导出、完整保存覆盖新格式后仍可完整导出，以及读取事务中另一条真实 SQLite/WAL 连接提交新版本时，读者仍取得完整旧快照，下一次读取才取得完整新版。

```powershell
.venv\Scripts\python.exe -m unittest tests.test_akshare_indexed_storage -v
```
