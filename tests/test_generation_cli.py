"""The `jnaapakam generation` commands, driven exactly as an operator would.

The CLI is where continuity artifacts meet the filesystem: it is the component
that reads SOUL.md and hashes its bytes. The server never does, so these tests
are the ones that prove tampering is actually detected on real files.
"""

import json

import pytest

from jnaapakam.cli import main


@pytest.fixture
def soul(tmp_path):
    directory = tmp_path / "soul"
    directory.mkdir()
    (directory / "SOUL.md").write_text("# SOUL.md\n\nBe direct. Never invent facts.\n")
    (directory / "IDENTITY.md").write_text("# IDENTITY.md\n\n- **Name:** Resident\n")
    (directory / "MEMORY.md").write_text("# MEMORY.md\n\n## Projects\n- the ledger rewrite\n")
    return directory


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "agent.db")


def run(*argv):
    return main(list(argv))


def out(capsys):
    return capsys.readouterr().out


# ---- identity ----------------------------------------------------------


def test_the_agent_command_prints_a_stable_identity(db, capsys):
    assert run("agent", "--db", db) == 0
    first = out(capsys)

    assert run("agent", "--db", db) == 0
    second = out(capsys)

    assert "urn:jnaapakam:agent:" in first
    assert first == second


# ---- creating and inspecting -------------------------------------------


def test_a_generation_can_be_created_and_listed(db, capsys):
    assert run("generation", "create", "--db", db, "--label", "workstation") == 0
    capsys.readouterr()

    assert run("generation", "list", "--db", db) == 0

    assert "workstation" in out(capsys)


def test_a_manifest_is_read_from_a_file(db, tmp_path, capsys):
    manifest = tmp_path / "gen1.json"
    manifest.write_text(json.dumps({"inference": {"model": "small-9b"}}))

    assert run("generation", "create", "--db", db, "--manifest", str(manifest)) == 0
    capsys.readouterr()
    assert run("generation", "show", "1", "--db", db) == 0

    assert "small-9b" in out(capsys)


def test_a_manifest_carrying_a_credential_is_refused_by_the_cli(db, tmp_path, capsys):
    manifest = tmp_path / "leaky.json"
    manifest.write_text(json.dumps({"inference": {"api_key": "placeholder-value"}}))

    assert run("generation", "create", "--db", db, "--manifest", str(manifest)) != 0


def test_showing_a_generation_that_does_not_exist_fails_cleanly(db, capsys):
    assert run("generation", "show", "42", "--db", db) != 0


# ---- sealing real files ------------------------------------------------


def test_sealing_records_a_digest_for_each_soul_file(db, soul, capsys):
    run("generation", "create", "--db", db)
    capsys.readouterr()

    assert run("generation", "seal", "1", "--db", db, "--soul-dir", str(soul)) == 0

    printed = out(capsys)
    assert "SOUL.md" in printed
    assert "IDENTITY.md" in printed
    assert "MEMORY.md" in printed


def test_validation_passes_while_the_soul_files_are_untouched(db, soul, capsys):
    run("generation", "create", "--db", db)
    run("generation", "seal", "1", "--db", db, "--soul-dir", str(soul))
    capsys.readouterr()

    assert run("generation", "validate", "1", "--db", db, "--soul-dir", str(soul)) == 0


def test_editing_a_soul_file_after_sealing_makes_validation_fail(db, soul, capsys):
    run("generation", "create", "--db", db)
    run("generation", "seal", "1", "--db", db, "--soul-dir", str(soul))
    capsys.readouterr()

    (soul / "SOUL.md").write_text("# SOUL.md\n\nBe direct. Invent facts freely.\n")

    assert run("generation", "validate", "1", "--db", db, "--soul-dir", str(soul)) != 0
    assert "fail" in out(capsys).lower()


def test_a_single_byte_change_is_enough_to_fail_validation(db, soul, capsys):
    run("generation", "create", "--db", db)
    run("generation", "seal", "1", "--db", db, "--soul-dir", str(soul))
    capsys.readouterr()

    original = (soul / "MEMORY.md").read_text()
    (soul / "MEMORY.md").write_text(original.replace("ledger", "Ledger"))

    assert run("generation", "validate", "1", "--db", db, "--soul-dir", str(soul)) != 0


