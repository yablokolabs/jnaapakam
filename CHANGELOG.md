# Changelog

All notable changes to jñāpakaṁ are recorded here. The protocol specification is
[PROTOCOL.md](PROTOCOL.md); this file tracks the reference implementation.

## [0.5.0] — 2026-09-02

**Integrity is not authenticity, and words are not meaning.** v0.4 could prove a
corpus had not drifted. It could not prove who sealed it, could not retire a memory
that had simply stopped being used, and could only find a memory by the words it
happened to contain.

### Added

- **Age-based retention policy.** `POST /prune` accepts `older_than_days` alongside
  `keep`, and `Store.expire()` archives memories neither created nor recalled within
  the window. The two policies answer different questions — `keep` bounds how much a
  namespace holds, `older_than_days` retires what stopped being used — so a namespace
  under its cap can still be full of memories nobody has read in a year. Both may be
  given; the age policy runs first.
- **Scheduled expiry.** `MEMORY_EXPIRE_AFTER_DAYS` / `--expire-after` applies the age
  policy hourly, starting at boot. Off unless configured.
- Protocol §8 gains the age policy, its recall-resets-the-clock rule, and a MAY for
  applying it on a schedule.
- **Signed continuity records.** A seal can now be signed with an Ed25519 key
  (`MEMORY_SIGNING_KEY`), and validation gains an eighth check, `signature`. The
  digests of v0.4 give a seal integrity; they cannot give it authenticity, because
  whoever can write the store can recompute them. A signature is what separates a
  corpus that survived from one that was replaced and resealed.
- Signing is an optional extra: `pip install jnaapakam[signing]`. The base install
  stays at one dependency, an unsigned seal does not fail validation, and a seal
  that cannot be verified reports `skipped` — unverifiable is not verified.
- `--public-key` on `generation validate` (and `public_key` on
  `POST /generations/validate`) checks provenance rather than self-consistency:
  verifying against the key recorded beside the signature only proves an impostor
  was internally consistent.
- Protocol §10.7 defines seal signatures, including the rule that the signed
  statement must bind the agent, generation and artifact — signing the digest alone
  leaves a signature that can be lifted onto another generation's seal.

- **Operator dashboard** at `GET /dashboard`: counts, namespace filter, recent
  memories and search, as one dependency-free HTML file with no build step. Served
  on local binds only — on a public bind it is a 404 even with a valid token,
  because exposing the API should not also publish a browsable window onto the
  agent's memory. It reads only documented endpoints and never writes.

- **Optional semantic retrieval.** With `MEMORY_EMBEDDING_MODEL` set, memories are
  embedded at ingest and semantically similar ones are **added to** the candidate
  pool rather than merely reordering it — re-ranking BM25 hits could never surface
  the memory that shares no words with the query, which is the only reason to run an
  embedding model. Relevance becomes `(1 - w)·lexical + w·semantic`.
- Runtime-detected, not merely configured: without numpy the capability reports off
  and retrieval stays lexical, with a warning, rather than a configured feature that
  silently never runs. `pip install jnaapakam[embeddings]`.
- `POST /embed` backfills memories stored before the model was configured, and
  `/status` reports embedding coverage — a half-embedded corpus answers half its
  queries lexically, which a feature flag cannot tell an operator.
- An embedding failure never costs a memory: ingest still stores it and lexical
  search still finds it.
- Protocol §5 gains the optional semantic term, the rule that it must widen the
  candidate set rather than reorder it, and the requirement to fall back to lexical
  ranking rather than fail.

### Changed — breaking only for a client asserting the exact string

- Protocol version reported by `/status` and `/backup` becomes `"0.5"`. No digest
  rule changed: a v0.4 generation validates unchanged, with `signature` reporting
  `skipped`. The bump is because §5, §8 and §10.5 gained normative rules, and two
  implementations both claiming "0.4" would now disagree about what `/prune` accepts
  and what a `signature` check means.
- `/status`, `/health` and the MCP server info now report the installed package
  version instead of a hardcoded `0.4.0` that went stale at the last release.
- Schema version 6: a `memory_embeddings` table, storing the model with each vector
  so vectors from different models are never compared.
- Schema version 5: `generation_artifacts` gains `signature` and `public_key`.
  Additive — seals written before signing existed read back as unsigned.

