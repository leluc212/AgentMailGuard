"""LoRA / QLoRA supervised fine-tuning of the injection judge (Qwen2.5-7B, Llama-3.1-8B).

    python -m training.finetune_llm_judge --config training/configs/qwen2.5-7b_lora.yaml
    python -m training.finetune_llm_judge --config training/configs/llama-3.1-8b_lora.yaml
    python -m training.finetune_llm_judge --config training/configs/smoke_qwen0.5b.yaml --max-steps 5

Hardware: QLoRA (4-bit) on a 7B/8B model needs about 10-12 GB of GPU memory (T4 16 GB on
Colab/Kaggle is enough); plain LoRA in bf16 needs about 20 GB. This laptop's RTX 3050
(4 GB) cannot run it - use the smoke config to validate the pipeline locally.

After training: ``python -m training.finetune_llm_judge --config ... --merge`` writes the
merged model, then follow training/EXPORT_OLLAMA.md to convert to GGUF and register it in
Ollama as ``mailguard-qwen2.5-7b:v1`` (update configs/models.yaml).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

import yaml

from mailguard.config.settings import PROJECT_ROOT

logger = logging.getLogger("training.finetune")


def load_config(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("train_file", "datasets/processed/sft_judge/train.jsonl")
    cfg.setdefault("val_file", "datasets/processed/sft_judge/val.jsonl")
    cfg.setdefault("output_dir", f"artifacts/lora/{Path(path).stem}")
    cfg.setdefault("max_length", 2048)
    cfg.setdefault(
        "lora",
        {
            "r": 16,
            "alpha": 32,
            "dropout": 0.05,
            "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj"],
        },
    )
    cfg.setdefault(
        "train",
        {
            "epochs": 1,
            "lr": 2e-4,
            "batch_size": 2,
            "grad_accum": 8,
            "warmup_ratio": 0.03,
            "logging_steps": 20,
            "eval_steps": 200,
            "save_steps": 200,
        },
    )
    cfg.setdefault("quantization", "none")  # none | 4bit | 8bit
    return cfg


def read_jsonl(path: Path, limit: int = 0) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
                if limit and len(rows) >= limit:
                    break
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument(
        "--max-steps", type=int, default=0, help="stop after N optimizer steps (smoke tests)"
    )
    ap.add_argument("--limit", type=int, default=0, help="use only the first N training rows")
    ap.add_argument(
        "--merge", action="store_true", help="merge the trained adapter into the base weights"
    )
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = load_config(args.config)

    import torch
    from peft import LoraConfig, PeftModel, get_peft_model, prepare_model_for_kbit_training
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        DataCollatorForSeq2Seq,
        Trainer,
        TrainingArguments,
    )

    from datasets import Dataset

    base = cfg["base_model"]
    out_dir = PROJECT_ROOT / cfg["output_dir"]
    tok = AutoTokenizer.from_pretrained(base, use_fast=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    tok.padding_side = "right"

    if args.merge:
        model = AutoModelForCausalLM.from_pretrained(
            base, torch_dtype=torch.bfloat16 if torch.cuda.is_available() else torch.float32
        )
        model = PeftModel.from_pretrained(model, str(out_dir / "adapter"))
        merged = model.merge_and_unload()
        merged.save_pretrained(str(out_dir / "merged"))
        tok.save_pretrained(str(out_dir / "merged"))
        logger.info("merged model saved to %s", out_dir / "merged")
        return 0

    quant_kwargs: dict[str, Any] = {}
    if cfg["quantization"] in ("4bit", "8bit"):
        from transformers import BitsAndBytesConfig

        quant_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=cfg["quantization"] == "4bit",
            load_in_8bit=cfg["quantization"] == "8bit",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
        )
    dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32
    model = AutoModelForCausalLM.from_pretrained(
        base,
        torch_dtype=dtype,
        device_map="auto" if torch.cuda.is_available() else None,
        **quant_kwargs,
    )
    if quant_kwargs:
        model = prepare_model_for_kbit_training(model)
    lora = cfg["lora"]
    model = get_peft_model(
        model,
        LoraConfig(
            r=int(lora["r"]),
            lora_alpha=int(lora["alpha"]),
            lora_dropout=float(lora["dropout"]),
            target_modules=list(lora["target_modules"]),
            task_type="CAUSAL_LM",
        ),
    )
    model.print_trainable_parameters()

    def tokenize(example: dict) -> dict:
        msgs = example["messages"]
        prompt_ids = tok.apply_chat_template(msgs[:-1], tokenize=True, add_generation_prompt=True)
        full_ids = tok.apply_chat_template(msgs, tokenize=True, add_generation_prompt=False)
        full_ids = full_ids[: cfg["max_length"]]
        labels = [-100] * min(len(prompt_ids), len(full_ids)) + full_ids[len(prompt_ids) :]
        return {
            "input_ids": full_ids,
            "attention_mask": [1] * len(full_ids),
            "labels": labels[: len(full_ids)],
        }

    train_rows = read_jsonl(PROJECT_ROOT / cfg["train_file"], args.limit)
    val_rows = read_jsonl(
        PROJECT_ROOT / cfg["val_file"], max(50, args.limit // 10) if args.limit else 0
    )
    train_ds = Dataset.from_list(train_rows).map(
        tokenize, remove_columns=["messages", "id", "source"]
    )
    val_ds = (
        Dataset.from_list(val_rows).map(tokenize, remove_columns=["messages", "id", "source"])
        if val_rows
        else None
    )
    t = cfg["train"]
    targs = TrainingArguments(
        output_dir=str(out_dir),
        num_train_epochs=float(t["epochs"]),
        max_steps=args.max_steps if args.max_steps else -1,
        per_device_train_batch_size=int(t["batch_size"]),
        per_device_eval_batch_size=int(t["batch_size"]),
        gradient_accumulation_steps=int(t["grad_accum"]),
        learning_rate=float(t["lr"]),
        warmup_ratio=float(t["warmup_ratio"]),
        lr_scheduler_type="cosine",
        logging_steps=int(t["logging_steps"]),
        eval_strategy="steps" if val_ds is not None else "no",
        eval_steps=int(t["eval_steps"]),
        save_steps=int(t["save_steps"]),
        save_total_limit=2,
        bf16=torch.cuda.is_available(),
        gradient_checkpointing=True,
        report_to=[],
        seed=42,
    )
    trainer = Trainer(
        model=model,
        args=targs,
        train_dataset=train_ds,
        eval_dataset=val_ds,
        data_collator=DataCollatorForSeq2Seq(tok, padding=True, label_pad_token_id=-100),
    )
    trainer.train()
    model.save_pretrained(str(out_dir / "adapter"))
    tok.save_pretrained(str(out_dir / "adapter"))
    metrics = trainer.evaluate() if val_ds is not None else {}
    with open(out_dir / "train_metrics.json", "w", encoding="utf-8") as f:
        json.dump(
            {"config": cfg, "metrics": metrics, "steps": trainer.state.global_step},
            f,
            indent=2,
            default=str,
        )
    logger.info("adapter saved to %s (%s)", out_dir / "adapter", metrics)
    return 0


if __name__ == "__main__":
    sys.exit(main())
