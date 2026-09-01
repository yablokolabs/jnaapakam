"""Signed continuity records.

v0.4 gives a seal integrity: the digests prove the corpus was not altered *by
accident*. They prove nothing about who sealed it — anyone who can write the
database can recompute every digest and leave a self-consistent lie. A signature
is what makes a seal evidence rather than an assertion.

Signing is optional (`pip install jnaapakam[signing]`), so the checks here also
pin the behaviour when it is absent: unverifiable must report `skipped`, never
`pass`.
"""

import pytest

from jnaapakam import signing
from jnaapakam.store import Store

pytestmark = pytest.mark.skipif(not signing.available(), reason="cryptography not installed")


@pytest.fixture
def key_path(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    path = tmp_path / "sealing.key"
    path.write_bytes(
        ed25519.Ed25519PrivateKey.generate().private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return str(path)


@pytest.fixture
def other_key_path(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    path = tmp_path / "someone-else.key"
    path.write_bytes(
        ed25519.Ed25519PrivateKey.generate().private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return str(path)


@pytest.fixture
def signing_store(tmp_path, key_path):
    store = Store(str(tmp_path / "signed.db"), signing_key=key_path).initialize()
    store.add_memory(
        raw_text="the deploy runbook lives in the ops repo",
        summary="deploy runbook location",
        entities=["ops"],
        topics=["deploy"],
        importance=0.8,
        source="test",
    )
    yield store
    store.close()


def _seal(store):
    generation = store.create_generation(label="signed")
    store.seal_corpus(generation["id"])
    return generation["id"]


# ---- signing ------------------------------------------------------------


def test_a_seal_made_with_a_key_carries_a_signature(signing_store):
    generation = _seal(signing_store)

    artifacts = signing_store.artifacts(generation)

    assert artifacts, "sealing records artifacts"
    assert all(a["signature"] and a["public_key"] for a in artifacts)


def test_a_seal_made_without_a_key_is_left_unsigned(store):
    generation = _seal(store)

    assert all(a["signature"] is None for a in store.artifacts(generation))


def test_a_signature_verifies_against_the_sealed_digests(signing_store):
    generation = _seal(signing_store)

    check = signing_store.validate_continuity(generation)["checks"]["signature"]

    assert check["status"] == "pass"


def test_an_unsigned_seal_reports_skipped_rather_than_pass(store):
    """A validation that passes because nothing was checked is the failure to avoid."""
    generation = _seal(store)

    check = store.validate_continuity(generation)["checks"]["signature"]

    assert check["status"] == "skipped"


# ---- what the signature is for ------------------------------------------


def test_an_altered_digest_breaks_the_signature(signing_store):
    """The tamper the digests alone cannot catch: rewriting the seal to match."""
    generation = _seal(signing_store)
    signing_store.db.execute(
        "UPDATE generation_artifacts SET digest = ? WHERE generation_id = ?",
        ("0" * 64, generation),
    )
    signing_store.db.commit()

    check = signing_store.validate_continuity(generation)["checks"]["signature"]

    assert check["status"] == "fail"


def test_a_signature_cannot_be_replayed_onto_another_generation(signing_store):
    """The statement binds the generation, so a valid seal is not portable."""
    first = _seal(signing_store)
    second = signing_store.create_generation(parent=first, label="second")["id"]
    signing_store.seal_corpus(second)
    stolen = signing_store.artifacts(first)[0]
    signing_store.db.execute(
        "UPDATE generation_artifacts SET signature = ?, public_key = ?, recorded_at = ? "
        "WHERE generation_id = ? AND name = ?",
        (stolen["signature"], stolen["public_key"], stolen["recorded_at"], second, stolen["name"]),
    )
    signing_store.db.commit()

    check = signing_store.validate_continuity(second)["checks"]["signature"]

    assert check["status"] == "fail"


def test_a_seal_signed_by_an_unexpected_key_is_rejected(tmp_path, key_path, other_key_path):
    """Verifying against the key recorded beside the signature proves only self-consistency."""
    store = Store(str(tmp_path / "impostor.db"), signing_key=other_key_path).initialize()
    generation = _seal(store)
    expected = signing.public_key_hex(key_path)

    check = store.validate_continuity(generation, public_key=expected)["checks"]["signature"]
    store.close()

    assert check["status"] == "fail"


def test_the_expected_key_accepts_the_seal_it_actually_made(signing_store, key_path):
    generation = _seal(signing_store)
    expected = signing.public_key_hex(key_path)

    check = signing_store.validate_continuity(generation, public_key=expected)["checks"]["signature"]

    assert check["status"] == "pass"


def test_the_check_names_the_key_that_sealed_it(signing_store, key_path):
    generation = _seal(signing_store)

    check = signing_store.validate_continuity(generation)["checks"]["signature"]

    assert signing.fingerprint(signing.public_key_hex(key_path)) in check["detail"]


# ---- soul artifacts are covered too -------------------------------------


def test_recorded_soul_digests_are_signed_as_well(signing_store):
    generation = signing_store.create_generation(label="signed")["id"]
    signing_store.record_artifacts(
        generation, [{"name": "SOUL.md", "algorithm": "sha256", "digest": "a" * 64, "bytes": 10}]
    )

    recorded = {a["name"]: a for a in signing_store.artifacts(generation)}

    assert recorded["SOUL.md"]["signature"]
