"""Generational continuity policy.

Pure functions over dicts, in the same spirit as `retrieval.py` and
`retention.py`: no database, no LLM, no filesystem, no clock. What the protocol
calls a *generation* is described here only as data — minting and checking an
agent's permanent identifier, validating the metadata a generation may declare,
hashing continuity artifacts, and comparing two generations.

Two boundaries are load-bearing and are enforced here rather than trusted:

* A manifest is **untrusted metadata**. It is never executed, never used to build
  a filesystem path, and never placed in an LLM prompt. It is scanned for
  credentials on the way in, because a continuity record outlives the machine it
  describes and tends to be copied around.
* A digest proves **integrity, not authenticity**. It says the bytes did not
  change; it says nothing about who produced them. Anyone who can write the
  store can write a digest. Signatures are out of scope for v0.3.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid

AGENT_URN_PREFIX = "urn:jnaapakam:agent:"
AGENT_ID_PATTERN = re.compile(r"^urn:jnaapakam:agent:[0-9a-f]{32}$")

DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SUPPORTED_ALGORITHM = "sha256"

# Artifact names are labels, not paths. Anything that could be resolved against a
# filesystem is refused, so a continuity record can never become a file-read oracle.
# The leading character must be alphanumeric, which is what rules out `..`, `.env`
# and `-rf`: this implementation never joins a name to a directory, but a name that
# would traverse if some other implementation did has no business being stored.
ARTIFACT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

CORPUS_ARTIFACT = "memory_corpus"
STATE_ARTIFACT = "memory_state"

# Written into a state digest where a cross-row link points at a memory that is not
# in the corpus. A dangling link and a dropped link are different failures, and
# neither may hash the same as an intact one.
MISSING_LINK = "\x00missing"

# Sections the protocol gives meaning to. Every one is optional: a minimal
# compliant generation declares `{}` and discloses no hardware, model, or
# infrastructure at all. Unknown sections are preserved verbatim so vendors can
# extend the manifest without a protocol revision.
MANIFEST_SECTIONS = ("runtime", "inference", "environment", "hardware", "workspace", "capabilities")

MAX_MANIFEST_BYTES = 16_384
MAX_STRING_BYTES = 2_048
MAX_MANIFEST_DEPTH = 8

# A key whose normalised form is one of these — or ends with one — is refused.
# Matching on the whole normalised key rather than on substrings keeps legitimate
# fields like `tokenizer` and `max_tokens` usable while still catching
# `api_key`, `access_token`, and `private_key`.
CREDENTIAL_KEYS = frozenset(
    {
        "auth",
        "authorization",
        "bearer",
        "credential",
        "credentials",
        "key",
        "passphrase",
        "passwd",
        "password",
        "secret",
        "token",
    }
)

# scheme://user:password@host — credentials smuggled inside a reference URI.
URI_USERINFO_PATTERN = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^/\s@]*:[^/\s@]*@")


# ---- permanent identity ------------------------------------------------


def new_agent_id() -> str:
    """Mint an identifier that outlives every model, host, and generation.

    Random rather than derived: deriving it from a name, host, or model would
    make it change exactly when the protocol promises it will not.
    """
    return AGENT_URN_PREFIX + uuid.uuid4().hex


def is_agent_id(value) -> bool:
    return isinstance(value, str) and bool(AGENT_ID_PATTERN.match(value))


# ---- integrity ---------------------------------------------------------


def digest_bytes(data: bytes) -> str:
    """SHA-256 over the exact bytes given, lowercase hex.

    No normalisation of any kind — no newline translation, no BOM stripping, no
    whitespace trimming. What is hashed is what is on disk, so two
    implementations agree without sharing a text pipeline.
    """
    return hashlib.sha256(data).hexdigest()


def is_digest(value) -> bool:
    return isinstance(value, str) and bool(DIGEST_PATTERN.match(value))


def is_artifact_name(value) -> bool:
    return isinstance(value, str) and bool(ARTIFACT_NAME_PATTERN.match(value))


CONTENT_FIELDS = ("namespace", "source", "kind", "summary", "raw_text", "created_at", "event_time")


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _as_list(value) -> list:
    """Read a JSON-array column that may arrive as text or as an already-parsed list."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    return value if isinstance(value, list) else []


