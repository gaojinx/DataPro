"""把 HuggingFace 句向量模型导出为 OpenVINO IR（内网 / 离线部署用）。

**转换与运行是两套依赖，别混在一起**：
- 转换（只在开发机或 CI 跑一次，产物几百 MB 进制品库）：
      pip install torch --index-url https://download.pytorch.org/whl/cpu
      pip install "optimum-intel>=1.20" "optimum>=1.20" "openvino>=2024.4" "transformers>=4.40"
- 运行（生产镜像只需这些）：
      pip install "openvino>=2024.4" "transformers>=4.40" "numpy>=1.24"
  见 `requirements-local-encoder.txt`。

用法：
  python -m importer.export_encoder
  python -m importer.export_encoder --model BAAI/bge-small-zh-v1.5 --out models/bge-small-zh-ov
  python -m importer.export_encoder --force --pooling cls

产物目录里应包含 `openvino_model.xml` + `.bin` + tokenizer 文件；
脚本会自动扫描并打印可直接粘贴的配置片段。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

SAMPLE = "上个月的支付订单数是多少"

CLI_FALLBACK = """自动导出失败时，可直接执行官方 CLI（参数来自 Optimum 文档）：

  optimum-cli export openvino --model {model} --task feature-extraction --weight-format fp32 {out}
  # 若报 library 推断失败，改用句子向量库：
  optimum-cli export openvino --model {model} --task sentence-similarity --library sentence_transformers {out}
