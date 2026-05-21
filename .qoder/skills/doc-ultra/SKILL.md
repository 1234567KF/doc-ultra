---
name: doc-ultra
description: >-
  Load when user needs to process, optimize, check, expand, or polish formal
  documents — especially bids, patents, software copyright registrations, group
  standards, project applications, and knowledge base articles. Triggers:
  doc-ultra, 文档处理, 文档优化, 文档检查, 文档扩写, 润色文档, 标书,
  专利, 软著, 团标, 申报书, 知识库文档. Do NOT load for code generation,
  code review, API design, configuration files, or database schema design.
metadata:
  pattern: pipeline+reviewer+generator
  domain: document-processing
  recommended_model: pro
  steps: "6"
---
# doc-ultra — 文档超融合处理

## Gotchas

- CLI tool `python -m doc_ultra.cli` may or may not be installed. ALWAYS try CLI first;
  if it fails with "No module named doc_ultra", fall back to manual pipeline.
- All intermediate artifacts MUST go to `.doc-ultra/` directory, never to root.
- Stage3 check loop exits after 3 rounds even if not PASS — mark unresolved issues
  with `[未决]` prefix and present to user for manual decision.
- Stage3 REJECT verdict means "return to Stage2 for re-fusion", NOT "fix in place."
- When user-provided input is a plain idea (no document file), Stage0 MUST still
  produce a 需求规格书 from the idea description before any optimization begins.
- `doc-ultra` ≠ code generator, ≠ API designer, ≠ code reviewer. If a request
  is about writing code, routing configs, or database schemas, decline and suggest
  the appropriate tool instead.
- Memory: after each pipeline run, append a structured log entry to
  `.doc-ultra/memory/pipeline-log.md` (see "Memory" section at bottom).

---

## Gate Conditions (Non-Negotiable)

| Gate | Stage | MUST pass before proceeding |
|------|-------|----------------------------|
| G0 | Stage0→Stage1 | 需求规格书 has all 5 sections (元信息/内容范围/格式约束/附件信息/质量标准) |
| G1 | Stage1→Stage2 | All 3 draft files exist and are non-empty |
| G2 | Stage2→Stage3 | Fused draft file exists and is non-empty |
| G3 | Stage3→Stage4/5 | Check report verdict is PASS |
| G4 | Stage4→Stage5 | Expanded draft exists (skip if no expansion requested) |
| G5 | Stage5→Done | Output file written and final scan passed |

**If any gate fails, DO NOT continue until the problem is resolved.**

---

## Execution Logic

### First: Try CLI (one command)

```bash
python -m doc_ultra.cli <input_file> -p <preset> -o <output_file>
```

If `python -m doc_ultra.cli` succeeds, the entire pipeline runs automatically.
Report results and stop. No manual stages needed.

**If CLI fails** (ModuleNotFoundError), proceed to manual pipeline below.

### Manual Pipeline

Execute stages 0–5 sequentially. **MUST NOT skip any stage** unless explicitly
marked optional. Each stage's output is the next stage's input.

---

#### Stage0 — 需求解析

**Input**: user-provided document file(s) or idea description
**Output**: `.doc-ultra/stage0-requirement-spec.md`

Read the input, extract structured requirements. MUST use the template in
[reference.md §Stage0](reference.md). Write the output file, then verify
Gate G0 (all 5 sections present). If any section is empty or missing, fill
it with best-effort inference and mark as `[推断]`.

#### Stage1 — 多视角并行优化 (3 Agents)

**Input**: stage0 output + original document
**Output**: 3 files: `.doc-ultra/stage1-radical.md`, `.doc-ultra/stage1-conservative.md`, `.doc-ultra/stage1-balanced.md`

Spawn 3 parallel sub-agents. Each agent gets the same input but a different
system prompt for its optimization perspective. Detailed prompts are in
[reference.md §Stage1](reference.md).

**Model routing**: coordinate on **pro**, workers on **flash** (or equivalent
high/economical split). If Agent tool supports `model` parameter, specify it
per agent.

After all 3 agents return, verify Gate G1 (all 3 files exist and non-empty).
If any agent failed, re-run that single perspective before proceeding.