The age clock is `last_accessed` falling back to `created_at`, so recall keeps a
memory alive regardless of age. Importance deliberately does not: it is assigned once
at ingest and never revised, so it cannot notice that a memory stopped mattering.
Expiry archives and never deletes, so `/archive` with `restore` undoes it.

## [0.4.1] — 2026-09-01

Three fixes to the self-hosted / OpenAI-compatible path (`LLM_BASE_URL`), which
was documented as supported but only half-wired.

### Fixed

- **The cloud fallback no longer fires when `LLM_BASE_URL` is set.** A failure at
  the custom endpoint was caught and silently retried against Anthropic or OpenAI
  whenever their key happened to be in the environment — sending the memory content
  to the provider the operator had chosen not to use. Self-hosting is a decision
  about where the content is allowed to go; a transient local error must not undo
  it. The failure is now reported as-is.
- **Model aliases are no longer expanded against a custom endpoint.** `haiku` was
  rewritten to `claude-haiku-4-5` before being posted to Ollama, which does not
  serve it. The model string is now passed through exactly as written, so proxy and
  local model names work; leaving the model unset (`default`) raises a message
  naming `MEMORY_MODEL` instead of 404ing on every ingest.
- **`LLM_BASE_URL` is declared in `mcpize.yaml`.** Only `LLM_API_KEY` was exposed,
  so a hosted subscriber could supply the key but never the address.

## [0.4.0] — 2026-08-31

**Content is not continuity.** v0.3 verified that an agent's memories survived a
migration. It did not verify that the agent still *read* them the same way.

A migration could carry every memory across intact and still drop the correction
chain: *"the preferred database is PostgreSQL"*, superseded by *"the user switched
to ClickHouse"*, arrives as two live and contradictory memories. Every byte of text
is present, the v0.3 corpus digest is byte-identical, and the agent is wrong.

### Added

- **Semantic state digest** — covers each memory's validity interval, archival
  flag, correction chain and consolidation links, alongside the existing content
  digest. Sealing records both; `/backup` carries both.
- **`semantic_state`** — a seventh continuity check, reported separately from
  `memory`. The two mean different things: `memory` says knowledge was lost or
  altered, `semantic_state` says it all arrived and is now interpreted
  differently. Collapsing them would tell an operator something is wrong without
  saying what.
- `Store.corpus_digests()` returning both digests; `corpus_state_digest` in the
  backup payload; a `memory_state` artifact recorded at seal time.

### Changed — breaking, and why it is a minor bump

- **The content digest now covers `entities` and `topics`.** They are indexed, so
  corrupting them changes what the agent can recall even when `raw_text` is
  untouched. Their omission in v0.3 was an oversight, not a tradeoff — it was never
  documented as deliberate.
- **Cross-row references are hashed by the target's content digest**, not its row
  id. `17 → 26` and `3 → 91` agree after a restore renumbers everything, while
  `17 → nothing` does not.
- The digest rules in PROTOCOL.md §10.6 are normative, so a v0.3 and a v0.4
  implementation compute different digests for the same corpus. A generation
  sealed under v0.3 will not verify under v0.4. That is an interoperability break,
  and shipping it as a patch would leave two implementations silently disagreeing
  about whether an agent's continuity held.

### Compatibility

- A v0.3 generation carries no `memory_state` artifact, so that check reports
  `skipped` rather than failing.
- Databases, backups and every endpoint are otherwise unchanged. v0.2 backups
  still restore; v0.2 databases still upgrade in place.
- **Re-seal generations created under v0.3** (`jnaapakam generation seal <id>`) so
  their digests are recomputed under the v0.4 rules.

## [0.3.0] — 2026-08-31

**Generational continuity.** An agent's model, runtime, tools, hardware and
capabilities may all change without changing its continuity identity.

This is a claim about systems, not minds: jñāpakaṁ defines a stable identifier, a
verifiable memory corpus, an auditable lineage and migration provenance. It makes
no claim about consciousness or subjective identity.

### Added

- **Permanent agent identity** — `urn:jnaapakam:agent:<32 hex>`, minted once and
  independent of the agent's display name, model, provider, runtime, host,
  hardware, OS and generation number. Opening an existing database mints one
  automatically.
