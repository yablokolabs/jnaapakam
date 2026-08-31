# Generation 1 → Generation 2

A resident agent starts on a modest workstation with a small local model. Months
later it moves to much stronger hardware and a much larger model. This walkthrough
carries its identity, memories, decisions and project context across that move —
and *proves* they arrived.

Everything here is vendor-neutral. No product, model, engine or cloud is required,
and jñāpakaṁ depends on none of them. A non-normative appendix at the end names
some real technologies purely to make the shape concrete.

## The setup

| | Generation 1 | Generation 2 |
|---|---|---|
| Machine | modest workstation | substantially stronger host |
| Model | small local model | large local model |
| Runtime | resident agent runtime v1.x | resident agent runtime v2.x |
| Memory | jñāpakaṁ | jñāpakaṁ (restored) |
| Workflows | an external durable execution system | the same system, referenced |

jñāpakaṁ owns exactly one column of that table: the memory. It **references** the
workflow engine and the workspace revision without managing, executing, or
verifying them.

---

## Generation 1 — the agent accumulates a history

The agent runs, works on a project, and stores what it learns:

```bash
export ANTHROPIC_API_KEY=...        # or any other provider; see the README
jnaapakam serve --db ~/agent/memory.db

curl -X POST http://localhost:8889/ingest -H 'Content-Type: application/json' -d '{
  "text": "The team chose PostgreSQL over MySQL for the ledger, because of partial indexes.",
  "source": "design-review"
}'
curl -X POST http://localhost:8889/ingest -H 'Content-Type: application/json' -d '{
  "text": "The deploy target is eu-west and must stay in-region for compliance.",
  "source": "ops"
}'
curl -X POST http://localhost:8889/ingest -H 'Content-Type: application/json' -d '{
  "text": "The user prefers short answers and no filler.",
  "source": "conversation"
}'
```

Record what this generation actually *is*. Every section is optional — a minimal
generation is `{}`, and nothing forces you to disclose your hardware:

```json
// gen1.json
{
  "runtime":      {"framework": "resident-agent", "version": "1.4"},
  "inference":    {"server": "local", "model": "small-9b", "quantization": "q4"},
  "environment":  {"os": "linux", "architecture": "x86_64"},
  "hardware":     {"cpu": "8-core", "ram_gb": 32, "gpu": "consumer", "vram_gb": 12},
  "workspace":    {"vcs": "git", "revision": "6f1c2ab"},
  "capabilities": {"coding": true, "shell": true, "browser": false},
  "external_state": [
    {"type": "durable_execution", "provider": "example-engine",
     "reference": "workflow/ledger-import/run-118", "status": "verified"}
  ]
}
```

> [!WARNING]
> `external_state` entries are references, never credentials. A manifest carrying
> a field named like a secret — or a URI with an embedded password — is refused.

```bash
jnaapakam generation create --db ~/agent/memory.db --label workstation --manifest gen1.json
jnaapakam generation seal 1 --db ~/agent/memory.db --soul-dir ~/agent/soul
```

```
created generation 1 (promoted)
  SOUL.md          sha256:7758b5bb3ffeee4474f56b267eb1d7f5a2a22608404560e8d46b0aeff06c065e
  IDENTITY.md      sha256:e617c9170d13c249ce83b13737bc4966b3c9033022871d7d8d2649306cfa0cb2
  MEMORY.md        sha256:e8c89c6e0133a0eec1bf88706f0bb7719b9c4ba3089014e6d1f93ce70b05696d
  memory_corpus    sha256:e4f0f2c0c317175a788af550ba0944af001c2026e72afd10bd9e3f035a3829dc  (3 records)
sealed generation 1
```

The first generation is promoted on creation: there is nothing before it to
migrate from, and nothing to validate against.

Note what was *not* swept up. `seal` reads only soul filenames from the directory
you name — a stray `.env` sitting beside them is never hashed into the record.

## The move

Take the semantic state with you. Soul files are plain files you already version;
they travel however you version them, and only their digests go in the backup.

```bash
# On the old machine
curl -s http://localhost:8889/backup > backup.json

# On the new machine — a fresh, empty store
curl -X POST http://localhost:8889/restore -H 'Content-Type: application/json' -d @backup.json
```

```json
{"status": "restored", "memories_imported": 3, "consolidations_imported": 0,
 "generations_imported": 1, "agent_id": "urn:jnaapakam:agent:9d689f18…"}
```

The new store had no lineage of its own, so it **adopted** the agent's identity.
A store that already had a lineage would have refused a different agent outright,
importing nothing.

## Generation 2 — record, verify, then promote

```json
// gen2.json — different machine, different model, more capabilities
{
  "runtime":      {"framework": "resident-agent", "version": "2.1"},
  "inference":    {"server": "local", "model": "large-70b", "quantization": "fp8"},
  "environment":  {"os": "linux", "architecture": "x86_64"},
  "hardware":     {"cpu": "64-core", "ram_gb": 256, "gpu": "datacenter", "vram_gb": 96},
  "workspace":    {"vcs": "git", "revision": "6f1c2ab"},
  "capabilities": {"coding": true, "shell": true, "browser": true, "long_context": true},
  "external_state": [
    {"type": "durable_execution", "provider": "example-engine",
     "reference": "workflow/ledger-import/run-118", "status": "verified"}
  ]
}
```

```bash
jnaapakam generation create --db ~/agent/memory.db --parent 1 --label datacenter --manifest gen2.json
jnaapakam generation seal 2 --db ~/agent/memory.db --soul-dir ~/agent/soul
```