def memory_content_digest(memory: dict) -> str:
    """Hash what a memory *knows*.

    Covers the text, its extracted entities and topics, its provenance and its
    place in time. Entities and topics are included because they are indexed and
    therefore decide what is findable: corrupting them changes what the agent can
    recall even when `raw_text` is untouched.

    Excluded: `id` (a restore may renumber rows), and `access_count` /
    `last_accessed` (a recall is usage, not a change of knowledge). Everything
    about how this memory is currently *interpreted* — validity, archival,
    supersession, links — belongs to the state digest instead.

    Tags are sorted, because reordering extracted tags is not new knowledge.
    `importance` is formatted to fixed decimals rather than dumped as a float, so
    the digest is reproducible outside Python.
    """
    payload = {
        field: ("" if memory.get(field) is None else str(memory.get(field)))
        for field in CONTENT_FIELDS
    }
    payload["importance"] = f"{float(memory.get('importance') or 0.0):.6f}"
    payload["entities"] = sorted(str(e) for e in _as_list(memory.get("entities")))
    payload["topics"] = sorted(str(t) for t in _as_list(memory.get("topics")))
    return digest_bytes(_canonical(payload))


def memory_state_digest(memory: dict, content_by_id: dict) -> str:
    """Hash how a memory is currently *read*.

    Content alone is not continuity. Two stores can hold identical text while
    disagreeing about which memories are still true — and an agent restored into
    the second one believes a fact its predecessor had already retracted. So this
    covers the validity interval, archival, the correction chain, and the
    consolidation graph.

    Cross-row links are hashed as the *content digest of what they point at*,
    never as a row id. That is what lets the digest survive a restore that
    renumbers every row while still noticing that a link vanished: `17 -> 26`
    becoming `3 -> 91` hashes identically, `17 -> nothing` does not.
    """

    def target(reference) -> str:
        if reference is None:
            return ""
        return content_by_id.get(reference, MISSING_LINK)

    payload = {
        "content": content_by_id.get(memory.get("id")) or memory_content_digest(memory),
        "valid_from": str(memory.get("valid_from") or memory.get("created_at") or ""),
        "valid_to": str(memory.get("valid_to") or ""),
        "archived": "1" if memory.get("archived") else "0",
        "superseded_by": target(memory.get("superseded_by")),
        "connections": sorted(
            f"{target(link.get('linked_to'))}\x00{link.get('relationship') or ''}"
            for link in _as_list(memory.get("connections"))
            if isinstance(link, dict)
        ),
    }
    return digest_bytes(_canonical(payload))


def corpus_digests(memories) -> dict:
    """Two digests over a whole corpus, both independent of order and row ids.

    * ``content`` — what knowledge exists
    * ``state``   — how that knowledge is currently interpreted and retrieved

    They are separate because they fail for different reasons and demand different
    responses. A content mismatch means memories were lost or altered. A state
    mismatch means every memory arrived intact but the agent now reads them
    differently — a dropped correction chain being the case that matters most.

    Per-memory digests are sorted before being combined, and duplicates are kept:
    losing one of two identical memories is a real loss.

    Two memories whose content is byte-identical necessarily share a content
    digest, so a link to either hashes the same. That is a deliberate limit and
    the right reading — if two memories say exactly the same thing, pointing at
    one or the other is not a semantic difference.
    """
    memories = list(memories)
    content_by_id = {memory.get("id"): memory_content_digest(memory) for memory in memories}
    contents = sorted(content_by_id[memory.get("id")] for memory in memories)
    states = sorted(memory_state_digest(memory, content_by_id) for memory in memories)
    return {
        "content": digest_bytes("\n".join(contents).encode("utf-8")),
        "state": digest_bytes("\n".join(states).encode("utf-8")),
    }


# ---- manifests ---------------------------------------------------------


def _normalise_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _looks_like_credential(key: str) -> bool:
    normalised = _normalise_key(key)
    return normalised in CREDENTIAL_KEYS or any(
        normalised.endswith(candidate) for candidate in CREDENTIAL_KEYS
    )


def _scan(value, path: str, depth: int) -> None:
    """Reject credentials, unserialisable values, and pathological nesting."""
    if depth > MAX_MANIFEST_DEPTH:
        raise ValueError(f"manifest nests too deeply at {path or 'the top level'}")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"manifest keys must be strings, at {path or 'the top level'}")
            if _looks_like_credential(key):
                raise ValueError(
                    f"refusing a manifest carrying a credential field: {path}{'.' if path else ''}{key}"
                )
            _scan(item, f"{path}.{key}" if path else key, depth + 1)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan(item, f"{path}[{index}]", depth + 1)
    elif isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_STRING_BYTES:
            raise ValueError(f"manifest value at {path} is too long")
        if URI_USERINFO_PATTERN.search(value):
            raise ValueError(f"refusing a reference with embedded credentials at {path}")
    elif not isinstance(value, (int, float, bool)) and value is not None:
        raise ValueError(f"manifest value at {path} is not JSON data")


