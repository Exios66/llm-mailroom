"""The audit viewer is read-only, authenticated, bounded and honestly redacted.

Covered here:

* bearer authorization on both audit routes;
* an absent database is reported, never created;
* hostile document ids are rejected without leaking paths;
* event-chain verification (verified / tampered) using ``schemas.audit``;
* unified-row verification: main row + addenda with the shared v2 primitive,
  attempt sub-rows only with the foundation hasher, honest ``unverified`` when
  that hasher is absent, and a failure when a hash actually mismatches;
* a stored non-object ``detail`` (null/list/scalar) fails per scope instead of
  being coerced to ``{}`` and verifying; padded or truncated hashed columns
  fail too, while legitimate padded/long actors and UTC timestamps verify;
* migrated legacy events are surfaced separately;
* a document with a valid legacy section but orphan attempt/addendum sub-rows
  is failed, not verified;
* an unreadable migration marker makes the schema ambiguous: never verified and
  never guessed back into the event chain;
* bounded reads never claim ``verified``;
* the projection never returns raw detail values;
* the CLI has the same semantics and exit codes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SEED_TEXT = "SYNTHETIC-DEMO-DOCUMENT-TEXT-NOT-FOR-DISPLAY"
SEED_NOTE = "SYNTHETIC-OPERATOR-NOTE-NOT-FOR-DISPLAY"
DOC = "demo-contract-001"
LEGACY_DOC = "demo-claim-003"


def test_event_truncation_does_not_hide_a_failed_legacy_chain(client, make_store, monkeypatch):
    from mailroom_diagnostics import audit

    path = make_store("event-chain")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE audit_log_legacy AS SELECT * FROM audit_log WHERE doc_id=? AND seq=1", (DOC,))
        conn.execute("UPDATE audit_log_legacy SET actor='tampered'")
    monkeypatch.setattr(audit, "MAX_EVENTS", 2)
    verdict = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()["verification"]
    assert verdict["truncated"] is True
    assert verdict["status"] == "failed"
    assert any(failure["scope"] == "legacy" for failure in verdict["failures"])


@pytest.fixture
def auth_headers(monkeypatch):
    monkeypatch.setenv("MAILROOM_API_TOKEN", "audit-token")
    return {"Authorization": "Bearer audit-token"}


@pytest.fixture
def client(auth_headers):
    from mailroom_diagnostics.app import create_app

    return TestClient(create_app(), headers=auth_headers)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    base = tmp_path / "base"
    base.mkdir()
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(base))
    return base


@pytest.fixture
def make_store(tmp_path, monkeypatch):
    """Seed a synthetic store in an explicit temp output dir and point the reader at it."""

    def build(schema: str = "event-chain") -> Path:
        from mailroom_diagnostics.seed_audit_demo import seed

        demo = tmp_path / f"demo-{schema}"
        db = demo / "mailroom.db"
        seed(db, schema=schema)
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")
        return db

    return build


def _exec(db: Path, sql: str, params: tuple = ()) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def test_audit_routes_require_the_bearer_token(client, monkeypatch):
    from mailroom_diagnostics.app import create_app

    anonymous = TestClient(create_app())
    assert anonymous.get("/v1/diagnostics/audit/documents").status_code == 401
    assert anonymous.get(f"/v1/diagnostics/audit/documents/{DOC}").status_code == 401
    assert client.get("/v1/diagnostics/audit/documents").status_code == 200


def test_absent_database_is_reported_and_never_created(client, store):
    response = client.get(f"/v1/diagnostics/audit/documents/{DOC}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["store"]["state"] == "unavailable"
    assert payload["store"]["reason"] == "database-missing"
    assert payload["record"]["state"] == "unavailable"
    assert payload["verification"]["status"] == "unavailable"
    assert not (store / "mailroom.db").exists(), "reading diagnostics must not create a database"

    listing = client.get("/v1/diagnostics/audit/documents")
    assert listing.status_code == 200
    assert listing.json()["store"]["reason"] == "database-missing"
    assert listing.json()["documents"] == []
    assert not (store / "mailroom.db").exists()


def test_store_summary_never_contains_a_path(client, make_store, store):
    make_store()
    text = client.get(f"/v1/diagnostics/audit/documents/{DOC}").text
    assert str(store) not in text
    assert "mailroom.db" not in text


@pytest.mark.parametrize(
    "doc_id",
    ["..%2Fsecret", "a%2Fb", "%2E%2E", "x" * 129, "%00", " leading", "trailing "],
)
def test_hostile_document_ids_are_rejected(client, store, doc_id):
    response = client.get(f"/v1/diagnostics/audit/documents/{doc_id}")
    assert response.status_code in (404, 422)
    assert "mailroom.db" not in response.text


def test_event_chain_is_verified_and_redacted(client, make_store):
    db = make_store("event-chain")
    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    response_text = json.dumps(payload)

    assert payload["store"]["schema"] == "event-chain"
    assert payload["record"]["state"] == "event-chain"
    assert payload["record"]["entries"] == 4
    assert payload["record"]["status"] == "archived"
    assert payload["verification"]["status"] == "verified"
    assert payload["verification"]["method"] == "schemas.audit.verify_chain"
    assert payload["verification"]["checked"]["main"] == 4
    assert [event["event"] for event in payload["record"]["events"]][-1] == "archived"
    assert payload["record"]["events"][0]["prev_hash"] in (None, "")

    # Decision field *names* are useful; their values are never served.
    assert "document_text" in payload["record"]["events"][0]["decision_fields"]
    assert SEED_TEXT not in response_text
    assert SEED_NOTE not in response_text
    assert "synthetic-demo.pdf" not in response_text
    assert str(db) not in response_text

    assert payload["legacy"]["state"] == "absent"


def test_tampered_event_chain_is_failed_not_verified(client, make_store):
    db = make_store("event-chain")
    _exec(
        db,
        "UPDATE audit_log SET detail = ? WHERE doc_id = ? AND event = 'archived'",
        (json.dumps({"document_text": "tampered"}), DOC),
    )
    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["verification"]["status"] == "failed"
    assert payload["verification"]["failures"][0]["scope"] == "events"


def _fake_foundation(monkeypatch) -> None:
    """Install the foundation's M3 hashing surface for the adapter test."""
    from schemas.audit import compute_audit_hash

    module = types.ModuleType("audit.writer")

    def hash_entry(*, prev_hash, doc_id, entry_id, event, detail, matter_id, actor, timestamp):
        return compute_audit_hash(
            prev_hash,
            doc_id,
            entry_id,
            event,
            detail,
            matter_id=matter_id,
            actor=actor,
            timestamp=timestamp,
        )

    def attempt_hash(sub, *, matter_id, actor):
        envelope = {
            "parent_id": sub["parent_id"],
            "stage": sub["stage"],
            "attempt_no": sub["attempt_no"],
            "trigger": sub["trigger"],
            "outcome": sub.get("outcome"),
            "started_at": sub.get("started_at"),
            "ended_at": sub.get("ended_at"),
            "detail": sub.get("detail") or {},
        }
        return compute_audit_hash(
            "",
            sub["doc_id"],
            sub["attempt_id"],
            f"attempt:{sub['stage']}",
            envelope,
            matter_id=matter_id,
            actor=actor,
            timestamp=sub.get("started_at") or "",
        )

    module.hash_entry = hash_entry
    module.attempt_hash = attempt_hash
    monkeypatch.setitem(sys.modules, "audit.writer", module)


