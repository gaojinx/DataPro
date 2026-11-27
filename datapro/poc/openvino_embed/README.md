# OpenVINO embedding 加速 POC

独立评估：同一 embedding 模型在 CPU 上 **PyTorch** 与 **OpenVINO IR** 的延迟/吞吐差异，
用于判断是否值得作为 A05 向量召回的推理后端。**不修改主链路、不进入主依赖**。

## 环境

- 本机：i7-12700H（14 核 20 线程），Windows，Python 3.14
- OpenVINO 2026 支持 Python 3.10–3.14

## 运行

```powershell
# 1) 独立 venv（避免污染主环境）
& "C:\Program Files\Python314\python.exe" -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2) 国内网络 + 避免系统缓存目录权限问题
$env:HF_ENDPOINT="https://hf-mirror.com"
$env:HF_HOME="e:\Project\AgentProject\datapro\.tmp\hf-cache"
$env:HF_HUB_DISABLE_SYMLINKS_WARNING="1"

# 3) 跑对比（默认 bge-small-zh-v1.5，batch 1/8/32）
.\.venv\Scripts\python.exe bench_embedding.py --backend both --batch 1 8 32 --repeat 3

# 4) 量化（INT8 全量 PTQ + fp16 + 仅权重 INT8）
.\.venv\Scripts\python.exe quantize_embedding.py --model BAAI/bge-small-zh-v1.5 --preset mixed --fp16 --weights-only
.\.venv\Scripts\python.exe bench_quantized.py --batch 1 8 32 --repeat 3
.\.venv\Scripts\python.exe eval_recall_consistency.py

# 5) 大模型（BGE-M3 只做 fp16 + 仅权重压缩，跳过全量 PTQ）
.\.venv\Scripts\python.exe quantize_embedding.py --model BAAI/bge-m3 --skip-int8 --weights-only --fp16
```

结果输出到 `results.md`（延迟、吞吐、两种后端向量余弦一致性）。

## 实测结果（2026-11-24，i7-12700H / 20 逻辑核）

| 后端 | batch | p50 | texts/s |
|---|---|---|---|
| PyTorch | 1 | 6.76 ms | 146 |
| OpenVINO | 1 | 4.07 ms | 244 |
| PyTorch | 8 | 16.99 ms | 401 |
| OpenVINO | 8 | 15.74 ms | 447 |
| PyTorch | 32 | 45.10 ms | 444 |
| OpenVINO | 32 | 39.83 ms | 503 |

- 单条（在线查询）p50 加速 **1.66x**，批量建库约 1.1x
- torch 与 OpenVINO 向量余弦一致性 **1.000000**
- 结论：适合作为 A05 在线 query embedding 的推理后端；建库批量收益有限，需再评估 INT8

## 量化方案对比（2026-11-24，bge-small-zh-v1.5）

校准集 130 条业务问句/口径定义（`build_calib`），INT8 全量量化为 NNCF MIXED preset。

| 方案 | p50 加速 | 向量余弦 | Top-1 一致率 | Top-5 重合率 | fp32 Top-1 仍在 Top-5 | 判定 |
|---|---|---|---|---|---|---|
| fp16 | 1.05–1.12x | 1.0000 | 100% | 100% | 100% | 无损但增益有限 |
| INT8 全量（MIXED） | 1.75–2.87x | 0.9610 | 88.0% | 84.4% | 100% | ❌ 未达门槛 |
| INT8 仅权重 | 1.72–2.08x | 0.9987 | 98.0% | 98.8% | 100% | ✅ 达标 |

- 早期 45 条校准 + PERFORMANCE preset 时余弦仅 0.904、Top-5 79.2%；扩校准集并改 MIXED 后升到 0.961/84.4%，仍不达标
- INT8 全量的 Top-1 翻转基本是近义文档互换（销售额↔客单价、曝光量↔点击率），fp32 Top-1 100% 仍在 Top-5 内
- **结论：线上 query embedding 选「仅权重 INT8」**（`nncf.compress_weights`，INT8_ASYM），精度与 fp16 基本等价、单条 1.7x；全量 INT8 激活量化只适合能接受排序抖动的离线建库
- 明细见 `results_int8.md`、`recall_consistency.md`

## BGE-M3 复测（2026-11-24）

同机 CPU（i7-12700H），只做 fp16 与仅权重 INT8（跳过全量 PTQ，大模型校准太慢）。

| 方案 | 单条 p50 | batch 8 | batch 32 | 向量余弦 | Top-1 一致 | Top-5 重合 |
|---|---|---|---|---|---|---|
| fp32 | 61.1 ms | 273 ms | 773 ms | — | — | — |
| fp16 | 65.2 ms | 298 ms | 829 ms | 1.0000 | 100% | 100% |
| 仅权重 INT8 | 25.1 ms | 133 ms | 377 ms | 0.9992 | 100% | 98.0% |

- 仅权重 INT8 单条 **61.1→25.1ms（2.43x）**，批量约 2.05x，精度几乎无损（余弦 0.9992、Top-1 100%、Top-5 98%）
- fp16 在 M3 上反而略慢（0.92–0.94x），不适合作为加速手段
- 对比 bge-small：M3 单条仍是 25ms vs 2.4ms，在线查询建议 bge-small + 仅权重 INT8；M3 更适合离线建库/长文本、多语言场景
- 明细见 `results_m3.md`、`recall_consistency_m3.md`

## 判定门槛（建议）

- p50 加速比 ≥ 1.5x，且向量余弦一致性 ≥ 0.999
- 召回质量：用评测集或 50 条真实问题对比 Top-5 命中，不能只看速度
- 量化上线门槛：Top-5 重合率 ≥ 95% 且 Top-1 一致率 ≥ 90%

## 边界

- 只测 embedding 推理，不包含向量库检索、rerank、网络 IO
- 评测语料是 POC 内置的 50 条业务问句 × 30 条候选文档，不等同真实召回集；接入 A05 前需用真实元数据重跑
- BGE-M3 等大模型在 CPU 上仍偏重，接入前建议 INT8 量化并复测
- 本地模型效果不等同云端 DeepSeek，是否替换主链路需单独评测