def validate_manifest(manifest) -> dict:
    """Check a generation manifest and return it, or raise ValueError.

    Shape is only enforced where the protocol depends on it. Everything else is
    accepted and preserved, because the whole point of the manifest is that a
    generation can describe a runtime this version has never heard of.
    """
    if manifest is None:
        return {}
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a JSON object")

    for section in MANIFEST_SECTIONS:
        if section in manifest and not isinstance(manifest[section], dict):
            raise ValueError(f"manifest section '{section}' must be an object")

    external = manifest.get("external_state")
    if external is not None:
        if not isinstance(external, list):
            raise ValueError("manifest section 'external_state' must be an array")
        if any(not isinstance(entry, dict) for entry in external):
            raise ValueError("every 'external_state' entry must be an object")

    _scan(manifest, "", 0)

    try:
        encoded = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError("manifest is not JSON-serialisable") from None
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ValueError(f"manifest exceeds {MAX_MANIFEST_BYTES} bytes")
    return manifest


def validate_artifacts(artifacts) -> list[dict]:
    """Normalise a list of artifact digests supplied by a caller."""
    if not isinstance(artifacts, list):
        raise ValueError("'artifacts' must be an array")
    checked = []
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            raise ValueError(f"artifacts[{index}] must be an object")
        name = artifact.get("name")
        if not is_artifact_name(name):
            raise ValueError(
                f"artifacts[{index}].name must be a plain label, not a path: {name!r}"
            )
        algorithm = artifact.get("algorithm") or SUPPORTED_ALGORITHM
        if algorithm != SUPPORTED_ALGORITHM:
            raise ValueError(f"artifacts[{index}].algorithm must be {SUPPORTED_ALGORITHM}")
        digest = artifact.get("digest")
        if not is_digest(digest):
            raise ValueError(f"artifacts[{index}].digest must be a lowercase hex sha256")
        size = artifact.get("bytes")
        if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size < 0):
            raise ValueError(f"artifacts[{index}].bytes must be a non-negative integer")
        checked.append({"name": name, "algorithm": algorithm, "digest": digest, "bytes": size})
    return checked


# ---- comparison --------------------------------------------------------


def _diff_mapping(before: dict, after: dict) -> dict:
    added = {k: v for k, v in after.items() if k not in before}
    removed = {k: v for k, v in before.items() if k not in after}
    changed = {k: [before[k], after[k]] for k in before.keys() & after.keys() if before[k] != after[k]}
    return {name: value for name, value in
            (("added", added), ("removed", removed), ("changed", changed)) if value}


def diff_manifests(before: dict, after: dict) -> dict:
    """Report what changed between two generations' declared environments.

    Sections absent from both sides are absent from the result, so comparing two
    generations that declared nothing yields nothing rather than a wall of nulls.
    `external_state` is compared separately by `diff_external_state`: it is a list
    of references, not a mapping of fields.
    """
    sections: dict[str, dict] = {}
    for name in sorted((before.keys() | after.keys()) - {"external_state"}):
        left, right = before.get(name), after.get(name)
        if isinstance(left, dict) or isinstance(right, dict):
            difference = _diff_mapping(left or {}, right or {})
            if difference:
                sections[name] = difference
        elif left != right:
            sections[name] = {"changed": {name: [left, right]}}
    return sections


def diff_external_state(before: dict, after: dict) -> dict:
    """Compare external references by identity, not by their recorded status.

    A reference whose status changed from `unverified` to `verified` is the same
    reference; only appearing and disappearing references are reported.
    """

    def keyed(manifest):
        entries = manifest.get("external_state") or []
        return {(entry.get("type"), entry.get("provider"), entry.get("reference")): entry
                for entry in entries if isinstance(entry, dict)}

    left, right = keyed(before), keyed(after)
    added = [right[k] for k in right.keys() - left.keys()]
    removed = [left[k] for k in left.keys() - right.keys()]
    return {name: value for name, value in (("added", added), ("removed", removed)) if value}
