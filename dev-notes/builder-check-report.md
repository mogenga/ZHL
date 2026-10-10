# Builder Check Report

Source: `docs/superpowers/specs/2026-10-10-zhl-system-spec.md`

## Blocking questions (ranked)

### 1. What is "AGH"?
The spec references "AGH Skill" and "AGH Plugin" as the orchestration layer throughout (§3.1, §4 Phase A/B, §7). But nowhere is it defined what AGH actually is — a framework to install, this harness itself, or something else. Concretely:
- Is there a `pyproject.toml` dependency for it?
- What does a Skill file look like (Python class? YAML? Python with a specific interface)?
- What does a Plugin look like?
- How does a Skill invoke a Plugin?

**Why it blocks the plan:** Phase A and Phase B both require building 5 Skills and ~6 Plugins. Without knowing the framework's contract (interfaces, registration, lifecycle, config), I can't plan any orchestration work. The risk of guessing wrong is high — the entire 编排层 would need to be rewritten.

### 2. Which AI model/service backs the two P0 AI features?
P0 requires "structured extraction with confidence" (§6.3) and "evidenced attribution" (§6.3), both of which need an LLM. The spec mentions "model service unavailable" as a degradation case (architecture doc §10) but never specifies:
- Which model provider (OpenAI, local, AGH-native, etc.)?
- How is it configured (env var, config file, AGH setting)?
- Is the model required for the system to start, or can P0 be demoed without it?
- What's the prompt structure for extraction and attribution?

**Why it blocks the plan:** These are two of the three P0 AI features. If I don't know whether the model is external or embedded, the dependency list, the API surface (do skills call a model client?), and the test strategy (mock or real) all change. For a demo-focused P0, knowing whether "structured extraction" can be stubbed with a deterministic parser (CSV → fields with simulated confidence) would let me plan the intake phase without the model dependency.

### 3. How are rule versions created and published?
The spec states `QualityRuleVersion` is immutable after publication and "only published rules can judge" (§2.1), but there's no workflow for:
- Creating a draft rule version
- Publishing it
- What fields a rule version carries (nameplate power, tolerances, current bands, sample plan, etc. are in the requirements doc §3.1 but not re-stated in the spec)
- Is there a REST API for rule CRUD, or are rules seeded via migration?

**Why it blocks the plan:** Phase 0 "测试夹具 + 规则版本种子" requires knowing what a rule version looks like in storage. If rules are seeded via SQL, I need the full field list. If there's a rule management API, it's a separate module not listed in Phase A or B.

### 4. How is a DeliveryBatch created?
The state machine starts at `draft`, but the spec never describes:
- The API or flow to create a batch (set `projectId`, `model`, `ruleVersionId`)
- What happens between `draft` and `pending_intake` (presumably intake files are uploaded, which triggers the transition)
- Whether `plannedQuantity` matters or is just informational

**Why it blocks the plan:** Phase A's intake API is `POST /api/intake` but doesn't specify how it relates to a batch. Does the caller specify a `batchId`? Or does intake create the batch? This changes the API contract.

### 5. What static pages are in scope for P0?
The spec says "内置静态页" in the tech stack, and Phase A/B list "页面" responsibilities (batch list, judgment detail, incident detail, release assertion, report). But:
- Are these full pages, or just API endpoints with a minimal HTML/JS shell?
- Is there a frontend framework (the spec says "zero frontend build"), so presumably plain HTML + JS?
- What's the scope of the "human confirmation queue" page?

**Why it blocks the plan:** A builder needs to know whether to plan a UI layer at all, or just API. The "zero build" constraint suggests plain HTML, but the level of detail (forms, tables, links) affects Phase A/B task sizing.

## Decidable by builder (not blockers)

- **Full DDL schema**: derivable from §2.1 object definitions + architecture doc §4.1 table list. A builder can produce `001_init.sql` from these.
- **Missing API routes** (batch creation, rule CRUD, human confirmation): inferable from the workflow descriptions.
- **ISO sampling table data**: the spec says "预置固定表" — a builder can create a reasonable table with a few inspection types × AQL levels.
- **Evidence file storage path**: config-driven, a builder picks a sensible default.
- **Partial batch judgment** (some components judged, some not): the state machine handles this — a batch stays in `pending_judge` until all components are judged.