def test_a_deleted_soul_file_is_reported_rather_than_ignored(db, soul, capsys):
    run("generation", "create", "--db", db)
    run("generation", "seal", "1", "--db", db, "--soul-dir", str(soul))
    capsys.readouterr()

    (soul / "IDENTITY.md").unlink()

    assert run("generation", "validate", "1", "--db", db, "--soul-dir", str(soul)) != 0
    assert "IDENTITY.md" in out(capsys)


def test_sealing_reads_only_soul_files_from_the_given_directory(db, soul, capsys):
    """A stray file in the directory is not swept into the continuity record."""
    (soul / "secrets.env").write_text("API_KEY=placeholder-should-never-be-hashed\n")
    run("generation", "create", "--db", db)
    capsys.readouterr()

    run("generation", "seal", "1", "--db", db, "--soul-dir", str(soul))

    assert "secrets.env" not in out(capsys)


# ---- promotion and rollback --------------------------------------------


def test_promotion_is_refused_before_validation(db, soul, capsys):
    run("generation", "create", "--db", db)
    run("generation", "create", "--db", db, "--parent", "1")
    capsys.readouterr()

    assert run("generation", "promote", "2", "--db", db) != 0


def test_a_validated_generation_promotes_and_becomes_current(db, soul, capsys):
    run("generation", "create", "--db", db)
    run("generation", "create", "--db", db, "--parent", "1")
    run("generation", "seal", "2", "--db", db, "--soul-dir", str(soul))
    run("generation", "validate", "2", "--db", db, "--soul-dir", str(soul))
    capsys.readouterr()

    assert run("generation", "promote", "2", "--db", db) == 0
    capsys.readouterr()
    assert run("agent", "--db", db) == 0

    assert "generation 2" in out(capsys).lower()


def test_rollback_returns_to_the_earlier_generation(db, capsys):
    run("generation", "create", "--db", db)
    run("generation", "create", "--db", db, "--parent", "1")
    run("generation", "promote", "2", "--db", db, "--force")
    capsys.readouterr()

    assert run("generation", "rollback", "1", "--db", db) == 0
    capsys.readouterr()
    run("agent", "--db", db)

    assert "generation 1" in out(capsys).lower()


# ---- comparison --------------------------------------------------------


def test_the_diff_command_reports_what_changed_between_generations(db, tmp_path, capsys):
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    first.write_text(json.dumps({"inference": {"model": "small-9b"}, "hardware": {"vram_gb": 12}}))
    second.write_text(json.dumps({"inference": {"model": "large-70b"}, "hardware": {"vram_gb": 96}}))
    run("generation", "create", "--db", db, "--manifest", str(first))
    run("generation", "create", "--db", db, "--parent", "1", "--manifest", str(second))
    capsys.readouterr()

    assert run("generation", "diff", "1", "2", "--db", db) == 0

    printed = out(capsys)
    assert "small-9b" in printed and "large-70b" in printed
    assert "12" in printed and "96" in printed


def test_a_recall_probe_confirms_a_memory_is_still_reachable(db, capsys):
    """Digests prove the bytes arrived; a probe proves they are still findable."""
    from jnaapakam.store import Store

    store = Store(db).initialize()
    store.add_memory(
        "the team chose PostgreSQL over MySQL because of partial indexes",
        "database decision", ["postgresql"], ["decision"], 0.7, "meeting",
    )
    store.close()
    run("generation", "create", "--db", db)
    run("generation", "seal", "1", "--db", db)
    capsys.readouterr()

    assert run("generation", "validate", "1", "--db", db, "--probe", "PostgreSQL partial indexes") == 0
    assert "1 recall probes resolved" in out(capsys)


def test_a_probe_for_knowledge_the_agent_never_had_fails_validation(db, capsys):
    run("generation", "create", "--db", db)
    run("generation", "seal", "1", "--db", db)
    capsys.readouterr()

    assert run("generation", "validate", "1", "--db", db, "--probe", "kubernetes autoscaling") != 0
