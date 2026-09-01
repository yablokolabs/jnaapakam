"""Ed25519 signatures over continuity seals.

Digests give a seal integrity: they detect a corpus that changed after it was
sealed. They give it no authenticity — anyone who can write the database can
recompute every digest and leave a self-consistent record of an agent that never
existed. A signature is what turns the seal from an assertion into evidence.

Optional by design: `pip install jnaapakam[signing]`. Everything here degrades to
"cannot verify", never to "verified" — an installation without the dependency
reports `skipped`, because a check that passes when nothing was checked is the
exact failure the continuity record exists to prevent.

The private key is read from disk per operation rather than held on the Store.
Seals are rare, Ed25519 is microseconds, and key material that is not resident
cannot be read out of a long-lived process.
"""

from __future__ import annotations

import hashlib

ALGORITHM = "ed25519"

INSTALL_HINT = "signature support is not installed: pip install jnaapakam[signing]"


class SigningUnavailable(RuntimeError):
    """The optional signing dependency is not installed."""


def _ed25519():
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised by installs without the extra
        raise SigningUnavailable(INSTALL_HINT) from exc
    return ed25519


def available() -> bool:
    """True when seals can be signed and verified on this installation."""
    try:
        _ed25519()
    except SigningUnavailable:
        return False
    return True


def _load_private(path: str):
    from cryptography.hazmat.primitives import serialization

    _ed25519()
    with open(path, "rb") as handle:
        data = handle.read()
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except ValueError:
        # Also accept a bare 32-byte seed, hex or raw, so a key can be minted
        # without a PEM toolchain.
        seed = bytes.fromhex(data.decode().strip()) if len(data.strip()) == 64 else data.strip()
        key = _ed25519().Ed25519PrivateKey.from_private_bytes(seed)
    if not isinstance(key, _ed25519().Ed25519PrivateKey):
        raise ValueError(f"{path} is not an Ed25519 private key")
    return key


def public_key_hex(private_key_path: str) -> str:
    """The public half of a private key, as the hex recorded beside a signature."""
    from cryptography.hazmat.primitives import serialization

    return (
        _load_private(private_key_path)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
        .hex()
    )


def sign(statement: bytes, private_key_path: str) -> dict:
    """Sign canonical statement bytes. Returns the fields recorded with the seal."""
    from cryptography.hazmat.primitives import serialization

    key = _load_private(private_key_path)
    return {
        "algorithm": ALGORITHM,
        "signature": key.sign(statement).hex(),
        "public_key": key.public_key()
        .public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
        .hex(),
    }


def verify(statement: bytes, signature: str, public_key: str) -> bool:
    """True only if `signature` is this key's signature over exactly these bytes."""
    from cryptography.exceptions import InvalidSignature

    ed25519 = _ed25519()
    try:
        key = ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key))
        key.verify(bytes.fromhex(signature), statement)
    except (InvalidSignature, ValueError):
        return False
    return True


def fingerprint(public_key: str) -> str:
    """A short, stable label for a key, for saying *which* key sealed something."""
    return hashlib.sha256(bytes.fromhex(public_key)).hexdigest()[:16]