def _drop_fake_foundation(monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "audit.writer", raising=False)


def test_unified_row_and_addenda_verify_with_the_foundation_hasher(client, make_store, monkeypatch):
    _fake_foundation(monkeypatch)
    make_store("unified")

    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["store"]["schema"] == "unified-row"
    assert payload["store"]["migration"]["migrated"] is True
    assert payload["record"]["state"] == "unified-row"
    assert payload["record"]["event"] == "archived"
    assert payload["record"]["terminal"] is True
    assert payload["record"]["attempts"][0]["stage"] == "extraction"
    assert payload["record"]["addenda"][0]["kind"] == "rescore"
    assert payload["verification"]["status"] == "verified"
    assert payload["verification"]["method"] == "audit.writer"
    assert payload["verification"]["checked"]["main"] == 1
    assert payload["verification"]["checked"]["attempts"] == 1
    assert payload["verification"]["checked"]["addenda"] == 1
    assert SEED_TEXT not in json.dumps(payload)
    assert SEED_NOTE not in json.dumps(payload)


def test_unified_without_foundation_hasher_is_unverified_not_verified(client, make_store, monkeypatch):
    # The foundation is installed on this branch, so simulate the intended
    # "hasher unavailable" state at the seam instead of deleting sys.modules.
    from mailroom_diagnostics import audit

    monkeypatch.setattr(audit, "_foundation_writer", lambda: (None, None))
    make_store("unified")

    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["record"]["state"] == "unified-row"
    assert payload["verification"]["status"] == "unverified"
    assert payload["verification"]["checked"]["main"] == 1
    assert payload["verification"]["checked"]["attempts"] == 0
    assert any("attempt" in note for note in payload["verification"]["notes"])


def test_tampered_addendum_fails_verification(client, make_store, monkeypatch):
    _fake_foundation(monkeypatch)
    db = make_store("unified")
    _exec(
        db,
        "UPDATE audit_addenda SET detail = ? WHERE doc_id = ?",
        (json.dumps({"reason": "tampered"}), DOC),
    )
    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["verification"]["status"] == "failed"
    assert {failure["scope"] for failure in payload["verification"]["failures"]} == {"addenda"}


def test_tampered_attempt_row_fails_verification(client, make_store, monkeypatch):
    _fake_foundation(monkeypatch)
    db = make_store("unified")
    _exec(db, "UPDATE audit_attempts SET outcome = 'tampered' WHERE doc_id = ?", (DOC,))
    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["verification"]["status"] == "failed"
    assert any(
        failure["scope"] == "attempts" and failure["reason"] == "hash-mismatch"
        for failure in payload["verification"]["failures"]
    )


def test_truncated_attempt_read_never_reports_verified(client, make_store, monkeypatch):
    from mailroom_diagnostics import audit

    _fake_foundation(monkeypatch)
    db = make_store("unified")
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO audit_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"{DOC}-extract-2",
                f"row-{DOC}",
                DOC,
                "extraction",
                2,
                "retry",
                "ok",
                None,
                None,
                json.dumps({}),
                "",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(audit, "MAX_ATTEMPTS", 1)

    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["verification"]["status"] == "unverified"
    assert payload["verification"]["truncated"] is True


def test_verification_reads_one_consistent_snapshot(client, make_store, monkeypatch):
    """A commit between the row and addenda reads must not look like tampering.

    Without an explicit read transaction each SELECT gets its own snapshot; a
    writer committing mid-verification would then make a valid chain look
    broken. The probe mutates the addendum from a second connection between
    the main-row read and the addenda read.
    """
    from mailroom_diagnostics import audit

    _fake_foundation(monkeypatch)
    db = make_store("unified")
    writer = sqlite3.connect(db)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    finally:
        writer.close()

    observed: list[tuple[str, bool]] = []

    def probe(stage: str, conn) -> None:
        raw = conn.connection.dbapi_connection
        observed.append((stage, bool(getattr(raw, "in_transaction", False))))
        if stage == "mid":
            side = sqlite3.connect(db)
            try:
                side.execute(
                    "UPDATE audit_addenda SET prev_hash = 'tampered-between-reads' WHERE doc_id = ?",
                    (DOC,),
                )
                side.commit()
            finally:
                side.close()

    monkeypatch.setattr(audit, "_snapshot_probe", probe)
    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()

    assert observed[0] == ("begin", True), "the read transaction must open before the first SELECT"
    assert ("mid", True) in observed, "the snapshot must still be open between row and addenda reads"
    assert payload["verification"]["status"] == "verified"
    assert payload["verification"]["failures"] == []

    # The concurrent write did commit; the viewer simply never observed it.
    check = sqlite3.connect(db)
    try:
        assert (
            check.execute(
                "SELECT prev_hash FROM audit_addenda WHERE doc_id = ?", (DOC,)
            ).fetchone()[0]
            == "tampered-between-reads"
        )
    finally:
        check.close()


def test_listing_includes_legacy_only_documents_once(client, make_store, monkeypatch):
    _fake_foundation(monkeypatch)
    db = make_store("unified")
    _exec(db, "DELETE FROM audit_log WHERE doc_id = ?", (LEGACY_DOC,))

    documents = client.get("/v1/diagnostics/audit/documents").json()["documents"]
    ids = [document["doc_id"] for document in documents]
    assert sorted(ids) == sorted([DOC, "demo-correspondence-002", LEGACY_DOC])
    legacy = [document for document in documents if document["doc_id"] == LEGACY_DOC]
    assert len(legacy) == 1
    assert legacy[0]["source"] == "legacy"

    page = client.get("/v1/diagnostics/audit/documents", params={"limit": 2}).json()
    assert page["pagination"]["more"] is True
    assert len(page["documents"]) == 2


def test_missing_attempt_table_is_unverified_not_failed(client, make_store, monkeypatch):
    _fake_foundation(monkeypatch)
    db = make_store("unified")
    _exec(db, "DROP TABLE audit_attempts")

    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["record"]["state"] == "unified-row"
    assert payload["verification"]["status"] == "unverified"
    assert any("attempt" in note for note in payload["verification"]["notes"])


