from __future__ import annotations

import copy
import io
import json
import threading
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient
from workbench.api import create_app
from workbench.demos import LIBRARY, RESIDENTS
from workbench.evaluation import evaluate
from workbench.exports import csv_bytes, export_job, ontology_package
from workbench.imports import assign_splits, digest, read_records
from workbench.jobs import JobRunner
from workbench.locking import InstanceLock
from workbench.models import Annotation, Connection, Ontology, api_schema, validate_annotation
from workbench.providers import ProviderClient, ProviderError, SecretStore
from workbench.service import Service
from workbench.storage import Database, encode, now, uid

KEY = "TEST_ONLY_SENTINEL_DO_NOT_EXPORT_7821"


def negative(status="no_match"):
    return {"status": status, "labels": [], "entities": [], "relations": [], "rationale": "Технический тест на вымышленной записи"}


def positive(concept_id, text):
    return {**negative(), "status": "labeled", "labels": [{"concept_id": concept_id, "evidence": [
        {"field": "text", "start": 0, "end": len(text), "quote": text}]}]}


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.fail_once = None
        self.entered = threading.Event()
        self.release = None

    def generate(self, profile, key, system, prompt, schema):
        self.calls.append(prompt)
        self.entered.set()
        if self.release:
            self.release.wait(5)
        if self.fail_once:
            code = self.fail_once
            self.fail_once = None
            raise ProviderError(code)
        data = json.loads(prompt)
        text = data["item"]["text"]
        concept_id = data["ontology"]["concepts"][0]["id"]
        return positive(concept_id, text) if text.startswith("+") else negative()


@pytest.fixture
def service(tmp_path):
    service = Service(Database(tmp_path / "test.sqlite3"), SecretStore(), FakeProvider())
    connection = Connection(name="Test", provider="chat", base_url="https://example.test/v1", model="fixture-model")
    service.db.execute("INSERT INTO profiles VALUES(?,?,?)", ("profile", connection.model_dump_json(), "session"))
    service.secrets.put("profile", KEY, "session")
    return service


def add_item(service, pid, text, split="dev", provenance="synthetic", restricted=0, group=None, context=""):
    item_id = uid()
    service.db.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        item_id, pid, item_id, group or item_id, text, context, digest(text), provenance, split, restricted, None, None, now()))
    return service.db.one("SELECT * FROM items WHERE id=?", (item_id,))


def wait_runner(runner):
    runner.thread.join(10)
    assert not runner.thread.is_alive()


