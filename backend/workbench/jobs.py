from __future__ import annotations

import json
import threading

from .models import Annotation, Connection, Ontology, api_schema, validate_annotation
from .providers import ProviderError
from .storage import encode, now


class JobRunner:
    """One local worker; task commit is atomic. Stop never starts the next request."""

    def __init__(self, service):
        self.service = service
        self.db = service.db
        self.lock = threading.RLock()
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.job_id = None
        # An uncommitted request may have been billed remotely; warn before retrying.
        self.db.execute("UPDATE tasks SET status='error',error='interrupted' WHERE status='running'")
        self.db.execute("UPDATE jobs SET state='paused' WHERE state IN ('running','stopping')")

    def start(self, job_id, retry_errors=False):
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError("Другое задание ещё выполняется или останавливается")
            job = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
            _, mode = self.service.profile(job["profile_id"])
            self.service.secrets.get(job["profile_id"], mode)
            if retry_errors:
                self.db.execute("UPDATE tasks SET status='pending',error=NULL WHERE job_id=? AND status='error'", (job_id,))
            self.stop_event.clear()
            self.job_id = job_id
            self.db.execute("UPDATE jobs SET state='running',finished=NULL WHERE id=?", (job_id,))
            self.thread = threading.Thread(target=self._run, args=(job_id,), daemon=True,
                                           name="ontology-annotation")
            self.thread.start()

    def create_and_start(self, project_id, version_id, split, confirmation):
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError("Другое задание ещё выполняется. Дождитесь остановки.")
            job_id = self.service.create_job(project_id, version_id, split, confirmation)
            self.start(job_id)
            return job_id

    def stop(self, job_id):
        with self.lock:
            if self.job_id == job_id and self.thread and self.thread.is_alive():
                self.stop_event.set()
                self.db.execute("UPDATE jobs SET state='stopping' WHERE id=?", (job_id,))
            else:
                self.db.one("SELECT id FROM jobs WHERE id=?", (job_id,))

    def _run(self, job_id):
        job = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        profile = Connection.model_validate_json(job["profile"])
        prompt = json.loads(job["prompt"])
        ontology = Ontology.model_validate(prompt["ontology"])
        try:
            while not self.stop_event.is_set():
                with self.db.connect() as db:
                    task = db.execute("SELECT item_id FROM tasks WHERE job_id=? AND status='pending' "
                                      "ORDER BY item_id LIMIT 1", (job_id,)).fetchone()
                    if not task:
                        break
                    item_id = task[0]
                    db.execute("UPDATE tasks SET status='running',attempts=attempts+1 WHERE job_id=? AND item_id=?",
                               (job_id, item_id))
                item = self.db.one("SELECT * FROM items WHERE id=?", (item_id,))
                try:
                    _, mode = self.service.profile(job["profile_id"])
                    key = self.service.secrets.get(job["profile_id"], mode)
                    raw = self.service.provider.generate(profile, key, prompt["system"], encode({
                        "ontology": prompt["ontology"], "approved_examples": prompt["approved_dev_examples"],
                        "item": {"text": item["text"], "context": item["context"]},
                    }), api_schema(Annotation))
                    try:
                        annotation = validate_annotation(ontology, item, Annotation.model_validate(raw))
                    except ValueError as exc:
                        raise ProviderError("format") from exc
                    self.db.execute("UPDATE tasks SET status='done',result=?,error=NULL WHERE job_id=? AND item_id=?",
                                    (annotation.model_dump_json(), job_id, item_id))
                except ProviderError as exc:
                    self.db.execute("UPDATE tasks SET status='error',error=?,result=NULL WHERE job_id=? AND item_id=?",
                                    (exc.code, job_id, item_id))
                    if exc.code in {"auth", "rate_limit", "network", "server", "no_key", "secret_store", "request"}:
                        self.stop_event.set()
                except Exception:
                    # Never persist raw exception text: it could include a key or private text.
                    self.db.execute("UPDATE tasks SET status='error',error='internal',result=NULL WHERE job_id=? AND item_id=?",
                                    (job_id, item_id))
                    self.stop_event.set()
        finally:
            pending = self.db.query("SELECT 1 FROM tasks WHERE job_id=? AND status='pending' LIMIT 1", (job_id,))
            errors = self.db.query("SELECT 1 FROM tasks WHERE job_id=? AND status='error' LIMIT 1", (job_id,))
            state = "paused" if pending or self.stop_event.is_set() else "completed_with_errors" if errors else "completed"
            self.db.execute("UPDATE jobs SET state=?,finished=? WHERE id=?", (state, now(), job_id))

    def shutdown(self):
        self.stop_event.set()
        # Threads are daemonized; unresolved in-flight tasks are marked interrupted next startup.
        if self.thread:
            self.thread.join(timeout=2)


def job_summary(db, job_id):
    job = db.one("SELECT id,project_id,version_id,split,state,created,finished FROM jobs WHERE id=?", (job_id,))
    counts = {r["status"]: r["count"] for r in db.query(
        "SELECT status,COUNT(*) count FROM tasks WHERE job_id=? GROUP BY status", (job_id,))}
    job["counts"] = counts
    job["total"] = sum(counts.values())
    return job