def test_migrated_legacy_events_are_surfaced_separately(client, make_store, monkeypatch):
    _fake_foundation(monkeypatch)
    db = make_store("unified")
    # The legacy section is the only record left for the third document. Its
    # sub-rows go with the terminal row: otherwise they are orphans (covered by
    # test_legacy_only_document_with_orphan_subrows_is_failed).
    _exec(db, "DELETE FROM audit_log WHERE doc_id = ?", (LEGACY_DOC,))
    _exec(db, "DELETE FROM audit_attempts WHERE doc_id = ?", (LEGACY_DOC,))

    payload = client.get(f"/v1/diagnostics/audit/documents/{LEGACY_DOC}").json()
    assert payload["record"]["state"] == "legacy-events"
    assert payload["legacy"]["state"] == "present"
    assert payload["legacy"]["count"] == 3
    assert [entry["event"] for entry in payload["legacy"]["entries"]] == [
        "ingested",
        "classified",
        "archived",
    ]
    assert payload["verification"]["status"] == "verified"
    assert payload["verification"]["checked"]["legacy"] == 3
    assert SEED_TEXT not in json.dumps(payload)


def test_listing_is_bounded_and_paginated(client, make_store, monkeypatch):
    _fake_foundation(monkeypatch)
    make_store("unified")

    first = client.get("/v1/diagnostics/audit/documents", params={"limit": 2}).json()
    assert len(first["documents"]) == 2
    assert first["pagination"] == {"limit": 2, "offset": 0, "returned": 2, "more": True}
    assert all("document_text" not in doc for doc in first["documents"])

    second = client.get(
        "/v1/diagnostics/audit/documents", params={"limit": 2, "offset": 2}
    ).json()
    assert len(second["documents"]) == 1
    assert second["pagination"]["more"] is False

    too_large = client.get("/v1/diagnostics/audit/documents", params={"limit": 101})
    assert too_large.status_code == 422
    negative = client.get("/v1/diagnostics/audit/documents", params={"offset": -1})
    assert negative.status_code == 422
    assert SEED_TEXT not in json.dumps(first)


def test_missing_document_is_reported_as_not_recorded(client, make_store):
    make_store("event-chain")
    payload = client.get("/v1/diagnostics/audit/documents/absent-doc-999").json()
    assert payload["record"]["state"] == "not-recorded"
    assert payload["verification"]["status"] == "not-recorded"