@pytest.mark.parametrize("ontology", [RESIDENTS, LIBRARY], ids=["residents", "library"])
def test_full_workflow_two_ontologies(service, ontology):
    """Technical synthetic fixtures simulate real-source splits; not empirical validation."""
    pid = service.create_project(ontology["name"])["id"]
    v = service.save_version(pid, ontology)
    category = ontology["concepts"][0]["id"]
    dev = add_item(service, pid, "+ синтетический пример настройки")
    service.save_gold(dev["id"], v["id"], positive(category, dev["text"]))
    for i in range(6):
        add_item(service, pid, ("+" if i % 2 else "-") + f" тестовая запись {i}", "pool", "real", group=f"topic-{i//2}")
    assignment = service.split(pid, .3, 19)
    assert assignment["test"] and assignment["dev"]
    tests = service.db.query("SELECT * FROM items WHERE project_id=? AND split='test'", (pid,))
    for item in tests:
        body = positive(category, item["text"]) if item["text"].startswith("+") else negative()
        service.save_gold(item["id"], v["id"], body)
    frozen = service.freeze(v["id"], "profile", True)
    assert all(example["item_id"] not in {i["id"] for i in tests} for example in frozen["examples"])
    preview = service.job_preview(pid, v["id"], "test")
    job = service.create_job(pid, v["id"], "test", preview["confirmation"])
    runner = JobRunner(service)
    runner.start(job)
    wait_runner(runner)
    results = service.db.query("SELECT * FROM tasks WHERE job_id=?", (job,))
    assert all(t["status"] == "done" for t in results)
    for call in service.provider.calls:
        prompt = json.loads(call)
        assert all(e["text"] not in {i["text"] for i in tests} for e in prompt["approved_examples"])
        assert "gold" not in prompt
    evaluation = evaluate(service.db, job)
    assert all(m["fp"] == m["fn"] == 0 for m in evaluation["metrics"])
    with zipfile.ZipFile(io.BytesIO(export_job(service, job))) as z:
        assert {"annotations.jsonl", "annotations.csv", "entities.csv", "relations.csv", "aggregates.csv"} <= set(z.namelist())
        assert all(KEY.encode() not in z.read(n) for n in z.namelist())
    package = ontology_package(service, v["id"])
    assert "testовая" not in encode(package)
    assert "base_url" not in encode(package) and KEY not in encode(package)
    with pytest.raises(ValueError):
        service.save_gold(tests[0]["id"], v["id"], negative())
    changed = copy.deepcopy(ontology)
    changed["concepts"][0]["include"] += " Уточнение границы."
    next_v = service.save_version(pid, changed, "Изменение смысла", v["id"])
    assert not service.db.query("SELECT 1 FROM gold WHERE version_id=? AND reviewed=1", (next_v["id"],))
    with pytest.raises(ValueError, match="Пересмотрите"):
        service.freeze(next_v["id"], "profile", True)
    # Reopen the actual SQLite database, not an in-memory clone.
    reloaded = Service(Database(service.db.path), SecretStore(), FakeProvider())
    assert reloaded.version(v["id"])["frozen"]
    assert reloaded.db.one("SELECT COUNT(*) n FROM gold WHERE version_id=?", (v["id"],))["n"] == len(tests) + 1
    assert reloaded.version(v["id"])["body"] != reloaded.version(next_v["id"])["body"]


def simple_job(service, n=3):
    pid = service.create_project("Тест")["id"]
    v = service.save_version(pid, RESIDENTS)
    for i in range(n):
        add_item(service, pid, f"- запись {i}")
    service.freeze(v["id"], "profile", True)
    preview = service.job_preview(pid, v["id"], "dev")
    job_id = service.create_job(pid, v["id"], "dev", preview["confirmation"])
    return pid, v, job_id


@pytest.mark.parametrize("failure", ["auth", "rate_limit", "network", "format", "refusal", "server"])
def test_resume_errors_no_duplicate_results(service, failure):
    _, _, job_id = simple_job(service)
    service.provider.fail_once = failure
    runner = JobRunner(service)
    runner.start(job_id)
    wait_runner(runner)
    assert service.db.one("SELECT COUNT(*) n FROM tasks WHERE job_id=? AND status='error'", (job_id,))["n"] == 1
    assert service.db.one("SELECT COUNT(*) n FROM tasks WHERE job_id=? AND status='error' AND result IS NULL", (job_id,))["n"] == 1
    done_before = service.db.query("SELECT item_id,attempts FROM tasks WHERE job_id=? AND status='done'", (job_id,))
    runner.start(job_id, retry_errors=True)
    wait_runner(runner)
    assert service.db.one("SELECT COUNT(*) n FROM tasks WHERE job_id=? AND status='done'", (job_id,))["n"] == 3
    for row in done_before:
        assert service.db.one("SELECT attempts FROM tasks WHERE job_id=? AND item_id=?", (job_id, row["item_id"]))["attempts"] == row["attempts"]


def test_stop_then_resume(service):
    _, _, job_id = simple_job(service)
    service.provider.release = threading.Event()
    runner = JobRunner(service)
    runner.start(job_id)
    assert service.provider.entered.wait(3)
    runner.stop(job_id)
    service.provider.release.set()
    wait_runner(runner)
    assert len(service.provider.calls) == 1
    runner.start(job_id)
    wait_runner(runner)
    assert len(service.provider.calls) == 3


