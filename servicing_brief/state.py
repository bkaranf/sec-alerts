"""Durable collection and briefing state, separate from SMTP acknowledgments."""
import json
import sqlite3
import hashlib
from pathlib import Path
from .models import Document, utcnow
from .document_identity import asset_manifest, content_fingerprint, verify_document_assets


def _archive_intact(path, expected_hash):
    try:
        with Path(path).open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest() == str(expected_hash).lower()
    except (OSError, TypeError, ValueError):
        return False


def _repair_archive_paths(original, recovered):
    """Update only missing/corrupt local copies with verified identical bytes."""
    identity_fields = ("id", "cik", "url")
    if not all(original.get(key) == recovered.get(key) for key in identity_fields):
        return
    if str(original.get("content_hash", "")).lower() != str(recovered.get("content_hash", "")).lower():
        return
    if (
        original["path"] != recovered["path"]
        and not _archive_intact(original["path"], original["content_hash"])
    ):
        if _archive_intact(recovered["path"], original["content_hash"]):
            original["path"] = recovered["path"]
    recovered_assets = {
        (asset.get("document"), str(asset.get("content_hash", "")).lower()): asset
        for asset in recovered.get("metadata", {}).get("assets", [])
        if isinstance(asset, dict)
    }
    for asset in original.get("metadata", {}).get("assets", []):
        replacement = recovered_assets.get((asset.get("document"), str(asset.get("content_hash", "")).lower()))
        if replacement and asset.get("path") != replacement.get("path"):
            if (
                not _archive_intact(asset.get("path", ""), asset.get("content_hash"))
                and _archive_intact(replacement.get("path", ""), asset.get("content_hash"))
            ):
                asset["path"] = replacement["path"]