def test_unreadable_store_is_unavailable_without_a_path(client, tmp_path, monkeypatch):
    broken = tmp_path / "broken" / "mailroom.db"
    broken.parent.mkdir()
    broken.write_text("this is not a database", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{broken}")
    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["store"]["state"] == "unavailable"
    assert payload["store"]["reason"] in {"query-failed", "schema-absent"}
    assert str(broken) not in json.dumps(payload)


def test_capabilities_advertise_the_audit_surface(client):
    payload = client.get("/v1/diagnostics/capabilities").json()
    assert payload["features"]["audit"] is True
    assert payload["audit"]["read_only"] is True
    assert "unified-row" in payload["audit"]["schema_modes"]
    assert payload["audit"]["truncated_reads_verified"] is False


def test_run_summaries_carry_the_document_id(tmp_path, monkeypatch, client):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    journal = {
        "execution_id": "run-audit-1",
        "document": {"filename": "demo.pdf", "doc_id": DOC, "source": "upload"},
        "events": [],
        "node_sequence": ["intake"],
        "transitions": [],
        "outcome": {"stage": "archived", "doc_type": "contract", "review": False, "failed": False},
        "started_at": "2026-11-09T10:00:00+00:00",
        "finished_at": "2026-11-09T10:00:01+00:00",
    }
    (runs_dir / "run.json").write_text(json.dumps(journal), encoding="utf-8")
    monkeypatch.setenv("MAILROOM_DIAGNOSTICS_RUNS_DIR", str(runs_dir))

    payload = client.get("/v1/diagnostics/runs").json()
    assert payload["runs"][0]["document"]["doc_id"] == DOC


def test_cli_reports_the_same_verdict_and_exit_codes(make_store):
    db = make_store("event-chain")
    env = dict(os.environ)
    env.update({"OBSERVABILITY_PROVIDER": "none", "DATABASE_URL": f"sqlite+aiosqlite:///{db}"})
    env.pop("PYTHONPATH", None)

    verified = subprocess.run(
        [sys.executable, "-m", "mailroom_diagnostics", "audit", DOC, "--json"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["verification"]["status"] == "verified"

    missing = subprocess.run(
        [sys.executable, "-m", "mailroom_diagnostics", "audit", "absent-doc-999", "--json"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert missing.returncode == 3
    assert json.loads(missing.stdout)["verification"]["status"] == "not-recorded"

    listing = subprocess.run(
        [sys.executable, "-m", "mailroom_diagnostics", "audit", "--list", "--json"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert listing.returncode == 0, listing.stderr
    assert len(json.loads(listing.stdout)["documents"]) == 3


FOUNDATION_TABLES = """
CREATE TABLE audit_log (
    entry_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, matter_id TEXT NOT NULL,
    event TEXT NOT NULL, actor TEXT NOT NULL, detail TEXT NOT NULL,
    prev_hash TEXT NOT NULL, entry_hash TEXT NOT NULL, seq INTEGER NOT NULL,
    timestamp TEXT NOT NULL);
CREATE INDEX ix_audit_log_doc_id ON audit_log (doc_id);
CREATE TABLE audit_attempts (
    attempt_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, doc_id TEXT NOT NULL,
    stage TEXT NOT NULL, attempt_no INTEGER NOT NULL, trigger TEXT NOT NULL,
    outcome TEXT, started_at TEXT, ended_at TEXT, detail TEXT, entry_hash TEXT);
CREATE TABLE audit_addenda (
    addendum_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, kind TEXT NOT NULL,
    seq INTEGER NOT NULL, actor TEXT NOT NULL, timestamp TEXT, detail TEXT,
    prev_hash TEXT, entry_hash TEXT);
CREATE TABLE audit_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE audit_log_legacy (
    entry_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, matter_id TEXT NOT NULL,
    event TEXT NOT NULL, actor TEXT NOT NULL, detail TEXT,
    prev_hash TEXT, entry_hash TEXT, seq INTEGER, timestamp TEXT);
"""


def _foundation_pre_migration_store(
    tmp_path, monkeypatch, *, detail: dict | None = None
) -> Path:
    """All foundation tables exist, migration marker absent, audit_log is per-event.

    This is the shape `ensure_schema` leaves behind when the migration has not
    run (e.g. Postgres without MAILROOM_AUDIT_MIGRATE=1). Treating it as a
    unified row would pick one arbitrary event as the document's terminal row.
    """
    from schemas.audit import compute_audit_hash

    db = tmp_path / "pre-migration" / "mailroom.db"
    db.parent.mkdir()
    conn = sqlite3.connect(db)
    try:
        conn.executescript(FOUNDATION_TABLES)
        prev = ""
        for step, event in enumerate(("classified", "archived")):
            timestamp = f"2026-11-09T11:0{step}:00+00:00"
            row_detail = (
                {"document_text": SEED_TEXT, "stage": event} if detail is None else dict(detail)
            )
            entry_id = f"pre-migration-{step + 1}"
            digest = compute_audit_hash(
                prev,
                "pre-migration-001",
                entry_id,
                event,
                row_detail,
                matter_id="MATTER-P",
                actor="event-writer",
                timestamp=timestamp,
            )
            conn.execute(
                "INSERT INTO audit_log VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    entry_id,
                    "pre-migration-001",
                    "MATTER-P",
                    event,
                    "event-writer",
                    json.dumps(row_detail),
                    prev,
                    digest,
                    step + 1,
                    timestamp,
                ),
            )
            prev = digest
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    return db


def test_foundation_tables_without_migration_marker_stay_event_chain(client, tmp_path, monkeypatch):
    _drop_fake_foundation(monkeypatch)
    _foundation_pre_migration_store(tmp_path, monkeypatch)

    payload = client.get("/v1/diagnostics/audit/documents/pre-migration-001").json()
    assert payload["store"]["engine"] == "sqlite"
    assert payload["store"]["schema"] == "event-chain"
    assert payload["store"]["migration"] == {
        "migrated": False,
        "legacy_rows": None,
        "migrated_at": None,
    }
    assert payload["record"]["state"] == "event-chain"
    assert [event["event"] for event in payload["record"]["events"]] == ["classified", "archived"]
    assert payload["verification"]["status"] == "verified"
    assert SEED_TEXT not in json.dumps(payload)


def test_foundation_tables_with_migration_marker_are_unified(client, make_store, monkeypatch):
    _fake_foundation(monkeypatch)
    make_store("unified")
    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["store"]["engine"] == "sqlite"
    assert payload["store"]["schema"] == "unified-row"
    assert payload["store"]["migration"]["migrated"] is True


def test_mixed_legacy_and_row_pagination_is_stable_and_unique(client, make_store, monkeypatch):
    _fake_foundation(monkeypatch)
    db = make_store("unified")
    _exec(db, "DELETE FROM audit_log WHERE doc_id = ?", (LEGACY_DOC,))

    seen: list[str] = []
    for offset in (0, 2):
        page = client.get(
            "/v1/diagnostics/audit/documents", params={"limit": 2, "offset": offset}
        ).json()
        seen.extend(document["doc_id"] for document in page["documents"])
    assert sorted(seen) == sorted([DOC, "demo-correspondence-002", LEGACY_DOC])
    assert len(seen) == len(set(seen)), "a document must not appear on two pages"

    # A stable ordering also makes repeated pages identical.
    first = client.get("/v1/diagnostics/audit/documents", params={"limit": 2}).json()
    again = client.get("/v1/diagnostics/audit/documents", params={"limit": 2}).json()
    assert [d["doc_id"] for d in first["documents"]] == [d["doc_id"] for d in again["documents"]]


def test_seeder_refuses_protected_and_existing_targets(tmp_path, monkeypatch):
    from mailroom_diagnostics.seed_audit_demo import seed

    base = tmp_path / "base"
    base.mkdir()
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(base))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        seed(base / "mailroom.db", schema="event-chain", force=True)
    assert not (base / "mailroom.db").exists()

    target = tmp_path / "demo" / "mailroom.db"
    seed(target, schema="event-chain")
    with pytest.raises(SystemExit):
        seed(target, schema="event-chain")
    seed(target, schema="event-chain", force=True)
    assert target.is_file()
    # Nothing was written outside the explicit output directory.
    assert not (base / "mailroom.db").exists()


def test_seeder_never_overwrites_the_database_url_store(tmp_path, monkeypatch):
    from mailroom_diagnostics.seed_audit_demo import seed

    target = tmp_path / "live" / "mailroom.db"
    target.parent.mkdir()
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{target}")
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path / "base"))
    with pytest.raises(SystemExit):
        seed(target, schema="unified", force=True)
    assert not target.exists()


def _attempt_row(
    db: Path,
    *,
    attempt_id: str,
    parent_id: str,
    doc_id: str = DOC,
    stage: str = "extraction",
    attempt_no: int = 9,
    hash_override: str | None = None,
) -> str:
    """Insert an attempt row hashed by the REAL foundation helper."""
    from audit.writer import attempt_hash

    sub = {
        "attempt_id": attempt_id,
        "parent_id": parent_id,
        "doc_id": doc_id,
        "stage": stage,
        "attempt_no": attempt_no,
        "trigger": "initial",
        "outcome": "ok",
        "started_at": None,
        "ended_at": None,
        "detail": {"synthetic": True},
    }
    digest = hash_override or attempt_hash(sub, matter_id="DEMO-MATTER-1", actor="demo-seeder")
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO audit_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                attempt_id,
                parent_id,
                doc_id,
                stage,
                attempt_no,
                "initial",
                "ok",
                None,
                None,
                json.dumps({"synthetic": True}),
                digest,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return digest


def test_orphan_attempt_row_fails_even_with_a_valid_hash(client, make_store):
    db = make_store("unified")
    _attempt_row(db, attempt_id="orphan-1", parent_id="missing-parent")

    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["verification"]["status"] == "failed"
    assert {"scope": "attempts", "id": "orphan-1", "reason": "attempt-orphan"} in payload[
        "verification"
    ]["failures"]


def test_attempt_from_another_documents_parent_fails(client, make_store):
    db = make_store("unified")
    _attempt_row(db, attempt_id="cross-doc-1", parent_id="row-demo-correspondence-002")

    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["verification"]["status"] == "failed"
    assert any(
        failure["reason"] == "attempt-orphan" and failure["id"] == "cross-doc-1"
        for failure in payload["verification"]["failures"]
    )


def test_attempt_row_not_listed_by_its_parent_fails(client, make_store):
    db = make_store("unified")
    _attempt_row(db, attempt_id="unlisted-1", parent_id=f"row-{DOC}")

    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["verification"]["status"] == "failed"
    assert any(
        failure["reason"] == "attempt-not-listed" and failure["id"] == "unlisted-1"
        for failure in payload["verification"]["failures"]
    )


def test_malformed_attempt_reference_fails_with_a_rehashed_row(client, make_store):
    from audit.writer import hash_entry

    db = make_store("unified")
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT * FROM audit_log WHERE doc_id = ?", (DOC,)).fetchone()
        columns = [item[1] for item in conn.execute("PRAGMA table_info(audit_log)")]
        data = dict(zip(columns, row))
        detail = json.loads(data["detail"])
        detail["attempts"] = [{"stage": "extraction"}]  # no attempt_id
        digest = hash_entry(
            prev_hash=data["prev_hash"],
            doc_id=data["doc_id"],
            entry_id=data["entry_id"],
            event=data["event"],
            detail=detail,
            matter_id=data["matter_id"],
            actor=data["actor"],
            timestamp=data["timestamp"],
        )
        conn.execute(
            "UPDATE audit_log SET detail = ?, entry_hash = ? WHERE doc_id = ?",
            (json.dumps(detail), digest, DOC),
        )
        conn.commit()
    finally:
        conn.close()

    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["verification"]["status"] == "failed"
    assert any(
        failure["reason"] == "attempt-ref-malformed"
        for failure in payload["verification"]["failures"]
    )


def test_mutated_global_seq_fails_even_though_seq_is_unhashed(client, make_store):
    db = make_store("unified")
    # demo-claim-003 keeps its predecessor link (seq 2 -> 3) when only seq moves.
    _exec(db, "UPDATE audit_log SET seq = 9 WHERE doc_id = ?", (LEGACY_DOC,))
    payload = client.get(f"/v1/diagnostics/audit/documents/{LEGACY_DOC}").json()
    assert payload["verification"]["status"] == "failed"
    assert any(
        failure["reason"] == "seq-gap" and failure["scope"] == "main"
        for failure in payload["verification"]["failures"]
    )


def test_mutated_head_seq_must_be_one(client, make_store):
    db = make_store("unified")
    _exec(db, "UPDATE audit_log SET seq = 42 WHERE doc_id = ?", (DOC,))
    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["verification"]["status"] == "failed"
    assert any(
        failure["reason"] in {"seq-start-mismatch", "seq-gap"}
        and failure["scope"] == "main"
        for failure in payload["verification"]["failures"]
    )


def test_mutated_addendum_seq_fails_contiguity(client, make_store):
    db = make_store("unified")
    _exec(db, "UPDATE audit_addenda SET seq = 5 WHERE doc_id = ?", (DOC,))
    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["verification"]["status"] == "failed"
    assert any(
        failure["scope"] == "addenda" and failure["reason"] == "seq-gap"
        for failure in payload["verification"]["failures"]
    )


def test_verification_notes_state_the_bounded_chain_scope(client, make_store):
    make_store("unified")
    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["verification"]["status"] == "verified"
    assert any("predecessor" in note for note in payload["verification"]["notes"])
    assert any("not a whole-database chain proof" in note for note in payload["verification"]["notes"])


# ---------------------------------------------------------------------------
# R2 regressions: non-object detail, raw hashed columns, ambiguous migration
# markers, orphan sub-rows and bounded-read truncation.
# ---------------------------------------------------------------------------

# Every value ``_row_dict`` turns into a non-object ``detail`` (SQLite stores the
# JSON text; NULL also decodes to None).
MALFORMED_DETAILS = ["null", "[]", "0", '"text"']


def _minimal_unified_store(
    tmp_path,
    monkeypatch,
    *,
    with_attempt: bool = False,
    with_addendum: bool = False,
    with_legacy: bool = False,
    actor: str = "demo-seeder",
    matter_id: str = "DEMO-MATTER-1",
    attempt_started: str | None = None,
    attempt_ended: str | None = None,
) -> Path:
    """One unified document whose every stored detail is the object ``{}``.

    ``{}`` matters: it is exactly what the reader used to coerce a non-object
    detail to, so a stored null/list/scalar kept the coerced hash valid. That
    is the false green these tests pin. Every hash is written by the REAL
    foundation helpers, so the fixture is a legitimate writer record first.
    """
    from audit.writer import attempt_hash, hash_entry

    db = tmp_path / "minimal-unified" / "mailroom.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    try:
        conn.executescript(FOUNDATION_TABLES)
        conn.execute(
            "INSERT INTO audit_meta VALUES (?,?)",
            (
                "migration",
                json.dumps({"migrated_at": "2026-11-09T08:00:00+00:00", "legacy_rows": 0}),
            ),
        )
        row_id = f"row-{DOC}"
        timestamp = "2026-11-09T12:00:00+00:00"
        main_detail: dict = {}
        if with_attempt:
            attempt_id = f"{DOC}-attempt-1"
            sub = {
                "attempt_id": attempt_id,
                "parent_id": row_id,
                "doc_id": DOC,
                "stage": "extraction",
                "attempt_no": 1,
                "trigger": "initial",
                "outcome": "ok",
                "started_at": attempt_started,
                "ended_at": attempt_ended,
                "detail": {},
            }
            digest = attempt_hash(sub, matter_id=matter_id, actor=actor)
            conn.execute(
                "INSERT INTO audit_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt_id,
                    row_id,
                    DOC,
                    "extraction",
                    1,
                    "initial",
                    "ok",
                    attempt_started,
                    attempt_ended,
                    "{}",
                    digest,
                ),
            )
            main_detail["attempts"] = [
                {
                    "attempt_id": attempt_id,
                    "stage": "extraction",
                    "attempt_no": 1,
                    "entry_hash": digest,
                }
            ]
        row_digest = hash_entry(
            prev_hash="",
            doc_id=DOC,
            entry_id=row_id,
            event="archived",
            detail=main_detail,
            matter_id=matter_id,
            actor=actor,
            timestamp=timestamp,
        )
        conn.execute(
            "INSERT INTO audit_log VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                row_id,
                DOC,
                matter_id,
                "archived",
                actor,
                json.dumps(main_detail),
                "",
                row_digest,
                1,
                timestamp,
            ),
        )
        if with_addendum:
            addendum_id = f"add-{DOC}-1"
            addendum_time = "2026-11-09T12:30:00+00:00"
            digest = hash_entry(
                prev_hash=row_digest,
                doc_id=DOC,
                entry_id=addendum_id,
                event="rescore",
                detail={},
                matter_id=matter_id,
                actor=actor,
                timestamp=addendum_time,
            )
            conn.execute(
                "INSERT INTO audit_addenda VALUES (?,?,?,?,?,?,?,?,?)",
                (addendum_id, DOC, "rescore", 1, actor, addendum_time, "{}", row_digest, digest),
            )
        if with_legacy:
            prev = ""
            for step, event in enumerate(("ingested", "archived")):
                entry_id = f"legacy-{DOC}-{step + 1}"
                legacy_time = f"2026-11-09T11:0{step}:00+00:00"
                digest = hash_entry(
                    prev_hash=prev,
                    doc_id=DOC,
                    entry_id=entry_id,
                    event=event,
                    detail={},
                    matter_id=matter_id,
                    actor=actor,
                    timestamp=legacy_time,
                )
                conn.execute(
                    "INSERT INTO audit_log_legacy VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        entry_id,
                        DOC,
                        matter_id,
                        event,
                        actor,
                        "{}",
                        prev,
                        digest,
                        step + 1,
                        legacy_time,
                    ),
                )
                prev = digest
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    return db