def test_recover_interrupted_job(service):
    _, _, job_id = simple_job(service, 1)
    service.db.execute("UPDATE tasks SET status='running',attempts=1 WHERE job_id=?", (job_id,))
    service.db.execute("UPDATE jobs SET state='running' WHERE id=?", (job_id,))
    runner = JobRunner(service)
    assert service.db.one("SELECT error FROM tasks WHERE job_id=?", (job_id,))["error"] == "interrupted"
    runner.start(job_id, True)
    wait_runner(runner)
    assert service.db.one("SELECT status FROM tasks WHERE job_id=?", (job_id,))["status"] == "done"


def test_evidence_and_relations_validate_exact_source():
    text = "Анна просит Олега."
    ontology = Ontology.model_validate(RESIDENTS)
    a = Annotation.model_validate(positive("request", text))
    validate_annotation(ontology, {"text": text}, a)
    a.labels[0].evidence[0].quote = "несуществующая цитата"
    with pytest.raises(ValueError):
        validate_annotation(ontology, {"text": text}, a)
    good = {**negative(), "status": "labeled", "entities": [
        {"id": "a", "type_id": "person", "span": {"field": "text", "start": 0, "end": 4, "quote": "Анна"}},
        {"id": "b", "type_id": "person", "span": {"field": "text", "start": 12, "end": 17, "quote": "Олега"}},
    ], "relations": [{"type_id": "asks", "source": "a", "target": "b", "evidence": [
        {"field": "text", "start": 0, "end": len(text), "quote": text}]}]}
    validate_annotation(ontology, {"text": text}, Annotation.model_validate(good))
    good["relations"][0]["target"] = "unknown"
    with pytest.raises(ValueError):
        validate_annotation(ontology, {"text": text}, Annotation.model_validate(good))


@pytest.mark.parametrize("status", ["uncertain", "needs_context", "no_match"])
def test_unknown_not_a_negative_label(status):
    assert Annotation.model_validate(negative(status)).status == status
    a = positive("request", "Текст")
    a["status"] = status
    with pytest.raises(ValueError):
        Annotation.model_validate(a)


def test_split_transitive_groups_duplicates_and_shared_context():
    def item(id, group, text, context="", split="pool"):
        return dict(id=id, group_id=group, digest=digest(text), context=context, split=split, provenance="real")
    rows = [item("a", "thread1", "Пример"), item("b", "thread1", "Другой"),
            item("c", "thread2", "  пример  "), item("d", "thread3", "Третий", "Один контекст"),
            item("e", "thread2", "Четвёртый", "Один контекст"), item("f", "separate", "Независимый")]
    result = assign_splits(rows, .5, 10)
    assert len({result[x] for x in "abcde"}) == 1
    assert result["a"] != result["f"]
    rows[0]["split"] = "dev"
    rows[2]["split"] = "test"
    with pytest.raises(ValueError):
        assign_splits(rows, .5, 10)


def test_private_excluded_from_export(service):
    pid, v, job = simple_job(service, 1)
    service.db.execute("UPDATE items SET restricted=1,text='PRIVATE_SENTINEL' WHERE project_id=?", (pid,))
    service.db.execute("UPDATE tasks SET status='done',result=? WHERE job_id=?", (encode(negative()), job))
    with zipfile.ZipFile(io.BytesIO(export_job(service, job))) as z:
        assert all(b"PRIVATE_SENTINEL" not in z.read(n) for n in z.namelist())
        assert json.loads(z.read("manifest.json"))["restricted_excluded"] == 1
    assert b"'=DANGEROUS()" in csv_bytes([{"text": "=DANGEROUS()"}], ["text"])


def test_metrics_keep_failures_and_uncertainty(service):
    pid, v, job = simple_job(service, 3)
    rows = service.db.query("SELECT * FROM items WHERE project_id=? ORDER BY id", (pid,))
    for row in rows:
        service.save_gold(row["id"], v["id"], positive("complaint", row["text"]))
        service.db.execute("UPDATE tasks SET gold_snapshot=?,gold_reviewed=1 WHERE job_id=? AND item_id=?",
                           (encode(positive("complaint", row["text"])), job, row["id"]))
    service.db.execute("UPDATE tasks SET status='error',error='format' WHERE job_id=? AND item_id=?", (job, rows[0]["id"]))
    service.db.execute("UPDATE tasks SET status='done',result=? WHERE job_id=? AND item_id=?", (encode(negative("uncertain")), job, rows[1]["id"]))
    service.db.execute("UPDATE tasks SET status='done',result=? WHERE job_id=? AND item_id=?", (encode(positive("complaint", rows[2]["text"])), job, rows[2]["id"]))
    result = evaluate(service.db, job)
    category = next(m for m in result["metrics"] if m["type_id"] == "complaint")
    assert category["tp"] == 1 and category["missed_unresolved"] == 2
    assert category["recall_all_gold"] == pytest.approx(1/3)