def _matches_pending_work(document, pending):
    """Match a recovered original to a specific issuer/source retry record."""
    source_key = str(pending.get("source_key", ""))
    source = pending.get("source") or source_key.rsplit(":", 1)[-1]
    if source != document.source:
        return False
    ticker = str(document.metadata.get("ticker", document.issuer))
    if pending.get("cik"):
        same_issuer = str(pending["cik"]).lstrip("0") == document.cik.lstrip("0")
    elif source_key:
        same_issuer = source_key == document.metadata.get("source_key", f"{ticker}:{document.source}")
    else:
        same_issuer = bool(pending.get("ticker")) and str(pending["ticker"]).casefold() == ticker.casefold()
    if not same_issuer:
        return False

    selector = pending.get("document")
    if selector == document.id:
        return True
    known_urls = {document.url, document.metadata.get("discovery_url", "")}
    if pending.get("url") and pending["url"] in known_urls:
        return True
    same_accession = bool(document.accession) and pending.get("accession") == document.accession
    if selector:
        # SEC attachment names are scoped to an accession, never an issuer's
        # entire history of similarly named exhibits.
        return same_accession and selector == document.metadata.get("document")
    # Whole-accession retries carry the filing index URL rather than each
    # exhibit URL. A document-specific URL must not broaden an IR retry.
    return (document.source == "sec" and same_accession
            and (not pending.get("url") or pending["url"] == document.discovered_from))


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,cik TEXT NOT NULL,period TEXT NOT NULL,hash TEXT NOT NULL,payload TEXT NOT NULL,first_seen TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS source_checkpoints(source TEXT PRIMARY KEY,last_success TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pending_work(key TEXT PRIMARY KEY,payload TEXT NOT NULL,updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS source_failures(id INTEGER PRIMARY KEY,recorded TEXT NOT NULL,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS briefings(id TEXT PRIMARY KEY,created TEXT NOT NULL,baseline INTEGER NOT NULL,status TEXT NOT NULL,output_dir TEXT NOT NULL,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS briefing_documents(report_id TEXT NOT NULL,document_id TEXT NOT NULL,PRIMARY KEY(report_id,document_id));
        CREATE TABLE IF NOT EXISTS baseline_issuers(cik TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS historical_documents(id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS recovered_documents(id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS document_fingerprints(id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS document_fingerprint_lookup ON document_fingerprints(fingerprint);
        CREATE TABLE IF NOT EXISTS run_history(id INTEGER PRIMARY KEY,started TEXT NOT NULL,finished TEXT,status TEXT NOT NULL,detail TEXT NOT NULL);
        """)
        # Add identity metadata without rewriting legacy documents, report
        # snapshots, delivery history, or their durable IDs.
        with self.db:
            for row in self.db.execute("SELECT d.id,d.payload FROM documents d LEFT JOIN document_fingerprints f ON f.id=d.id WHERE f.id IS NULL").fetchall():
                try:
                    fingerprint = content_fingerprint(Document(**json.loads(row["payload"])))
                except (ValueError, TypeError, KeyError):
                    # Unverifiable legacy manifests must never alias another
                    # disclosure merely because their HTML wrappers match.
                    fingerprint = "legacy-invalid:" + row["id"]
                self.db.execute("INSERT INTO document_fingerprints VALUES(?,?)", (row["id"], fingerprint))

    def close(self):
        self.db.close()

    def ingest(self, result):
        # Validate independently so one bad archive cannot discard other issuers.
        prior_pending = self.pending()
        valid = []
        with self.db:
            for doc in result.documents:
                try:
                    if not _archive_intact(doc.path, doc.content_hash):
                        raise ValueError("Invalid wrapper archive")
                    verify_document_assets(doc)
                    admitted = self._admit_document(doc)
                except (ValueError, TypeError, OSError):
                    source_key = doc.metadata.get("source_key", f"{doc.metadata.get('ticker', doc.issuer)}:{doc.source}")
                    issue = {"issuer": doc.issuer, "cik": doc.cik, "source": doc.source,
                             "source_key": source_key, "url": doc.url, "period": doc.period,
                             "accession": doc.accession, "document": doc.id,
                             "status": "archive_integrity_failed", "detail": "Archive or declared image assets absent, invalid or hash mismatch; reacquisition required."}
                    result.errors.append(issue)
                    result.pending.append(issue)
                    result.checkpoints.pop(source_key, None)
                    continue
                valid.append(admitted)
                if any(_matches_pending_work(doc, item) for item in prior_pending):
                    # Source checkpoints may clear pending_work below. Keep
                    # notification intent durable until a draft covers it,
                    # including retries after failed report preparation.
                    self.db.execute("INSERT OR IGNORE INTO recovered_documents VALUES(?)", (admitted.id,))
            result.documents = valid
            for source, checkpoint in result.checkpoints.items():
                self.db.execute("INSERT OR REPLACE INTO source_checkpoints VALUES(?,?)", (source, checkpoint))
            for error in result.errors:
                self.db.execute("INSERT INTO source_failures(recorded,payload) VALUES(?,?)", (utcnow(), json.dumps(error)))
            # Pending entries are refreshed per successfully checked source, failures retain earlier entries.
            for source in result.checkpoints:
                rows = self.db.execute("SELECT key,payload FROM pending_work").fetchall()
                for row in rows:
                    if json.loads(row["payload"]).get("source_key") == source:
                        self.db.execute("DELETE FROM pending_work WHERE key=?", (row["key"],))
            for item in result.pending:
                key = json.dumps({k: item.get(k) for k in ("issuer", "ticker", "cik", "source", "kind", "url", "period", "accession", "document")}, sort_keys=True)
                self.db.execute("INSERT OR REPLACE INTO pending_work VALUES(?,?,?)", (key, json.dumps(item), utcnow()))

    def _admit_document(self, recovered):
        """Preserve each verified bundle version; reacquisition only repairs paths."""
        fingerprint = content_fingerprint(recovered)
        versions = self.db.execute("""
            SELECT d.payload,f.fingerprint FROM documents d
            JOIN document_fingerprints f ON f.id=d.id
            WHERE d.cik=? AND json_extract(d.payload,'$.url')=? AND d.hash=? COLLATE NOCASE
            ORDER BY d.rowid DESC
        """, (recovered.cik, recovered.url, recovered.content_hash)).fetchall()
        # A partial reacquisition can repair an older outstanding report even
        # when its asset content is already superseded in the newest version.
        repaired_versions = []
        for row in versions:
            original = json.loads(row["payload"])
            if not row["fingerprint"].startswith("legacy-invalid:"):
                replacement = recovered.to_dict()
                replacement["id"] = original["id"]
                _repair_archive_paths(original, replacement)
                if json.dumps(original) != row["payload"]:
                    self.db.execute("UPDATE documents SET payload=? WHERE id=?", (json.dumps(original), original["id"]))
            repaired_versions.append({"payload": json.dumps(original), "fingerprint": row["fingerprint"]})
        versions = repaired_versions
        existing = next((row for row in versions if row["fingerprint"] == fingerprint), None)
        candidate = Document(**recovered.to_dict())
        if existing is None and versions:
            # Unchanged HTML cannot establish that an omitted image was removed.
            # Preserve known images and combine independently recovered pieces.
            # Check exact old versions first, so a stale subset cannot roll back
            # a corrected image in the newest bundle.
            latest = next((row for row in versions if not row["fingerprint"].startswith("legacy-invalid:")), None)
            if latest is not None:
                original = Document(**json.loads(latest["payload"]))
                assets = {item["document"]: item for item in original.metadata.get("assets", [])}
                known_pairs = {
                    (item["document"], item["content_hash"])
                    for row in versions if not row["fingerprint"].startswith("legacy-invalid:")
                    for item in asset_manifest(Document(**json.loads(row["payload"])))
                }
                for item in recovered.metadata.get("assets", []):
                    name = item["document"]
                    pair = (name, item["content_hash"].lower())
                    if name not in assets or pair not in known_pairs or item["content_hash"].lower() == assets[name]["content_hash"].lower():
                        assets[name] = item
                if assets:
                    candidate.metadata["assets"] = [assets[name] for name in sorted(assets)]
                fingerprint = content_fingerprint(candidate)
                existing = next((row for row in versions if row["fingerprint"] == fingerprint), None)
        if existing:
            original = json.loads(existing["payload"])
            recovered.id = original["id"]
            # Repairs above retain this version's original chronology and
            # reviewed metadata. Return the canonical persisted observation.
            admitted = Document(**original)
            verify_document_assets(admitted)
            return admitted

        # Validate reused assets as well as the newly acquired files before
        # admitting a new version that promises their combined evidence.
        verify_document_assets(candidate)
        identity_hash = fingerprint if asset_manifest(candidate) else candidate.content_hash
        candidate.id = hashlib.sha256(f"{candidate.cik}|{candidate.url}|{identity_hash}".encode()).hexdigest()[:24]
        collision = self.db.execute("SELECT fingerprint FROM document_fingerprints WHERE id=?", (candidate.id,)).fetchone()
        if collision and collision["fingerprint"].startswith("legacy-invalid:"):
            # A malformed legacy manifest may occupy the wrapper-only ID.
            # Recover valid content under its bundle identity, never overwrite
            # that historical record or its old report associations.
            candidate.id = hashlib.sha256(f"{candidate.cik}|{candidate.url}|{fingerprint}".encode()).hexdigest()[:24]
        if self.db.execute("SELECT 1 FROM documents WHERE id=?", (candidate.id,)).fetchone():
            raise ValueError("Document identity collision")
        self.db.execute("INSERT INTO documents VALUES(?,?,?,?,?,?)", (candidate.id, candidate.cik, candidate.period, candidate.content_hash, json.dumps(candidate.to_dict()), utcnow()))
        self.db.execute("INSERT INTO document_fingerprints VALUES(?,?)", (candidate.id, fingerprint))
        recovered.id = candidate.id
        return candidate

    def documents(self) -> list[Document]:
        return [Document(**json.loads(row[0])) for row in self.db.execute("SELECT payload FROM documents ORDER BY first_seen,id")]

    def restore_snapshot_paths(self, snapshot):
        """Resolve local paths for a saved context document, retaining its evidence."""
        restored = json.loads(json.dumps(snapshot))
        row = self.db.execute("SELECT payload FROM documents WHERE id=?", (restored.get("id"),)).fetchone()
        if row:
            _repair_archive_paths(restored, json.loads(row["payload"]))
        return restored

    def checkpoints(self) -> dict:
        return dict(self.db.execute("SELECT source,last_success FROM source_checkpoints"))

    def pending(self) -> list[dict]:
        return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM pending_work")]

    def recovered_ids(self) -> set[str]:
        return {row[0] for row in self.db.execute("SELECT id FROM recovered_documents")}

    def covered_ids(self) -> set[str]:
        # Retain every source URL. Only copies of the entire same disclosure,
        # including its images, share coverage for the same reporting CIK.
        return {row[0] for row in self.db.execute("""
            SELECT alias.id FROM documents alias JOIN documents original
            ON alias.cik=original.cik
            JOIN document_fingerprints af ON af.id=alias.id
            JOIN document_fingerprints ofp ON ofp.id=original.id
            WHERE af.fingerprint=ofp.fingerprint AND original.id IN (
                SELECT b.document_id FROM briefing_documents b JOIN briefings r
                ON r.id=b.report_id WHERE r.status!='withdrawn'
                UNION SELECT id FROM historical_documents
            )
        """)}

    def baseline_ciks(self) -> set[str]:
        return {row[0] for row in self.db.execute("SELECT cik FROM baseline_issuers")}

    def next_report_id(self, identity_hash):
        """Choose a new draft identity without overwriting durable history.

        The pipeline holds its run lock through selection and save. Failed
        preparation can reuse its unsaved identity; a withdrawn draft cannot.
        """
        report_id = identity_hash
        revision = 0
        while self.db.execute("SELECT 1 FROM briefings WHERE id=?", (report_id,)).fetchone():
            revision += 1
            report_id = hashlib.sha256(f"{identity_hash}|replacement:{revision}".encode()).hexdigest()[:24]
        return report_id

    def save_report(self, report_id, report, documents, output_dir, baseline, historical=()):
        with self.db:
            self.db.execute("INSERT INTO briefings VALUES(?,?,?,?,?,?)", (report_id, utcnow(), int(baseline), "prepared", str(output_dir), json.dumps(report)))
            for doc in documents:
                self.db.execute("INSERT OR IGNORE INTO briefing_documents VALUES(?,?)", (report_id, doc.id))
                self.db.execute("INSERT OR IGNORE INTO baseline_issuers VALUES(?)", (doc.cik,))
            for doc in historical:
                self.db.execute("INSERT OR IGNORE INTO historical_documents VALUES(?)", (doc.id,))

    def outstanding(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM briefings WHERE status IN ('prepared','pending','ambiguous') ORDER BY created")]

    def withdraw_unsent_report(self, report_id, reason):
        """Preserve a defective draft for audit while removing it from delivery."""
        row = self.db.execute("SELECT * FROM briefings WHERE id=?", (report_id,)).fetchone()
        if not row or row["status"] != "prepared":
            raise ValueError("Only an unsent prepared report can be withdrawn")
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='delivery_messages'").fetchone():
            for message in self.db.execute("SELECT path,attempts,status FROM delivery_messages"):
                if Path(message["path"]).resolve().parent == Path(row["output_dir"]).resolve() and (message["attempts"] > 0 or message["status"] in {"accepted", "ambiguous"}):
                    raise ValueError("This draft has delivery history and requires explicit reconciliation")
        payload = json.loads(row["payload"])
        payload["withdrawal"] = {"reason": reason, "at": utcnow()}
        ciks = {d.cik for d in self.report_documents(report_id)}
        with self.db:
            self.db.execute("UPDATE briefings SET status='withdrawn',payload=? WHERE id=?", (json.dumps(payload), report_id))
            for cik in ciks:
                remaining = self.db.execute("SELECT 1 FROM briefings r JOIN briefing_documents b ON b.report_id=r.id JOIN documents d ON d.id=b.document_id WHERE d.cik=? AND r.status!='withdrawn' LIMIT 1", (cik,)).fetchone()
                if not remaining:
                    self.db.execute("DELETE FROM baseline_issuers WHERE cik=?", (cik,))

    def report_documents(self, report_id):
        rows = self.db.execute("SELECT d.payload FROM documents d JOIN briefing_documents b ON d.id=b.document_id WHERE b.report_id=?", (report_id,))
        return [Document(**json.loads(row[0])) for row in rows]

    def set_report_status(self, report_id, status):
        with self.db:
            self.db.execute("UPDATE briefings SET status=? WHERE id=?", (status, report_id))

    def start_run(self):
        with self.db:
            cursor = self.db.execute("INSERT INTO run_history(started,status,detail) VALUES(?,?,?)", (utcnow(), "running", ""))
        return cursor.lastrowid

    def finish_run(self, run_id, status, detail=""):
        with self.db:
            self.db.execute("UPDATE run_history SET finished=?,status=?,detail=? WHERE id=?", (utcnow(), status, detail, run_id))

    def status(self):
        return {
            "documents": self.db.execute("SELECT count(*) FROM documents").fetchone()[0],
            "briefings": [dict(r) for r in self.db.execute("SELECT id,created,baseline,status,output_dir FROM briefings ORDER BY created DESC LIMIT 20")],
            "checkpoints": self.checkpoints(),
            "pending": [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM pending_work")],
            "recent_failures": [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM source_failures ORDER BY id DESC LIMIT 20")],
            "recent_runs": [dict(r) for r in self.db.execute("SELECT * FROM run_history ORDER BY id DESC LIMIT 10")],
        }