@pytest.mark.parametrize("stored", MALFORMED_DETAILS)
def test_non_object_main_detail_fails_not_verified(client, tmp_path, monkeypatch, stored):
    db = _minimal_unified_store(tmp_path, monkeypatch)
    assert client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()["verification"][
        "status"
    ] == "verified"
    _exec(db, "UPDATE audit_log SET detail = ? WHERE doc_id = ?", (stored, DOC))

    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["verification"]["status"] == "failed"
    assert {
        "scope": "main",
        "id": f"row-{DOC}",
        "reason": "detail-not-object",
    } in payload["verification"]["failures"]


@pytest.mark.parametrize("stored", MALFORMED_DETAILS)
def test_non_object_addendum_detail_fails_not_verified(client, tmp_path, monkeypatch, stored):
    db = _minimal_unified_store(tmp_path, monkeypatch, with_addendum=True)
    assert client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()["verification"][
        "status"
    ] == "verified"
    _exec(db, "UPDATE audit_addenda SET detail = ? WHERE doc_id = ?", (stored, DOC))

    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["verification"]["status"] == "failed"
    assert {
        "scope": "addenda",
        "id": f"add-{DOC}-1",
        "reason": "detail-not-object",
    } in payload["verification"]["failures"]


@pytest.mark.parametrize("stored", MALFORMED_DETAILS)
def test_non_object_attempt_detail_fails_not_verified(client, tmp_path, monkeypatch, stored):
    db = _minimal_unified_store(tmp_path, monkeypatch, with_attempt=True)
    assert client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()["verification"][
        "status"
    ] == "verified"
    _exec(db, "UPDATE audit_attempts SET detail = ? WHERE doc_id = ?", (stored, DOC))

    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["verification"]["status"] == "failed"
    assert {
        "scope": "attempts",
        "id": f"{DOC}-attempt-1",
        "reason": "detail-not-object",
    } in payload["verification"]["failures"]