The candidate is `staged`. It is not the agent yet, and it does not become the
agent by merely existing. Check it first:

```bash
jnaapakam generation validate 2 --db ~/agent/memory.db --soul-dir ~/agent/soul \
  --probe "PostgreSQL partial indexes" \
  --probe "deploy target eu-west"
```

```
  identity     pass     agent_id urn:jnaapakam:agent:9d689f1832ff4829b6a4899fb622cfe4
  memory       pass     3 records match the sealed corpus digest
  recall       pass     2 recall probes resolved
  soul         pass     3 artifacts match their recorded digests
  context      recorded 1 external references recorded; verifying them is the
                        responsibility of the systems that own them
  behavioral   skipped  no behavioural evaluation supplied
generation 2: continuity verified
```

That covers the eight things worth verifying:

| # | Verified | How |
|---|----------|-----|
| 1 | Stable `agent_id` | `identity` — the generation carries the same URN the store holds |
| 2 | Soul integrity | `soul` — SHA-256 over the exact bytes of each soul file |
| 3 | Memory count and integrity | `memory` — the corpus digest is byte-identical across machines |
| 4 | Historical decisions retrievable | `recall` — the PostgreSQL decision still comes back from search |
| 5 | Project context | `recall` — the eu-west deploy constraint still resolves |
| 6 | Lineage | `generation show 2` reports `ancestry: [1]` |
| 7 | External-state references | `context` — recorded, explicitly *not* verified by jñāpakaṁ |
| 8 | Migration validation | the migration record now reads `validated` |

`validate` exits non-zero on failure, so it works as a gate in a migration script.

Only now:

```bash
jnaapakam generation promote 2 --db ~/agent/memory.db
jnaapakam agent --db ~/agent/memory.db
```

```
agent:       urn:jnaapakam:agent:9d689f1832ff4829b6a4899fb622cfe4
created:     2026-08-31T09:12:44+00:00
current:     generation 2
generations: 2
```

**The identifier is the same one Generation 1 had.** The model, the runtime, the
CPU, the GPU, the RAM and the capability set all changed. The agent did not.

## What changed

```bash
jnaapakam generation diff 1 2 --db ~/agent/memory.db
```

```
Generation 1 -> Generation 2

agent_id:  unchanged  urn:jnaapakam:agent:9d689f1832ff4829b6a4899fb622cfe4

capabilities:
  browser: False -> True
  + long_context: True

hardware:
  cpu: 8-core -> 64-core
  vram_gb: 12 -> 96
  ram_gb: 32 -> 256
  gpu: consumer -> datacenter

inference:
  quantization: q4 -> fp8
  model: small-9b -> large-70b

runtime:
  version: 1.4 -> 2.1

memory:    3 -> 3 records (unchanged)
IDENTITY.md:    unchanged
MEMORY.md:      unchanged
SOUL.md:        unchanged
```

## When it goes wrong

Tampering is detected, not tolerated. Edit a soul file after sealing:

```
  soul         fail     digest mismatch: SOUL.md
generation 2: validation FAILED
```

and `promote` refuses:

```
error: refusing to promote generation 2: its last continuity validation did not pass
```

`--force` exists for the operator who has decided anyway, and writes onto the
migration record that the override was used.

If a promoted generation turns out badly:

```bash
jnaapakam generation rollback 1 --db ~/agent/memory.db
```

Generation 2 keeps its `promoted` status, the migration log gains a `rolled_back`
entry rather than losing the old one, and **not one memory is touched**. Returning
to an older runtime is not a retraction of what the agent learned while running
the newer one.

## Two candidates from one parent

Staging two generations from the same parent is fine and does not corrupt the
lineage:

```bash
jnaapakam generation create --db ~/agent/memory.db --parent 1 --label candidate-a --manifest a.json
jnaapakam generation create --db ~/agent/memory.db --parent 1 --label candidate-b --manifest b.json
jnaapakam generation promote 2 --db ~/agent/memory.db
```

Candidate B stays `staged`, still parented to generation 1, ready to be promoted
later or rejected with a reason. Note that the ids are 2 and 3: a generation id is
a record id, not a depth in the lineage.

---

## What jñāpakaṁ did *not* do

Worth being explicit, because the boundary is the design:

- It did not move a model, download weights, or start an inference server.
- It did not resume a workflow, replay a job, or fire a timer. It recorded a
  *reference* to `workflow/ledger-import/run-118` and reported it as `recorded`,
  never as `verified` — verifying it is that engine's job, not jñāpakaṁ's.
- It did not build an image, provision a host, or run a deployment.
- It did not check out the workspace at `6f1c2ab`. It recorded that revision so
  you can.

Semantic continuity is jñāpakaṁ's. Operational continuity and environmental
reproducibility belong to the systems that already do them well.

And to be clear about the claim being made: this establishes that a system carried
an identity, a memory corpus and a verifiable history across a change of every
underlying part. It is not evidence that two model instances are the same mind,
and nothing here should be read that way.

---

## Appendix — non-normative

The protocol names no vendors and depends on none. For readers who find concrete
names easier to picture, a deployment matching the shape above might use a resident
agent runtime such as **Hermes**, a durable execution system such as **Restate**, an
inference server such as **SGLang**, **vLLM** or **Ollama**, and models such as
**Qwen** on Generation 1 and **DeepSeek** on Generation 2.

None of those appear anywhere in jñāpakaṁ's code, schema, or specification, and
substituting any of them changes nothing about this walkthrough. That is the point:
the manifest is free-form metadata precisely so a generation can describe a stack
this project has never heard of.