def test_same_data_directory_cannot_be_opened_twice(tmp_path):
    lock = InstanceLock(tmp_path)
    try:
        with pytest.raises(ValueError, match="другом экземпляре"):
            InstanceLock(tmp_path)
    finally:
        lock.close()
    InstanceLock(tmp_path).close()


def test_gold_snapshot_stable_after_expert_revision(service):
    _, v, job = simple_job(service, 1)
    runner = JobRunner(service)
    runner.start(job)
    wait_runner(runner)
    before = evaluate(service.db, job)
    item = service.db.one("SELECT * FROM items LIMIT 1")
    service.save_gold(item["id"], v["id"], negative())
    assert evaluate(service.db, job) == before


def test_import_package_shared_context_rejected(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        client.headers['X-Session-Token'] = client.get('/api/session').json()['token']
        pid = app.state.service.create_project("Leak test")['id']
        add_item(app.state.service, pid, "held out", "test", "real", context="PRIVATE CONTEXT")
        package = {"format": "intuitive-ontology", "schema_version": 1, "ontology": RESIDENTS,
                   "approved_synthetic_examples": [{"text": "different", "context": " private   context ", "annotation": negative()}]}
        result = client.post(f'/api/projects/{pid}/import-ontology', json=package)
        assert result.status_code == 400
        assert not app.state.service.db.query("SELECT 1 FROM items WHERE split='dev'")


def test_busy_runner_does_not_create_phantom_job(service):
    pid, v, job = simple_job(service, 2)
    runner = JobRunner(service)
    service.provider.release = threading.Event()
    runner.start(job)
    assert service.provider.entered.wait(3)
    preview = service.job_preview(pid, v['id'], 'dev')
    try:
        with pytest.raises(ValueError):
            runner.create_and_start(pid, v['id'], 'dev', preview['confirmation'])
        assert len(service.db.query("SELECT id FROM jobs")) == 1
    finally:
        service.provider.release.set()
        wait_runner(runner)


@pytest.mark.parametrize("ext,data", [("txt", "один\nдва".encode()), ("csv", "id,text\n1,текст\n".encode()), ("jsonl", '{"text":"текст"}\n'.encode())])
def test_import_formats(ext, data):
    assert read_records("sample." + ext, data)[0]


def test_import_parquet():
    import pyarrow as pa
    import pyarrow.parquet as pq
    stream = io.BytesIO()
    pq.write_table(pa.table({"id": [1], "text": ["Вымышленный пример"]}), stream)
    assert read_records("sample.parquet", stream.getvalue())[0]["text"] == "Вымышленный пример"


@pytest.mark.parametrize("provider", ["responses", "chat", "anthropic", "gemini"])
def test_provider_contracts(provider):
    captured = []
    answer = encode(negative())
    response = {
        "responses": {"status": "completed", "output": [{"content": [{"type": "output_text", "text": answer}]}]},
        "chat": {"choices": [{"finish_reason": "stop", "message": {"content": answer}}]},
        "anthropic": {"stop_reason": "end_turn", "content": [{"type": "text", "text": answer}]},
        "gemini": {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": answer}]}}]},
    }[provider]
    def handler(request):
        captured.append(request)
        return httpx.Response(200, json=response)
    profile = Connection(name="Test", provider=provider, base_url="https://example.test/v1", model="fixture")
    client = ProviderClient(httpx.MockTransport(handler))
    assert client.generate(profile, KEY, "sys", "prompt", api_schema(Annotation)) == negative()
    request = captured[0]
    body = json.loads(request.content)
    assert KEY not in str(request.url) and KEY not in request.content.decode()
    if provider == "responses":
        assert body["store"] is False and body["text"]["format"]["strict"]
    elif provider == "chat":
        assert body["response_format"]["type"] == "json_schema"
    elif provider == "anthropic":
        assert body["output_config"]["format"]["schema"] and request.headers["x-api-key"] == KEY
    else:
        assert body["generationConfig"]["responseJsonSchema"] and request.headers["x-goog-api-key"] == KEY


@pytest.mark.parametrize("status,code", [(401, "auth"), (403, "auth"), (429, "rate_limit"), (500, "server"), (302, "request")])
def test_provider_errors_redacted(status, code):
    client = ProviderClient(httpx.MockTransport(lambda r: httpx.Response(status, text=KEY)))
    profile = Connection(name="Test", provider="chat", base_url="https://example.test/v1", model="fixture")
    with pytest.raises(ProviderError) as exc:
        client.generate(profile, KEY, "s", "p", {})
    assert exc.value.code == code and KEY not in str(exc.value)


def test_provider_malformed_and_network():
    profile = Connection(name="Test", provider="chat", base_url="https://example.test/v1", model="fixture")
    for handler, code in [(lambda r: httpx.Response(200, text="broken"), "format"),
                          (lambda r: httpx.Response(200, json={"choices": [{"finish_reason": "length"}]}), "truncated"),
                          (lambda r: httpx.Response(200, json={"choices": [{"message": {"refusal": "no"}}]}), "refusal")]:
        with pytest.raises(ProviderError) as exc:
            ProviderClient(httpx.MockTransport(handler)).generate(profile, KEY, "s", "p", {})
        assert exc.value.code == code
    def fail(request):
        raise httpx.ConnectError("private " + KEY)
    with pytest.raises(ProviderError) as exc:
        ProviderClient(httpx.MockTransport(fail)).generate(profile, KEY, "s", "p", {})
    assert exc.value.code == "network" and KEY not in str(exc.value)


def test_api_security_persistence_and_blindness(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as c:
        assert c.get('/api/projects').status_code == 403
        assert c.get('/api/session', headers={"Origin": "https://evil.test"}).status_code == 403
        assert c.get('/api/session', headers={"Host": "evil.test"}).status_code == 403
        c.headers["X-Session-Token"] = c.get('/api/session').json()["token"]
        pid = c.post('/api/projects', json={"name": "API test"}).json()["id"]
        version = c.post(f'/api/projects/{pid}/versions', json={"ontology": RESIDENTS}).json()
        item = add_item(app.state.service, pid, "Синтетический пример")
        app.state.service.db.execute("UPDATE items SET expected=?,synthetic_kind='negative' WHERE id=?", (encode(negative()), item["id"]))
        response = c.get(f'/api/projects/{pid}/items?version_id={version["id"]}').json()
        assert "expected" not in response[0] and "synthetic_kind" not in response[0]
        assert c.get(f'/api/items/{item["id"]}/reveal?version_id={version["id"]}').status_code == 404
        assert c.post(f'/api/items/{item["id"]}/gold', json={"version_id": version["id"], "annotation": negative()}).status_code == 200
        assert c.get(f'/api/items/{item["id"]}/reveal?version_id={version["id"]}').json()["expected"] == negative()
        connection = {"name": "No real requests", "provider": "chat", "base_url": "https://example.test/v1", "model": "fixture"}
        p = c.post('/api/profiles', json={"connection": connection, "key": KEY}).json()
        assert "id" in p and KEY not in c.get('/api/profiles').text
    assert KEY.encode() not in (tmp_path / 'workbench.sqlite3').read_bytes()
    with TestClient(create_app(tmp_path)) as c:
        c.headers['X-Session-Token'] = c.get('/api/session').json()['token']
        assert c.get('/api/projects').json()[0]['name'] == 'API test'
        assert not c.get('/api/profiles').json()[0]['has_key']
