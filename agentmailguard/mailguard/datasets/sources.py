"""Registry of the public datasets used by AgentMailGuard (download + provenance).

Every source is a peer-reviewed benchmark, a vendor-published challenge dataset or a
widely used public corpus with an explicit license. The registry is the single place
that records *where* data comes from so the paper's Section VI-A can be regenerated
from ``python -m mailguard.datasets.download --manifest``.

Roles
-----
    l1_train     text -> {0 benign, 1 injection} pairs for the Stage-2 classifier
    email_bench  email-context indirect-injection benchmark cases (ASR / TMR / DER)
    rag_poison   poisoned-passage generation material (PoisonedRAG)
    benign       benign emails / support requests (TSR, FPR)
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DatasetSource:
    name: str
    kind: str  # hf | github
    ref: str  # HF dataset id or GitHub owner/repo
    files: tuple[str, ...]  # exact paths or glob prefixes ending with '*'
    license: str
    citation: str
    url: str
    roles: tuple[str, ...]
    description: str
    branch: str = "main"
    size_hint_mb: float = 1.0
    gated: bool = False
    notes: str = ""
    extra: dict[str, str] = field(default_factory=dict)


SOURCES: dict[str, DatasetSource] = {
    # ------------------------------------------------------------- injection classification
    "deepset_prompt_injections": DatasetSource(
        name="deepset_prompt_injections",
        kind="hf",
        ref="deepset/prompt-injections",
        files=("data/train-*", "data/test-*"),
        license="Apache-2.0",
        citation="deepset (2023). prompt-injections dataset. Hugging Face.",
        url="https://huggingface.co/datasets/deepset/prompt-injections",
        roles=("l1_train",),
        description="662 labelled prompts (injection vs. legitimate) used to train deepset's DeBERTa detector.",
        size_hint_mb=0.1,
    ),
    "jackhhao_jailbreak": DatasetSource(
        name="jackhhao_jailbreak",
        kind="hf",
        ref="jackhhao/jailbreak-classification",
        files=(
            "balanced/jailbreak_dataset_train_balanced.csv",
            "balanced/jailbreak_dataset_test_balanced.csv",
        ),
        license="Apache-2.0",
        citation="Hao, J. (2023). jailbreak-classification. Hugging Face.",
        url="https://huggingface.co/datasets/jackhhao/jailbreak-classification",
        roles=("l1_train",),
        description="Balanced jailbreak vs. benign prompt classification set (~1.3k rows).",
        size_hint_mb=1.7,
    ),
    "xtram1_safeguard": DatasetSource(
        name="xtram1_safeguard",
        kind="hf",
        ref="xTRam1/safe-guard-prompt-injection",
        files=("data/train-*", "data/test-*"),
        license="unspecified (public)",
        citation="xTRam1 (2024). safe-guard-prompt-injection. Hugging Face.",
        url="https://huggingface.co/datasets/xTRam1/safe-guard-prompt-injection",
        roles=("l1_train",),
        description="~10k prompts labelled injection / benign, diverse phrasing.",
        size_hint_mb=2.5,
        notes="License not declared by the author; used for training only, not redistributed.",
    ),
    "lakera_gandalf": DatasetSource(
        name="lakera_gandalf",
        kind="hf",
        ref="Lakera/gandalf_ignore_instructions",
        files=("data/train-*", "data/validation-*", "data/test-*"),
        license="MIT",
        citation="Lakera AI (2023). gandalf_ignore_instructions. Hugging Face.",
        url="https://huggingface.co/datasets/Lakera/gandalf_ignore_instructions",
        roles=("l1_train",),
        description="1k real user prompts that tried to make Gandalf ignore its instructions (all positive).",
        size_hint_mb=0.1,
        extra={"label": "1"},
    ),
    "trustairlab_itw_jailbreak": DatasetSource(
        name="trustairlab_itw_jailbreak",
        kind="hf",
        ref="TrustAIRLab/in-the-wild-jailbreak-prompts",
        files=(
            "jailbreak_2023_12_25/train-00000-of-00001.parquet",
            "regular_2023_12_25/train-00000-of-00001.parquet",
        ),
        license="MIT",
        citation='Shen et al. (2024). "Do Anything Now": Characterizing and Evaluating In-The-Wild Jailbreak Prompts on LLMs. ACM CCS 2024.',
        url="https://huggingface.co/datasets/TrustAIRLab/in-the-wild-jailbreak-prompts",
        roles=("l1_train",),
        description="1.4k in-the-wild jailbreak prompts (positive) and regular prompts (negative) collected from Reddit/Discord/websites.",
        size_hint_mb=15,
        extra={
            "positive_file": "jailbreak_2023_12_25",
            "negative_file": "regular_2023_12_25",
            "negative_cap": "3000",
        },
    ),
    # ------------------------------------------------------------- email-specific injection
    "llmail_inject": DatasetSource(
        name="llmail_inject",
        kind="hf",
        ref="microsoft/llmail-inject-challenge",
        files=(
            "data/labelled_unique_submissions_phase2.json",
            "data/raw_submissions_phase2.jsonl",
            "data/emails_for_fp_tests.json",
            "data/scenarios.json",
            "data/system_prompt.json",
            "data/levels_descriptions.json",
            "data/objectives_descriptions.json",
        ),
        license="MIT",
        citation="Abdelnabi et al. (2025). LLMail-Inject: A Dataset from a Realistic Adaptive Prompt Injection Challenge. Microsoft.",
        url="https://huggingface.co/datasets/microsoft/llmail-inject-challenge",
        roles=("email_bench", "l1_train"),
        description="~370k adaptive attack emails from a public challenge against an email assistant with send_email tool; includes benign emails for false-positive tests.",
        size_hint_mb=335,
        notes="Phase-1 files (>1 GB) are skipped. Phase-2: raw_submissions_phase2.jsonl holds the email text, labelled_unique_submissions_phase2.json holds per-submission labels (attack_attempt, reason).",
    ),
    "injecagent": DatasetSource(
        name="injecagent",
        kind="github",
        ref="uiuc-kang-lab/InjecAgent",
        files=(
            "data/attacker_cases_dh.jsonl",
            "data/attacker_cases_ds.jsonl",
            "data/user_cases.jsonl",
            "data/test_cases_dh_base.json",
            "data/test_cases_ds_base.json",
            "data/test_cases_dh_enhanced.json",
            "data/test_cases_ds_enhanced.json",
        ),
        license="MIT",
        citation="Zhan et al. (2024). InjecAgent: Benchmarking Indirect Prompt Injections in Tool-Integrated LLM Agents. Findings of ACL 2024.",
        url="https://github.com/uiuc-kang-lab/InjecAgent",
        roles=("email_bench",),
        description="1,054 indirect-injection test cases (direct harm + data stealing) over tool-integrated agents; adapted here to the email tool surface.",
        size_hint_mb=3.5,
    ),
    "bipia": DatasetSource(
        name="bipia",
        kind="github",
        ref="microsoft/BIPIA",
        files=(
            "benchmark/email/train.jsonl",
            "benchmark/email/test.jsonl",
            "benchmark/text_attack_train.json",
            "benchmark/text_attack_test.json",
        ),
        license="MIT",
        citation="Yi et al. (2025). Benchmarking and Defending Against Indirect Prompt Injection Attacks on Large Language Models. ACM SIGKDD 2025.",
        url="https://github.com/microsoft/BIPIA",
        roles=("email_bench",),
        description="Email QA task contexts plus text attack instructions (15 attack types) for indirect prompt injection.",
        size_hint_mb=0.1,
    ),
    # ------------------------------------------------------------- RAG poisoning
    "poisonedrag": DatasetSource(
        name="poisonedrag",
        kind="github",
        ref="sleeepeer/PoisonedRAG",
        files=(
            "results/adv_targeted_results/nq.json",
            "results/adv_targeted_results/hotpotqa.json",
            "results/adv_targeted_results/msmarco.json",
        ),
        license="MIT",
        citation="Zou et al. (2025). PoisonedRAG: Knowledge Corruption Attacks to Retrieval-Augmented Generation of Large Language Models. USENIX Security 2025.",
        url="https://github.com/sleeepeer/PoisonedRAG",
        roles=("rag_poison",),
        description="Adversarial target questions/answers with generated poisoned passages for NQ, HotpotQA and MS-MARCO.",
        size_hint_mb=0.4,
    ),
    # ------------------------------------------------------------- benign corpora
    "enron_ham": DatasetSource(
        name="enron_ham",
        kind="hf",
        ref="SetFit/enron_spam",
        files=("test.jsonl",),
        license="public domain (Enron corpus, FERC release)",
        citation="Metsis, Androutsopoulos & Paliouras (2006). Spam Filtering with Naive Bayes - Which Naive Bayes? CEAS 2006 (Enron-Spam).",
        url="https://huggingface.co/datasets/SetFit/enron_spam",
        roles=("benign",),
        description="Real corporate emails; the 'ham' half provides realistic benign email bodies.",
        size_hint_mb=6,
        extra={"label_field": "label_text", "benign_value": "ham"},
    ),
    "bitext_support": DatasetSource(
        name="bitext_support",
        kind="hf",
        ref="bitext/Bitext-customer-support-llm-chatbot-training-dataset",
        files=("Bitext_Sample_Customer_Support_Training_Dataset_27K_responses-v11.csv",),
        license="CDLA-Sharing-1.0",
        citation="Bitext (2024). Customer Support LLM Chatbot Training Dataset (27 intents). Hugging Face.",
        url="https://huggingface.co/datasets/bitext/Bitext-customer-support-llm-chatbot-training-dataset",
        roles=("benign",),
        description="27k customer-support requests across 27 intents (refund, invoice, delivery, ...) -> benign task emails.",
        size_hint_mb=19,
    ),
}


def by_role(role: str) -> list[DatasetSource]:
    return [s for s in SOURCES.values() if role in s.roles]


def manifest_rows() -> list[dict[str, str]]:
    return [
        {
            "name": s.name,
            "kind": s.kind,
            "ref": s.ref,
            "license": s.license,
            "roles": ", ".join(s.roles),
            "url": s.url,
            "citation": s.citation,
        }
        for s in SOURCES.values()
    ]


__all__ = ["SOURCES", "DatasetSource", "by_role", "manifest_rows"]