- **Generations** — a portable record of the runtime an agent ran as, with an
  optional manifest describing runtime, inference, environment, hardware,
  workspace revision, capabilities and external state. Every section is optional;
  a minimal generation is `{}`. Unknown sections are preserved verbatim.
- **Branch-capable lineage** — generations name a parent, so two candidates staged
  from one parent do not corrupt each other's ancestry.
- **Migration records** — provenance for every transition, with statuses `staged`,
  `validated`, `failed`, `promoted`, `rejected` and `rolled_back`.
- **Continuity validation** — six checks (`identity`, `memory`, `recall`, `soul`,
  `context`, `behavioral`), each reporting its own result. A check that was not
  requested reports `skipped`, never `pass`.
- **Integrity metadata** — SHA-256 over the exact bytes of soul files, plus an
  order-independent digest over the memory corpus that survives a restore
  renumbering every row.
- **Endpoints** — `GET /agent`, `GET /generations`, `GET /generations/diff`,
  `GET /migrations`; `POST /generations`, `/generations/artifacts`,
  `/generations/validate`, `/generations/promote`, `/generations/reject`,
  `/generations/rollback`. All behind the existing bearer authentication.
- **MCP tools** — `get_agent_identity`, `list_generations`, `diff_generations`.
  Read-only: deciding which runtime *is* the agent stays an operator action, the
  same reason `/clear` and `/restore` are not MCP tools.
- **CLI** — `jnaapakam agent` and `jnaapakam generation {list,show,create,seal,
  validate,promote,reject,rollback,diff}`. These open the database directly, so
  continuity works offline with no server, token or network. `validate` exits
  non-zero on failure, which makes it usable as a migration gate.
- **Schema** — `meta`, `generations`, `migrations` and `generation_artifacts`
  tables; `PRAGMA user_version` 3 → 4.
- **Example** — [examples/generational-continuity](examples/generational-continuity/),
  a vendor-neutral Generation 1 → Generation 2 walkthrough.

### Changed

- `/backup` now carries `agent_id`, `current_generation`, `corpus_digest`,
  `generations`, `migrations` and `artifacts`. Soul files remain excluded, exactly
  as in v0.2 — only their digests travel.
- `/restore` adopts the backup's `agent_id` when the target store has no lineage
  of its own, and refuses a different agent when it does. Re-importing an agent's
  own backup no longer forks its lineage.
- `version` reported by `/status` and `/backup` is now `"0.3"`; the server and MCP
  `serverInfo` report `0.3.0`.
- The full-text index backfill is gated on `user_version < 3` rather than on the
  current schema version, so future schema bumps no longer trigger a pointless
  reindex.

### Security

- Generation manifests and external-state references are treated as untrusted
  metadata: never executed, never dereferenced, never used to build a filesystem
  path, and never placed in an LLM prompt.
- Manifests carrying a field named like a credential, or a reference URI with
  embedded userinfo, are refused. Manifest size and nesting depth are bounded.
- The server never reads a file to hash it. Digests arrive precomputed; the CLI
  does the reading, locally, restricted to soul filenames in a directory the
  operator names. An endpoint that hashes a caller-supplied path would be an
  arbitrary-file-read oracle.
- Artifact names must be plain labels and are rejected if they could be resolved
  against a filesystem.
- `/restore` validates the entire payload, including manifests and digests, before
  mutating anything.

### Not in this release

- **Signatures.** v0.3 provides integrity, not authenticity: a digest proves the
  bytes did not change, but anyone who can write the store can write a digest.
  Do not read one as the other.

### Compatibility

- Every v0.2 test passes unchanged.
- A v0.2 database opens and upgrades in place, gaining an identity without losing
  a memory.
- A v0.2 backup still restores.
- A v0.3 backup restored by a v0.2 implementation imports its memories and
  consolidations and ignores the continuity keys — the lineage is dropped, not
  corrupted.
- The only breaking change is the `version` string, and only for a client
  asserting it exactly.

## [0.2.0]

Retrieval as a protocol operation (`/search`, ranking requirements); enforceable
namespaces; memory correction by supersession; gated contradiction detection and
soft forgetting; bearer authentication and safe bind defaults; error semantics;
corrected default port and `/backup` payload.

## [0.1.0]

Initial protocol specification and reference implementation.