@pytest.mark.parametrize("stored", MALFORMED_DETAILS)
def test_non_object_legacy_detail_fails_not_verified(client, tmp_path, monkeypatch, stored):
    db = _minimal_unified_store(tmp_path, monkeypatch, with_legacy=True)
    assert client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()["verification"][
        "status"
    ] == "verified"
    _exec(db, "UPDATE audit_log_legacy SET detail = ? WHERE doc_id = ? AND seq = 2", (stored, DOC))

    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["verification"]["status"] == "failed"
    assert {
        "scope": "legacy",
        "id": f"legacy-{DOC}-2",
        "reason": "detail-not-object",
    } in payload["verification"]["failures"]


@pytest.mark.parametrize("stored", MALFORMED_DETAILS)
def test_non_object_event_detail_fails_not_verified(client, tmp_path, monkeypatch, stored):
    db = _foundation_pre_migration_store(tmp_path, monkeypatch, detail={})
    _exec(
        db,
        "UPDATE audit_log SET detail = ? WHERE doc_id = ? AND event = 'archived'",
        (stored, "pre-migration-001"),
    )

    payload = client.get("/v1/diagnostics/audit/documents/pre-migration-001").json()
    assert payload["verification"]["status"] == "failed"
    assert {
        "scope": "events",
        "id": "pre-migration-2",
        "reason": "detail-not-object",
    } in payload["verification"]["failures"]


@pytest.mark.parametrize(
    ("column", "tampered"),
    [
        ("actor", " demo-seeder "),
        ("matter_id", " DEMO-MATTER-1 "),
        ("event", " archived "),
        ("entry_id", f"row-{DOC} "),
        ("prev_hash", " "),
    ],
)
def test_padded_hashed_text_tamper_fails_verification(
    client, tmp_path, monkeypatch, column, tampered
):
    """Trimming a hashed column for comparison would hide this tamper."""
    db = _minimal_unified_store(tmp_path, monkeypatch)
    _exec(db, f"UPDATE audit_log SET {column} = ? WHERE doc_id = ?", (tampered, DOC))

    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["verification"]["status"] == "failed"
    assert any(
        failure["scope"] == "main" for failure in payload["verification"]["failures"]
    )


@pytest.mark.parametrize("actor", ["  demo-seeder  ", "\tdemo-seeder\t", "a" * 300])
def test_legitimate_padded_and_long_actor_verifies(client, tmp_path, monkeypatch, actor):
    """The raw column is what the writer hashed, so it must verify unchanged."""
    _minimal_unified_store(tmp_path, monkeypatch, actor=actor)
    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["verification"]["status"] == "verified"
    assert payload["verification"]["checked"]["main"] == 1


@pytest.mark.parametrize("column", ["started_at", "ended_at"])
@pytest.mark.parametrize("stored", ["not-a-timestamp", "", "2026-13-40T99:99:99"])
def test_invalid_attempt_timestamp_fails_instead_of_matching_null(
    client, tmp_path, monkeypatch, column, stored
):
    """``_iso`` used to collapse invalid text to None, matching a null-time hash."""
    db = _minimal_unified_store(tmp_path, monkeypatch, with_attempt=True)
    assert client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()["verification"][
        "status"
    ] == "verified"
    _exec(db, f"UPDATE audit_attempts SET {column} = ? WHERE doc_id = ?", (stored, DOC))

    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["verification"]["status"] == "failed"
    assert any(
        failure["scope"] == "attempts" and failure["reason"] == "timestamp-invalid"
        for failure in payload["verification"]["failures"]
    )


def test_attempt_timestamp_utc_offset_is_normalized_like_the_foundation(
    client, tmp_path, monkeypatch
):
    _minimal_unified_store(
        tmp_path, monkeypatch, with_attempt=True, attempt_started="2026-11-09T12:00:00+00:00"
    )
    db = tmp_path / "minimal-unified" / "mailroom.db"
    # The same instant expressed in another offset must still verify.
    _exec(
        db,
        "UPDATE audit_attempts SET started_at = ? WHERE doc_id = ?",
        ("2026-11-09T07:00:00-05:00", DOC),
    )
    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["verification"]["status"] == "verified"


def test_attempt_naive_timestamp_is_read_as_utc(client, tmp_path, monkeypatch):
    """SQLite loses the tzinfo; a naive stored value is UTC, not invalid."""
    _minimal_unified_store(
        tmp_path, monkeypatch, with_attempt=True, attempt_started="2026-11-09T12:00:00"
    )
    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["verification"]["status"] == "verified"


@pytest.mark.parametrize("stored", ["not-json", "42", "null", '"migrated"', "[]"])
def test_corrupt_migration_marker_is_ambiguous_and_never_verified(
    client, tmp_path, monkeypatch, stored
):
    db = _minimal_unified_store(tmp_path, monkeypatch)
    _exec(db, "UPDATE audit_meta SET value = ? WHERE key = 'migration'", (stored,))

    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["store"]["schema"] == "ambiguous"
    assert payload["store"]["migration"]["migrated"] is None
    assert payload["store"]["migration"]["reason"] == "migration-marker-invalid"
    assert payload["record"] == {"state": "unavailable", "reason": "migration-marker-invalid"}
    assert payload["verification"]["status"] == "unverified"
    assert any("migration marker" in note for note in payload["verification"]["notes"])

    listing = client.get("/v1/diagnostics/audit/documents").json()
    assert listing["store"]["schema"] == "ambiguous"
    assert listing["documents"][0]["source"] == "unknown"


