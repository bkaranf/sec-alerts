"""Durable collection and briefing state, separate from SMTP acknowledgments."""
import json
import sqlite3
import hashlib
from pathlib import Path
from .models import Document, utcnow


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
        CREATE TABLE IF NOT EXISTS run_history(id INTEGER PRIMARY KEY,started TEXT NOT NULL,finished TEXT,status TEXT NOT NULL,detail TEXT NOT NULL);
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def ingest(self, result):
        # Validate independently so one bad archive cannot discard other issuers.
        valid = []
        for doc in result.documents:
            try:
                intact = hashlib.sha256(Path(doc.path).read_bytes()).hexdigest() == doc.content_hash
            except OSError:
                intact = False
            if intact:
                valid.append(doc)
                continue
            source_key = doc.metadata.get("source_key", f"{doc.metadata.get('ticker', doc.issuer)}:{doc.source}")
            issue = {"issuer": doc.issuer, "cik": doc.cik, "source": doc.source,
                     "source_key": source_key, "url": doc.url, "period": doc.period,
                     "accession": doc.accession, "document": doc.id,
                     "status": "archive_integrity_failed", "detail": "Archive absent, unreadable or hash mismatch; reacquisition required."}
            result.errors.append(issue)
            result.pending.append(issue)
            result.checkpoints.pop(source_key, None)
        result.documents = valid
        with self.db:
            for doc in result.documents:
                self.db.execute("INSERT OR IGNORE INTO documents VALUES(?,?,?,?,?,?)", (doc.id, doc.cik, doc.period, doc.content_hash, json.dumps(doc.to_dict()), utcnow()))
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

    def documents(self) -> list[Document]:
        return [Document(**json.loads(row[0])) for row in self.db.execute("SELECT payload FROM documents ORDER BY first_seen,id")]

    def checkpoints(self) -> dict:
        return dict(self.db.execute("SELECT source,last_success FROM source_checkpoints"))

    def pending(self) -> list[dict]:
        return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM pending_work")]

    def covered_ids(self) -> set[str]:
        # Retain every source URL, but an unchanged byte-identical copy for the
        # same reporting CIK does not create another executive alert.
        return {row[0] for row in self.db.execute("""
            SELECT alias.id FROM documents alias JOIN documents original
            ON alias.cik=original.cik AND alias.hash=original.hash
            WHERE original.id IN (
                SELECT b.document_id FROM briefing_documents b JOIN briefings r
                ON r.id=b.report_id WHERE r.status!='withdrawn'
                UNION SELECT id FROM historical_documents
            )
        """)}

    def baseline_ciks(self) -> set[str]:
        return {row[0] for row in self.db.execute("SELECT cik FROM baseline_issuers")}

    def save_report(self, report_id, report, documents, output_dir, baseline, historical=()):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO briefings VALUES(?,?,?,?,?,?)", (report_id, utcnow(), int(baseline), "prepared", str(output_dir), json.dumps(report)))
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