#### Stage2 — 融合合成

**Input**: all 3 stage1 drafts
**Output**: `.doc-ultra/stage2-fused.md`

Use prompt from [reference.md §Stage2](reference.md). Conservative draft as
skeleton, absorb radical's structural improvements and balanced's phrasing
improvements. Use **pro** model (fusion requires reasoning + long context).

Verify Gate G2 (fused file exists and non-empty).

#### Stage3 — 拷问检查循环 ↺

**Input**: stage2 output + stage0 requirement spec
**Output**: `.doc-ultra/stage3-checked.md` + check reports per round

This is the **core quality gate**. Execute as Plan→Build→Verify→Fix loop:

```
Round 1: Check → Report → If FIX: Fix → Round 2
Round 2: Check → Report → If FIX: Fix → Round 3
Round 3: Check → Report → If FIX: mark [未决], output and ask user
Any round: REJECT → return to Stage2
Any round: PASS → break and proceed
```

Check protocol and grading (A/B/C/X) defined in [reference.md §Stage3](reference.md).
Use **pro** model — quality floor cannot be lowered.

Verify Gate G3 (verdict is PASS or [未决] with user notification).

#### Stage4 — 扩写 (Optional)

**Input**: stage3 output
**Output**: `.doc-ultra/stage4-expanded.md`

ONLY execute if user explicitly requested expansion or word count target.
Use prompt from [reference.md §Stage4](reference.md). Use **flash/economical** model.

#### Stage5 — 终审抛光

**Input**: stage3 or stage4 output
**Output**: `output.md` (or user-specified path)

Execute four steps per [reference.md §Stage5](reference.md):
1. AI trace removal
2. MD→doc compatibility
3. Format unification
4. Final scan

Use **flash** model. After writing output, verify Gate G5 (no [TODO], [待补充],
orphaned headings, or obvious grammar errors remain in the output).

---

## Model Routing

| Stage | Quality Sensitivity | Model Tier |
|-------|-------------------|------------|
| Stage0 | High — parsing precision | **pro** |
| Stage1 | Medium — 3× parallel volume | coordinate: **pro**, workers: **flash** |
| Stage2 | High — reasoning + long context | **pro** |
| Stage3 | Critical — quality floor | **pro** |
| Stage4 | Low — volume | **flash/economical** |
| Stage5 | Low — formatting | **flash** |

See [presets.md](presets.md) for per-document-type provider assignments.

---

## Output Template

Final output MUST follow the structure in [assets/output-template.md](assets/output-template.md).
Every required section must appear. Use the template as a skeleton, fill with
the polished content from Stage5.

---

## Report After Completion

After the pipeline finishes (either CLI or manual), MUST report to user:

```markdown
## Pipeline Complete

| Stage | Duration | Status |
|-------|----------|--------|
| Stage0 解析 | Xs | ✓ |
| Stage1 优化 | Xs | ✓ (3/3 agents) |
| Stage2 融合 | Xs | ✓ |
| Stage3 检查 | Xs | ✓ PASS (N rounds) |
| Stage4 扩写 | Xs | skipped / ✓ |
| Stage5 抛光 | Xs | ✓ |

**Output**: `path/to/output.md`
**Preset**: {preset_name} (or auto-detected)
```

---

## Memory

After each pipeline run, log to `.doc-ultra/memory/pipeline-log.md`:

```markdown
### {ISO timestamp}
- input: {file or description}
- preset: {preset name or "auto"}
- stages completed: {list}
- check rounds: {N}
- final verdict: {PASS / 未决-{reason}}
- output: {file path}
```

Before the next run, scan the last 3 log entries. If any show repeated verification
failures for the same document type, tighten the Stage3 criteria for this run.

---

## Supporting Files

| File | When to Load |
|------|-------------|
| [reference.md](reference.md) | Always — contains full stage prompts and protocols |
| [presets.md](presets.md) | When user specifies `-p` or asks for preset recommendations |
| [assets/output-template.md](assets/output-template.md) | Stage5 — output skeleton |
| [evals.md](evals.md) | Only when debugging skill routing issues |
| `doc-ultra.config.yaml` | Stage0 — provider configuration |