def test_corrupt_marker_never_falls_back_to_the_event_chain(client, tmp_path, monkeypatch):
    db = _foundation_pre_migration_store(tmp_path, monkeypatch)
    _exec(db, "INSERT INTO audit_meta VALUES ('migration', 'not-json')")

    payload = client.get("/v1/diagnostics/audit/documents/pre-migration-001").json()
    assert payload["store"]["schema"] == "ambiguous"
    assert payload["record"]["state"] == "unavailable"
    assert payload["verification"]["status"] == "unverified"

    env = dict(os.environ)
    env.update({"OBSERVABILITY_PROVIDER": "none", "DATABASE_URL": f"sqlite+aiosqlite:///{db}"})
    env.pop("PYTHONPATH", None)
    cli = subprocess.run(
        [sys.executable, "-m", "mailroom_diagnostics", "audit", "pre-migration-001", "--json"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert cli.returncode == 3, cli.stderr
    assert json.loads(cli.stdout)["verification"]["status"] == "unverified"


def test_legacy_only_document_with_orphan_subrows_is_failed(client, make_store, monkeypatch):
    _fake_foundation(monkeypatch)
    db = make_store("unified")
    _exec(db, "DELETE FROM audit_log WHERE doc_id = ?", (LEGACY_DOC,))
    # The seeded attempt for this document now has no terminal row; the
    # addendum is an orphan too. A valid legacy section cannot vouch for them.
    _exec(
        db,
        "INSERT INTO audit_addenda VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "add-orphan-legacy-1",
            LEGACY_DOC,
            "rescore",
            1,
            "demo-seeder",
            "2026-11-09T12:00:00+00:00",
            "{}",
            "",
            "",
        ),
    )

    payload = client.get(f"/v1/diagnostics/audit/documents/{LEGACY_DOC}").json()
    assert payload["record"]["state"] == "legacy-events"
    assert payload["legacy"]["state"] == "present"
    assert payload["verification"]["status"] == "failed"
    assert {"scope": "attempts", "id": None, "reason": "attempt-orphan", "count": 1} in payload[
        "verification"
    ]["failures"]
    assert {"scope": "addenda", "id": None, "reason": "addendum-orphan", "count": 1} in payload[
        "verification"
    ]["failures"]
    assert any("terminal row" in note for note in payload["verification"]["notes"])


def test_truncated_event_read_never_reports_verified(client, make_store, monkeypatch):
    from mailroom_diagnostics import audit

    make_store("event-chain")
    monkeypatch.setattr(audit, "MAX_EVENTS", 2)

    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["verification"]["status"] == "unverified"
    assert payload["verification"]["truncated"] is True


def test_truncated_legacy_read_never_reports_verified(client, make_store, monkeypatch):
    from mailroom_diagnostics import audit

    db = make_store("unified")
    _exec(db, "DELETE FROM audit_log WHERE doc_id = ?", (LEGACY_DOC,))
    _exec(db, "DELETE FROM audit_attempts WHERE doc_id = ?", (LEGACY_DOC,))
    monkeypatch.setattr(audit, "MAX_EVENTS", 2)

    payload = client.get(f"/v1/diagnostics/audit/documents/{LEGACY_DOC}").json()
    assert payload["verification"]["status"] == "unverified"
    assert payload["verification"]["truncated"] is True


def test_truncated_addenda_read_never_reports_verified(client, make_store, monkeypatch):
    from mailroom_diagnostics import audit

    make_store("unified")
    monkeypatch.setattr(audit, "MAX_ADDENDA", 0)

    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["verification"]["status"] == "unverified"
    assert payload["verification"]["truncated"] is True


def test_cli_exits_one_for_a_non_object_detail(tmp_path, monkeypatch):
    db = _minimal_unified_store(tmp_path, monkeypatch)
    _exec(db, "UPDATE audit_log SET detail = 'null' WHERE doc_id = ?", (DOC,))

    env = dict(os.environ)
    env.update({"OBSERVABILITY_PROVIDER": "none", "DATABASE_URL": f"sqlite+aiosqlite:///{db}"})
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-m", "mailroom_diagnostics", "audit", DOC, "--json"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert json.loads(result.stdout)["verification"]["status"] == "failed"


# R3 regressions: truncation must never invent an orphan, duplicate terminal
# rows must never verify, and a marker that says "not migrated" must fail
# closed instead of being read as a unified row.


def _append_addenda_with_attempt(db: Path, count: int) -> tuple[str, str]:
    """Extend the seeded document's addendum chain; the last addendum owns an attempt.

    The seeded store already has one valid addendum for ``DOC``. Appending
    ``count`` more keeps every hash legitimate, so any failure the reader
    reports is a reader defect, not a tampered fixture.
    """
    from audit.writer import attempt_hash, hash_entry

    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT matter_id, actor FROM audit_log WHERE doc_id = ?", (DOC,)
        ).fetchone()
        assert row is not None
        matter_id, row_actor = row
        last = conn.execute(
            "SELECT seq, entry_hash FROM audit_addenda WHERE doc_id = ? "
            "ORDER BY seq DESC LIMIT 1",
            (DOC,),
        ).fetchone()
        seq, prev = (int(last[0]), last[1]) if last is not None else (0, "")
        addendum_id = ""
        attempt_id = ""
        for offset in range(1, count + 1):
            seq += 1
            addendum_id = f"add-{DOC}-extra-{seq}"
            timestamp = f"2026-11-09T{13 + seq // 60:02d}:{seq % 60:02d}:00+00:00"
            detail: dict = {}
            if offset == count:
                attempt_id = f"{addendum_id}-attempt-1"
                sub = {
                    "attempt_id": attempt_id,
                    "parent_id": addendum_id,
                    "doc_id": DOC,
                    "stage": "review",
                    "attempt_no": 1,
                    "trigger": "resume",
                    "outcome": "ok",
                    "started_at": timestamp,
                    "ended_at": timestamp,
                    "detail": {},
                }
                digest = attempt_hash(sub, matter_id=matter_id, actor=row_actor)
                conn.execute(
                    "INSERT INTO audit_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        attempt_id,
                        addendum_id,
                        DOC,
                        "review",
                        1,
                        "resume",
                        "ok",
                        timestamp,
                        timestamp,
                        "{}",
                        digest,
                    ),
                )
                detail = {
                    "attempts": [
                        {
                            "attempt_id": attempt_id,
                            "stage": "review",
                            "attempt_no": 1,
                            "entry_hash": digest,
                        }
                    ]
                }
            digest = hash_entry(
                prev_hash=prev,
                doc_id=DOC,
                entry_id=addendum_id,
                event="rescore",
                detail=detail,
                matter_id=matter_id,
                actor=row_actor,
                timestamp=timestamp,
            )
            conn.execute(
                "INSERT INTO audit_addenda VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    addendum_id,
                    DOC,
                    "rescore",
                    seq,
                    row_actor,
                    timestamp,
                    json.dumps(detail),
                    prev,
                    digest,
                ),
            )
            prev = digest
        conn.commit()
        return addendum_id, attempt_id
    finally:
        conn.close()