"""


def _missing_convert_deps() -> list[str]:
    missing = []
    for mod, pkg in (("torch", "torch"), ("openvino", "openvino"),
                     ("transformers", "transformers"), ("optimum.intel", "optimum-intel")):
        try:
            __import__(mod)
        except Exception:  # noqa: BLE001 —— 缺依赖就是缺，不区分原因
            missing.append(pkg)
    return missing


def _guard_out_dir(out_dir: Path) -> None:
    """--force 会 rmtree 目标目录，先挡掉「误传成项目根/家目录/盘根」这类不可恢复的输入。

    `--out ..` 这种也要拦：它等于删掉整个仓库。规则是「不能是当前目录本身或其祖先」，
    允许显式的其他绝对路径（如 D:/models/xxx）。
    """
    resolved = out_dir.resolve()
    cwd = Path.cwd().resolve()
    home = Path.home().resolve()
    if resolved == cwd or resolved in cwd.parents or resolved == home or resolved in home.parents:
        raise SystemExit(f"拒绝在 {resolved} 上执行删除重建，请指定具体子目录"
                         f"（如 models/bge-small-zh-ov）")
    if len(resolved.parts) <= 2:
        raise SystemExit(f"拒绝在盘根 {resolved} 上执行删除重建，请指定具体子目录")


def _find_ir(out_dir: Path) -> Path | None:
    """在导出目录里找 IR 的 .xml。

    不硬编码 `openvino_model.xml`：不同 Optimum 版本的文件名可能是 `model.xml`，
    硬编码会让脚本在版本升级后「导出成功但验证找不到文件」。
    """
    candidates = sorted(out_dir.glob("*.xml"))
    if not candidates:
        return None
    for c in candidates:
        if c.stem in ("openvino_model", "model"):
            return c
    return candidates[0]


def _export(model_id: str, out_dir: Path, trust_remote_code: bool) -> None:
    from optimum.intel import OVModelForFeatureExtraction
    from transformers import AutoTokenizer

    print(f"导出 {model_id} → {out_dir}")
    model = OVModelForFeatureExtraction.from_pretrained(
        model_id, export=True, trust_remote_code=trust_remote_code)
    model.save_pretrained(str(out_dir))
    tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=trust_remote_code)
    tokenizer.save_pretrained(str(out_dir))


def _verify(out_dir: Path, sample: str, pooling: str, max_length: int) -> dict:
    """用 OpenVINO Runtime 重新加载 IR 跑一句，确认输出形状与池化方式匹配。

    复用生产同一个 `pool_embeddings`：验证逻辑和生产代码分两套时，
    最典型的失败是「验证过、上线错」，而且不报错。
    """
    import numpy as np
    import openvino as ov
    from transformers import AutoTokenizer

    from app.services.vector_recall import pool_embeddings

    xml = _find_ir(out_dir)
    if xml is None:
        raise SystemExit(f"导出目录里没有 .xml：{out_dir}，导出可能没成功")
    compiled = ov.Core().compile_model(str(xml), "CPU")
    out_port = compiled.output(0)
    input_names = [i.get_any_name() for i in compiled.inputs]

    tokenizer = AutoTokenizer.from_pretrained(str(out_dir))
    enc = tokenizer([sample], padding=True, truncation=True, max_length=max_length,
                    return_tensors="np")
    feed = {n: np.asarray(enc[n], dtype=np.int64) for n in input_names if n in enc}
    raw = compiled(feed)[out_port]
    arr = np.asarray(raw)
    vec = pool_embeddings(raw, enc.get("attention_mask"), pooling)
    norm = float(np.linalg.norm(vec[0]))
    print(f"IR 输出 shape={arr.shape}（{arr.ndim} 维），输入={input_names}")
    print(f"池化方式={pooling} → 句向量维度={vec.shape[-1]}，L2 范数={norm:.4f}")
    if norm <= 1e-6:
        raise SystemExit("句向量全 0：池化方式与模型不匹配，检查 --pooling（cls / mean）")
    return {"xml": xml, "dim": int(vec.shape[-1]), "ndim": int(arr.ndim)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="BAAI/bge-small-zh-v1.5")
    ap.add_argument("--out", default="models/bge-small-zh-ov")
    ap.add_argument("--pooling", default="cls", choices=["cls", "mean"])
    ap.add_argument("--max-length", type=int, default=512)
    ap.add_argument("--sample", default=SAMPLE)
    ap.add_argument("--trust-remote-code", action="store_true")
    ap.add_argument("--force", action="store_true", help="输出目录已存在时删除重建")
    args = ap.parse_args()

    # 参数校验先于依赖检查：路径写错时不该先让用户去装几个 GB 的包
    out_dir = Path(args.out)
    _guard_out_dir(out_dir)

    missing = _missing_convert_deps()
    if missing:
        print("缺少转换依赖：" + "、".join(missing), file=sys.stderr)
        print("安装（CPU 版 torch 足够，不需要 CUDA）：", file=sys.stderr)
        print('  pip install torch --index-url https://download.pytorch.org/whl/cpu', file=sys.stderr)
        print('  pip install "optimum-intel>=1.20" "optimum>=1.20" "openvino>=2024.4" '
              '"transformers>=4.40"', file=sys.stderr)
        print("装好后重跑本脚本；不想装 torch 也可以用 CLI 导出（见文件头注释）。",
              file=sys.stderr)
        raise SystemExit(2)

    out_dir = Path(args.out)
    if out_dir.exists() and any(out_dir.iterdir()):
        if not args.force:
            raise SystemExit(f"输出目录非空：{out_dir}；确认覆盖请加 --force")
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        _export(args.model, out_dir, args.trust_remote_code)
    except Exception as exc:  # noqa: BLE001 —— 导出失败必须给出可执行替代路径
        print(f"导出失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        print(CLI_FALLBACK.format(model=args.model, out=args.out), file=sys.stderr)
        raise SystemExit(1) from exc

    info = _verify(out_dir, args.sample, args.pooling, args.max_length)
    size_mb = sum(p.stat().st_size for p in out_dir.rglob("*") if p.is_file()) / 1024 / 1024
    print(f"产物：{out_dir}（{size_mb:.1f} MB）")
    print("\n配置（写入 .env，或改为容器内的绝对路径）：")
    print("  VECTOR_ENCODER=local")
    print(f"  VECTOR_IR_PATH={info['xml'].as_posix()}")
    print(f"  VECTOR_TOKENIZER={out_dir.as_posix()}")
    print(f"  VECTOR_POOLING={args.pooling}")
    print("\n换模型或换池化方式后，务必重跑：python -m importer.warm_vectors")


if __name__ == "__main__":
    main()
