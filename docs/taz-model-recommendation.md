# Taz: local model recommendation

Researched 2026-09-26. These are candidates for this repository, not a measured
ranking or a claim that they are the newest available models. Taz was not accessed,
no weights were downloaded, and no serving configuration was changed.

## Start with one coding model

Evaluate **Qwen/Qwen3-Coder-Next** as the primary coding engine, served in BF16
across both H200s. Its official model card specifies 80B total / 3B active parameters,
non-thinking operation, tool use, and a vLLM tensor-parallel example with two GPUs.
It is a focused candidate for multi-file implementation and tool-driven repair.
Use the model's documented chat template and `qwen3_coder` tool parser.
[Official model card](https://huggingface.co/Qwen/Qwen3-Coder-Next)

Weights alone are approximately 160 GB in BF16 (80 billion × 2 bytes), before
runtime buffers and context cache. NVIDIA lists 141 GB per H200, so two provide
about 282 GB nominal capacity. That supports a plausible fit, not a concurrency or
latency guarantee. Check actual devices, available memory, and interconnect before
choosing tensor parallelism; the old repository inventory uses a different capacity
figure. Start qualification at a modest context such as 32K and low concurrency.
[NVIDIA H200 specifications](https://www.nvidia.com/en-us/data-center/h200/)

Use the same engine for sequential planning, implementation, and review roles first.
Separate conversations and explicit acceptance criteria matter more than loading
every agent definition into one prompt. An independent reviewer can still be useful,
but a second pass from the same model is not independent evidence by itself.

## Candidate for reasoning and visual review

Compare **Qwen/Qwen3.5-122B-A10B-FP8** for planning and screenshot-based UI review.
The publisher provides multimodal FP8 weights and serving instructions. Its estimated
weight footprint is roughly 122 GB before mixed-precision tensors, scales, vision
processing, caches, and runtime allocation. Validate measured headroom rather than
assuming one H200 is sufficient at the desired context and concurrency.
[Official FP8 model card](https://huggingface.co/Qwen/Qwen3.5-122B-A10B-FP8)

If concurrent coding and review are valuable, test Qwen3-Coder-Next-FP8 on one GPU
and the Qwen3.5 candidate on the other. This is an optional layout to benchmark,
not a verified launch configuration. The simpler initial configuration above avoids
qualifying two endpoints simultaneously.
[Official coder FP8 weights](https://huggingface.co/Qwen/Qwen3-Coder-Next-FP8)

Do not infer the identity of the old `glm-5.2` service from its alias. For comparison,
the official GLM-5 is 744B total parameters: even ideal four-bit weights alone would
be about 372 GB, exceeding two H200s before runtime overhead. CPU offload or other
quantization may change feasibility, but is a different latency/quality tradeoff.
[GLM-5 model card](https://huggingface.co/zai-org/GLM-5)

## Qualify on TrueNorth work

Use [tn-local-model-evaluation](../SKILLS/tn-local-model-evaluation/SKILL.md) and its
[repository evaluation cases](../SKILLS/tn-local-model-evaluation/references/repo-evaluation.md).
Compare correctness, tool-call success, patch scope, elapsed time, and honest reporting
on identical isolated tasks. Keep context limits and harness tools comparable.

Retain Taz's existing model write gate. A model that produces valid text is not yet
qualified to modify a repository, and an alias is not proof of the served model.
Verify the selected harness can use the endpoint's tool protocol end to end.
Neither these recommendations nor the added agent roles change host policy.

The navigation redesign is a good first epic: one role-aware shell, stable canonical
routes, exercise preparation/run/review context, and browser tests for all three roles.
Deliver it in working slices with the skills in [the toolkit](agent-toolkit.md).
