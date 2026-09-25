# Exporting a fine-tuned judge to Ollama

1. Merge the LoRA adapter into the base weights:
   `python -m training.finetune_llm_judge --config training/configs/qwen2.5-7b_lora.yaml --merge`
   -> `artifacts/lora/qwen2.5-7b-judge-v1/merged/`
2. Convert to GGUF with llama.cpp (https://github.com/ggml-org/llama.cpp):
   `python convert_hf_to_gguf.py artifacts/lora/qwen2.5-7b-judge-v1/merged --outfile mailguard-qwen2.5-7b.gguf --outtype q8_0`
   `./llama-quantize mailguard-qwen2.5-7b.gguf mailguard-qwen2.5-7b-q4_k_m.gguf Q4_K_M`
3. Create a Modelfile:
   ```
   FROM ./mailguard-qwen2.5-7b-q4_k_m.gguf
   PARAMETER temperature 0
   PARAMETER num_ctx 8192
   ```
   `ollama create mailguard-qwen2.5-7b:v1 -f Modelfile`
4. Register in `configs/models.yaml`:
   ```yaml
   mailguard-qwen2.5-7b:
     backend: ollama
     tag: mailguard-qwen2.5-7b:v1
     num_ctx: 8192
   ```
   and point `GUARD_MODELS__JUDGE=mailguard-qwen2.5-7b` in `.env`.
5. Re-run `python -m evaluation.run_benchmark --agent qwen2.5-7b-instruct --guard-model mailguard-qwen2.5-7b --config all`.