def _duplicate_terminal_row(db: Path) -> None:
    """Insert a second, hash-valid terminal row for the same document."""
    from audit.writer import hash_entry

    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT matter_id, actor, entry_hash FROM audit_log WHERE doc_id = ?", (DOC,)
        ).fetchone()
        assert row is not None
        matter_id, actor, prev = row
        timestamp = "2026-11-09T14:00:00+00:00"
        digest = hash_entry(
            prev_hash=prev,
            doc_id=DOC,
            entry_id="row-duplicate-terminal",
            event="archived",
            detail={},
            matter_id=matter_id,
            actor=actor,
            timestamp=timestamp,
        )
        conn.execute(
            "INSERT INTO audit_log VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "row-duplicate-terminal",
                DOC,
                matter_id,
                "archived",
                actor,
                "{}",
                prev,
                digest,
                2,
                timestamp,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_truncated_addenda_read_does_not_invent_attempt_orphans(client, make_store):
    """An addendum beyond the bounded read still owns its attempt rows.

    Reading only the first addenda must not turn the attempt under an unread
    addendum into a fabricated orphan; the honest verdict is incomplete.
    """
    db = make_store("unified")
    addendum_id, attempt_id = _append_addenda_with_attempt(db, 200)

    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    verification = payload["verification"]
    assert verification["truncated"] is True
    assert verification["status"] == "unverified"
    assert not any(
        failure["scope"] == "attempts" and failure["reason"] == "attempt-orphan"
        for failure in verification["failures"]
    ), verification["failures"]
    assert not any(
        failure.get("id") in {addendum_id, attempt_id} for failure in verification["failures"]
    )
    assert any("bounded" in note for note in verification["notes"])


def test_truncated_addenda_read_still_reports_a_verified_attempt_mismatch(client, make_store):
    """Truncation only prevents "verified"; an observed mismatch stays failed."""
    db = make_store("unified")
    _append_addenda_with_attempt(db, 200)
    _exec(db, "UPDATE audit_attempts SET outcome = 'tampered' WHERE doc_id = ?", (DOC,))

    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    verification = payload["verification"]
    assert verification["status"] == "failed"
    assert any(
        failure["scope"] == "attempts" and failure["reason"] == "hash-mismatch"
        for failure in verification["failures"]
    )
    assert not any(
        failure["reason"] == "attempt-orphan" for failure in verification["failures"]
    )


def test_cli_exits_three_when_an_unread_addendum_owns_an_attempt(tmp_path, monkeypatch):
    from mailroom_diagnostics.seed_audit_demo import seed

    db = tmp_path / "r3-addenda" / "mailroom.db"
    seed(db, schema="unified")
    _append_addenda_with_attempt(db, 200)

    env = dict(os.environ)
    env.update({"OBSERVABILITY_PROVIDER": "none", "DATABASE_URL": f"sqlite+aiosqlite:///{db}"})
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-m", "mailroom_diagnostics", "audit", DOC, "--json"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert result.returncode == 3, result.stderr
    verification = json.loads(result.stdout)["verification"]
    assert verification["status"] == "unverified"
    assert not any(
        failure["reason"] == "attempt-orphan" for failure in verification["failures"]
    )


def test_seeder_refuses_the_repository_checkouts_data_directory(tmp_path, monkeypatch):
    from mailroom_diagnostics import seed_audit_demo

    checkout = tmp_path / "checkout"
    (checkout / "src" / "config").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("", encoding="utf-8")
    (checkout / "src" / "config" / "taxonomy.yaml").write_text("", encoding="utf-8")
    checkout_store = checkout / "data" / "mailroom.db"

    assert seed_audit_demo._checkout_data_target(checkout / "tools" / "backend") == checkout_store
    monkeypatch.setattr(seed_audit_demo, "_checkout_data_target", lambda: checkout_store)
    with pytest.raises(SystemExit):
        seed_audit_demo.seed(checkout_store, schema="event-chain")
    assert not checkout_store.exists()


def test_duplicate_terminal_rows_fail_and_are_never_verified(client, tmp_path, monkeypatch):
    db = _minimal_unified_store(tmp_path, monkeypatch)
    _duplicate_terminal_row(db)

    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    verification = payload["verification"]
    assert verification["status"] == "failed"
    assert {"scope": "main", "id": None, "reason": "duplicate-terminal-row"} in verification[
        "failures"
    ]
    assert payload["record"]["state"] == "duplicate-row"
    assert payload["record"]["reason"] == "duplicate-terminal-row"

    listing = client.get("/v1/diagnostics/audit/documents").json()
    row = next(document for document in listing["documents"] if document["doc_id"] == DOC)
    assert row["entries"] == 2


def test_cli_exits_one_for_duplicate_terminal_rows(tmp_path, monkeypatch):
    db = _minimal_unified_store(tmp_path, monkeypatch)
    _duplicate_terminal_row(db)

    env = dict(os.environ)
    env.update({"OBSERVABILITY_PROVIDER": "none", "DATABASE_URL": f"sqlite+aiosqlite:///{db}"})
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-m", "mailroom_diagnostics", "audit", DOC, "--json"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert json.loads(result.stdout)["verification"]["status"] == "failed"


def test_explicitly_unmigrated_marker_is_ambiguous_not_a_unified_row(
    client, tmp_path, monkeypatch
):
    db = _minimal_unified_store(tmp_path, monkeypatch)
    _exec(
        db,
        "UPDATE audit_meta SET value = ? WHERE key = 'migration'",
        (
            json.dumps(
                {
                    "migrated": False,
                    "migrated_at": "2026-11-09T08:00:00+00:00",
                    "legacy_rows": 0,
                }
            ),
        ),
    )

    payload = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    assert payload["store"]["schema"] == "ambiguous"
    assert payload["store"]["migration"]["migrated"] is None
    assert payload["store"]["migration"]["reason"] == "migration-marker-invalid"
    assert payload["record"]["state"] == "unavailable"
    assert payload["verification"]["status"] == "unverified"

    listing = client.get("/v1/diagnostics/audit/documents").json()
    assert listing["store"]["schema"] == "ambiguous"


def test_query_failure_note_is_truthful_and_leaks_no_detail(client, monkeypatch):
    from contextlib import contextmanager

    from mailroom_diagnostics import audit

    @contextmanager
    def failing_connection():
        raise RuntimeError("dsn=postgresql://secret-dsn")
        yield  # pragma: no cover - unreachable

    monkeypatch.setattr(audit, "_connection", failing_connection)

    listing = client.get("/v1/diagnostics/audit/documents").json()
    assert listing["store"]["reason"] == "query-failed"
    listing_text = json.dumps(listing)
    assert "server log" not in listing_text
    assert "secret-dsn" not in listing_text

    detail = client.get(f"/v1/diagnostics/audit/documents/{DOC}").json()
    detail_text = json.dumps(detail)
    assert detail["verification"]["status"] == "unavailable"
    assert "server log" not in detail_text
    assert "secret-dsn" not in detail_text
