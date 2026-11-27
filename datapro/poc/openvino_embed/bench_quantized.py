"""对比 OpenVINO fp32 IR 与 INT8 IR 的 embedding 性能与向量一致性。

用法：
  python bench_quantized.py --model BAAI/bge-small-zh-v1.5 --batch 1 8 32 --repeat 3
"""
import argparse
import statistics
import time
from pathlib import Path

import numpy as np

from bench_embedding import TEXTS


def load(ir_path: Path, tokenizer):
    import openvino as ov

    compiled = ov.Core().compile_model(str(ir_path), "CPU")
    out_port = compiled.output(0)
    input_names = [i.get_any_name() for i in compiled.inputs]

    def encode(texts):
        enc = tokenizer(texts, padding=True, truncation=True, max_length=128,
                        return_tensors="np")
        feed = {name: np.asarray(enc[name], dtype=np.int64)
                for name in input_names if name in enc}
        hidden = np.asarray(compiled(feed)[out_port])[:, 0]
        return hidden / (np.linalg.norm(hidden, axis=1, keepdims=True) + 1e-12)

    return encode


def measure(encode, batch: int, repeat: int) -> dict:
    batches = [TEXTS[i:i + batch] for i in range(0, len(TEXTS), batch)]
    for _ in range(2):
        for b in batches:
            encode(b)
    samples, started = [], time.perf_counter()
    for _ in range(repeat):
        for b in batches:
            t0 = time.perf_counter()
            encode(b)
            samples.append((time.perf_counter() - t0) * 1000)
    total = time.perf_counter() - started
    samples.sort()
    return {"batch": batch, "p50_ms": round(statistics.median(samples), 2),
            "p95_ms": round(samples[int(len(samples) * 0.95) - 1], 2),
            "texts_per_sec": round(len(TEXTS) * repeat / total, 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="BAAI/bge-small-zh-v1.5")
    ap.add_argument("--cache-dir", default=str(Path(__file__).parent / ".cache"))
    ap.add_argument("--batch", type=int, nargs="+", default=[1, 8, 32])
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--out", default=str(Path(__file__).parent / "results_int8.md"))
    args = ap.parse_args()

    from transformers import AutoTokenizer

    cache = Path(args.cache_dir)
    stem = args.model.replace("/", "__")
    tok = AutoTokenizer.from_pretrained(args.model)
    paths = {"ov-fp32": cache / f"{stem}.xml"}
    for p in sorted(cache.glob(f"{stem}.*.xml")):
        paths[f"ov-{p.name[len(stem) + 1:-4]}"] = p
    encoders = {name: load(p, tok) for name, p in paths.items()}

    rows = []
    for name, encode in encoders.items():
        for batch in args.batch:
            row = measure(encode, batch, args.repeat)
            row["backend"] = name
            rows.append(row)
            print(f"[{name}] batch={batch} p50={row['p50_ms']}ms p95={row['p95_ms']}ms "
                  f"qps={row['texts_per_sec']}")

    sample = TEXTS[:8]
    sims = {name: float(np.mean(np.sum(encoders["ov-fp32"](sample) * enc(sample), axis=1)))
            for name, enc in encoders.items() if name != "ov-fp32"}
    for name, sim in sims.items():
        print(f"fp32 vs {name} 向量余弦一致性：{sim:.6f}")

    speedup = {}
    for name in [n for n in encoders if n != "ov-fp32"]:
        speedup[name] = {}
        for batch in args.batch:
            f = next(r for r in rows if r["backend"] == "ov-fp32" and r["batch"] == batch)
            q = next(r for r in rows if r["backend"] == name and r["batch"] == batch)
            speedup[name][batch] = round(f["p50_ms"] / q["p50_ms"], 2)
        print(f"{name} 相对 fp32 的 p50 加速比：", speedup[name])

    lines = ["# OpenVINO 量化结果", "",
             f"- 模型：`{args.model}`", f"- 文本数：{len(TEXTS)}，每个 batch 重复 {args.repeat} 次", "",
             "| 后端 | batch | p50 (ms) | p95 (ms) | texts/s |", "|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['backend']} | {r['batch']} | {r['p50_ms']} | {r['p95_ms']} | {r['texts_per_sec']} |")
    lines += [""]
    for name, sim in sims.items():
        lines.append(f"fp32 vs {name} 向量余弦一致性：`{sim:.6f}`")
    lines += [""]
    for name, sp in speedup.items():
        lines.append(f"{name} 相对 fp32 的 p50 加速比：`{sp}`")
    Path(args.out).write_text("\n".join(lines), encoding="utf-8")
    print("结果已写入", args.out)


if __name__ == "__main__":
    main()
