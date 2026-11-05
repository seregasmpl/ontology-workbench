from __future__ import annotations

import json
import secrets
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .demos import templates
from .evaluation import evaluate
from .exports import export_job, ontology_package
from .imports import MAX_BYTES, digest, inherited_splits, map_records, read_records
from .jobs import JobRunner, job_summary
from .locking import InstanceLock
from .models import Annotation, Connection, Ontology, validate_annotation
from .providers import ProviderClient, ProviderError, SecretStore
from .service import Service, ensure_new_dev_safe
from .storage import Database, encode, now, uid


def frontend_path():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "frontend"
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"


def create_app(data_dir: Path, provider=None, secret_store=None) -> FastAPI:
    instance_lock = InstanceLock(Path(data_dir))
    db = Database(Path(data_dir) / "workbench.sqlite3")
    vault = secret_store or SecretStore()
    service = Service(db, vault, provider or ProviderClient())
    runner = JobRunner(service)
    session_token = secrets.token_urlsafe(32)
    uploads = {}

    @asynccontextmanager
    async def lifespan(app):
        yield
        runner.shutdown()
        vault.clear_session()
        if runner.thread and runner.thread.is_alive():
            def unlock_after_worker():
                runner.thread.join()
                instance_lock.close()
            threading.Thread(target=unlock_after_worker, daemon=True).start()
        else:
            instance_lock.close()

    app = FastAPI(title="Конструктор интуитивных онтологий", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service, app.state.runner = service, runner
    app.state.instance_lock = instance_lock
    app.state.shutdown_callback = None

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.headers.get("host", "")
        hostname = urlsplit("http://" + host).hostname
        origin = request.headers.get("origin")
        if hostname not in {"127.0.0.1", "localhost", "::1", "testserver"}:
            return JSONResponse({"detail": "Доступ только с локального адреса"}, status_code=403)
        if origin and (urlsplit(origin).netloc != host or urlsplit(origin).scheme not in {"http", "https"}):
            return JSONResponse({"detail": "Запрос с другого сайта отклонён"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Запрос с другого сайта отклонён"}, status_code=403)
        try:
            too_large = int(request.headers.get("content-length", "0")) > MAX_BYTES + 100_000
        except ValueError:
            too_large = True
        if too_large:
            return JSONResponse({"detail": "Запрос слишком большой"}, status_code=413)
        if request.url.path.startswith("/api/") and request.url.path != "/api/session":
            if not secrets.compare_digest(request.headers.get("x-session-token", ""), session_token):
                return JSONResponse({"detail": "Сеанс истёк. Обновите страницу."}, status_code=403)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                 "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=502)

    @app.exception_handler(RequestValidationError)
    async def request_error(request, exc):
        # Pydantic's default error representation contains input values, possibly API keys.
        return JSONResponse({"detail": "Неверная структура запроса"}, status_code=422)

    @app.exception_handler(ValidationError)
    async def model_error(request, exc):
        messages = [".".join(map(str, e["loc"])) + ": " + e["msg"] for e in exc.errors(include_input=False, include_url=False)]
        return JSONResponse({"detail": "; ".join(messages)}, status_code=422)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(LookupError)
    async def missing(request, exc):
        return JSONResponse({"detail": "Объект не найден"}, status_code=404)

    @app.get("/api/session")
    def session():
        return {"token": session_token, "version": "0.1.0", "data_dir": str(Path(data_dir).resolve())}

    @app.get("/api/projects")
    def projects():
        return db.query("SELECT * FROM projects ORDER BY created DESC")

    @app.get("/api/templates")
    def get_templates():
        return templates()

    @app.post("/api/projects")
    def new_project(payload: dict):
        return service.create_project(payload.get("name"))

    @app.get("/api/projects/{project_id}")
    def project(project_id: str):
        result = service.project(project_id)
        versions = db.query("SELECT id,revision,frozen,created,note FROM versions WHERE project_id=? ORDER BY revision DESC", (project_id,))
        result.update(versions=versions, counts=db.query(
            "SELECT split,provenance,COUNT(*) count FROM items WHERE project_id=? GROUP BY split,provenance", (project_id,)),
            jobs=[job_summary(db, row["id"]) for row in db.query(
                "SELECT id FROM jobs WHERE project_id=? ORDER BY created DESC", (project_id,))])
        return result

    @app.post("/api/projects/{project_id}/versions")
    def new_version(project_id: str, payload: dict):
        return service.save_version(project_id, payload["ontology"], payload.get("note", ""), payload.get("parent_id"))

    @app.get("/api/versions/{version_id}")
    def version(version_id: str):
        return service.version(version_id)

    @app.post("/api/versions/{version_id}/freeze")
    def freeze(version_id: str, payload: dict):
        return service.freeze(version_id, payload["profile_id"], payload.get("unit_confirmed") is True)

    @app.get("/api/versions/{version_id}/examples-preview")
    def preview_examples(version_id: str):
        service.version(version_id)
        return service.dev_examples(version_id)

    @app.get("/api/profiles")
    def profiles():
        result = []
        for row in db.query("SELECT * FROM profiles"):
            try:
                vault.get(row["id"], row["secret_mode"])
                has_key = True
            except ProviderError:
                has_key = False
            result.append({"id": row["id"], **json.loads(row["body"]), "secret_mode": row["secret_mode"], "has_key": has_key})
        return result

    @app.post("/api/profiles")
    def save_profile(payload: dict):
        connection = Connection.model_validate(payload["connection"])
        mode = payload.get("secret_mode", "session")
        if mode not in {"session", "system"}:
            raise ValueError("Неизвестный режим хранения ключа")
        profile_id = uid()
        key = payload.get("key")
        if key:
            vault.put(profile_id, key, mode)
        db.execute("INSERT INTO profiles VALUES(?,?,?)", (profile_id, connection.model_dump_json(), mode))
        return {"id": profile_id}

    @app.post("/api/profiles/{profile_id}/key")
    def set_key(profile_id: str, payload: dict):
        _, mode = service.profile(profile_id)
        vault.put(profile_id, payload.get("key", ""), mode)
        return {"saved": True}

    @app.delete("/api/profiles/{profile_id}/key")
    def delete_key(profile_id: str):
        _, mode = service.profile(profile_id)
        vault.forget(profile_id, mode)
        return {"removed": True}

    def consent(payload):
        if payload.get("consent") is not True:
            raise ValueError("Подтвердите отправку данных выбранному провайдеру")

    @app.post("/api/ai/ontology")
    def ai_ontology(payload: dict):
        consent(payload)
        description = payload.get("description", "")
        if not isinstance(description, str) or not 10 <= len(description) <= 40000:
            raise ValueError("Описание должно содержать от 10 до 40 000 символов")
        return service.propose_ontology(payload["profile_id"], description)

    @app.post("/api/versions/{version_id}/generate")
    def ai_generate(version_id: str, payload: dict):
        consent(payload)
        return service.generate_examples(version_id, payload["profile_id"], int(payload.get("count", 6)))

    @app.post("/api/versions/{version_id}/suggest")
    def ai_suggest(version_id: str, payload: dict):
        consent(payload)
        return service.suggest_rules(version_id, payload["profile_id"])

    @app.post("/api/projects/{project_id}/upload")
    async def upload(project_id: str, file: UploadFile = File(...), txt_mode: str = Form("lines")):
        service.project(project_id)
        data = await file.read(MAX_BYTES + 1)
        try:
            rows = read_records(file.filename or "", data, txt_mode)
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError("Не удалось прочитать UTF-8 файл. Проверьте кодировку и формат.")
        for key in list(uploads):
            if time.monotonic() - uploads[key][0] > 1800:
                del uploads[key]
        if len(uploads) >= 3:
            del uploads[next(iter(uploads))]
        upload_id = uid()
        uploads[upload_id] = (time.monotonic(), project_id, rows)
        return {"upload_id": upload_id, "count": len(rows), "fields": list(dict.fromkeys(k for row in rows[:100] for k in row)),
                "preview": rows[:3]}

    @app.post("/api/projects/{project_id}/import")
    def import_data(project_id: str, payload: dict):
        upload_id = payload["upload_id"]
        cached = uploads.get(upload_id)
        if not cached or cached[1] != project_id:
            raise ValueError("Предпросмотр истёк. Загрузите файл повторно.")
        if payload.get("unit_confirmed") is not True or not str(payload.get("unit", "")).strip():
            raise ValueError("Явно укажите и подтвердите единицу анализа")
        if payload.get("grouping_confirmed") is not True:
            raise ValueError("Подтвердите поле связи сообщений или независимость записей")
        rows = map_records(cached[2], payload)
        provenance = payload.get("provenance", "real")
        if provenance not in {"real", "synthetic"}:
            raise ValueError("Укажите происхождение данных")
        restricted = payload.get("restricted", True) is not False
        existing = db.query("SELECT * FROM items WHERE project_id=?", (project_id,))
        prepared = [{"id": uid(), **row, "split": "dev" if provenance == "synthetic" else "pool",
                     "provenance": provenance} for row in rows]
        # Resolve transitive links before committing any record, including links
        # introduced late in the same file. Existing assignments are immutable.
        all_splits = inherited_splits(existing + prepared)
        with db.connect() as conn:
            for row in prepared:
                item_id = row["id"]
                conn.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    item_id, project_id, row["source_id"], row["group_id"], row["text"], row["context"], row["digest"],
                    provenance, all_splits[item_id], int(restricted), None, None, now()))
            for row in existing:
                if row["split"] != all_splits[row["id"]]:
                    conn.execute("UPDATE items SET split=? WHERE id=?", (all_splits[row["id"]], row["id"]))
        del uploads[upload_id]
        db.decision(project_id, "import", {k: payload.get(k) for k in (
            "text_field", "id_field", "context_field", "group_field", "unit", "grouping_confirmed", "provenance", "restricted")})
        return {"count": len(rows)}

    @app.post("/api/projects/{project_id}/split")
    def split(project_id: str, payload: dict):
        return service.split(project_id, float(payload.get("fraction", 0.25)), int(payload.get("seed", 42)))

    @app.get("/api/projects/{project_id}/items")
    def items(project_id: str, version_id: str, split: str = "dev", offset: int = 0, search: str = ""):
        if service.version(version_id)["project_id"] != project_id:
            raise ValueError("Другая версия проекта")
        if offset < 0 or split not in {"dev", "test", "pool"}:
            raise ValueError("Неверные параметры списка")
        rows = db.query("SELECT i.id,i.source_id,i.group_id,i.text,i.context,i.split,i.provenance,i.restricted,"
                        "g.body gold,g.reviewed FROM items i LEFT JOIN gold g ON g.item_id=i.id AND g.version_id=? "
                        "WHERE i.project_id=? AND i.split=? AND (i.text LIKE ? OR i.source_id LIKE ?) "
                        "ORDER BY i.created,i.id LIMIT 100 OFFSET ?",
                        (version_id, project_id, split, "%" + search[:200] + "%", "%" + search[:200] + "%", offset))
        for row in rows:
            row["gold"] = json.loads(row["gold"]) if row["gold"] else None
        return rows

    @app.post("/api/items/{item_id}/gold")
    def gold(item_id: str, payload: dict):
        return service.save_gold(item_id, payload["version_id"], payload["annotation"])

    @app.get("/api/items/{item_id}/reveal")
    def reveal(item_id: str, version_id: str):
        db.one("SELECT 1 FROM gold WHERE item_id=? AND version_id=? AND reviewed=1", (item_id, version_id))
        row = db.one("SELECT expected,synthetic_kind FROM items WHERE id=?", (item_id,))
        return {"expected": json.loads(row["expected"]) if row["expected"] else None, "kind": row["synthetic_kind"]}

    @app.post("/api/projects/{project_id}/jobs/preview")
    def preview_job(project_id: str, payload: dict):
        return service.job_preview(project_id, payload["version_id"], payload["split"])

    @app.post("/api/projects/{project_id}/jobs")
    def start_job(project_id: str, payload: dict):
        consent(payload)
        job_id = runner.create_and_start(project_id, payload["version_id"], payload["split"], payload["confirmation"])
        return job_summary(db, job_id)

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        return job_summary(db, job_id)

    @app.get("/api/jobs/{job_id}/results")
    def job_results(job_id: str, offset: int = 0, only_errors: bool = False):
        job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if job["split"] == "test":
            db.execute("UPDATE jobs SET evaluation_seen=1 WHERE id=?", (job_id,))
        condition = " AND (t.status='error' OR (t.result IS NOT NULL AND json_extract(t.result,'$.status') IN ('uncertain','needs_context')))" if only_errors else ""
        rows = db.query("SELECT t.*,i.source_id,i.text,i.context FROM tasks t JOIN items i ON i.id=t.item_id "
                        "WHERE t.job_id=?" + condition + " ORDER BY i.source_id LIMIT 100 OFFSET ?", (job_id, max(0, offset)))
        for row in rows:
            row["result"] = json.loads(row["result"]) if row["result"] else None
        return rows

    @app.post("/api/jobs/{job_id}/stop")
    def stop(job_id: str):
        runner.stop(job_id)
        return job_summary(db, job_id)

    @app.post("/api/jobs/{job_id}/resume")
    def resume(job_id: str, payload: dict):
        consent(payload)
        runner.start(job_id, payload.get("retry_errors") is True)
        return job_summary(db, job_id)

    @app.get("/api/jobs/{job_id}/evaluation")
    def evaluation(job_id: str):
        return evaluate(db, job_id)

    @app.get("/api/versions/{version_id}/export")
    def export_ontology(version_id: str):
        package = ontology_package(service, version_id)
        return Response(encode(package), media_type="application/json",
                        headers={"Content-Disposition": "attachment; filename=ontology-v1.json"})

    @app.post("/api/projects/{project_id}/import-ontology")
    def import_ontology(project_id: str, payload: dict):
        if payload.get("format") != "intuitive-ontology" or payload.get("schema_version") != 1:
            raise ValueError("Неизвестная версия пакета онтологии")
        # Imported annotations are intentionally not trusted as gold for this expert.
        ontology = Ontology.model_validate(payload["ontology"])
        examples = payload.get("approved_synthetic_examples", [])
        if len(examples) > 100:
            raise ValueError("Слишком много примеров в пакете")
        for example in examples:
            validate_annotation(ontology, example, Annotation.model_validate(example["annotation"]))
        with db.connect() as conn:
            for example in examples:
                ensure_new_dev_safe(conn, project_id, example["text"], example.get("context", ""))
        version = service.save_version(project_id, ontology.model_dump(), "Импорт пакета; требуется проверка эксперта")
        with db.connect() as conn:
            for example in examples:
                item_id = uid()
                ensure_new_dev_safe(conn, project_id, example["text"], example.get("context", ""))
                conn.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (item_id, project_id, item_id,
                             "synthetic:" + item_id, example["text"], example.get("context", ""), digest(example["text"]),
                             "synthetic", "dev", 0, "imported", encode(example["annotation"]), now()))
        return version

    @app.post("/api/jobs/{job_id}/export")
    def export_results(job_id: str, payload: dict):
        if payload.get("include_restricted") and payload.get("confirmed") is not True:
            raise ValueError("Подтвердите экспорт закрытых материалов")
        return Response(export_job(service, job_id, payload.get("include_restricted") is True,
                                   payload.get("include_text") is True), media_type="application/zip",
                        headers={"Content-Disposition": "attachment; filename=annotations.zip"})

    @app.post("/api/shutdown")
    def shutdown():
        if app.state.shutdown_callback:
            app.state.shutdown_callback()
        return {"stopping": True}

    dist = frontend_path()
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}")
    def index(path: str):
        if path.startswith("api/"):
            return JSONResponse({"detail": "Маршрут не найден"}, status_code=404)
        if (dist / "index.html").is_file():
            return FileResponse(dist / "index.html")
        return Response("Интерфейс не собран. Выполните npm install и npm run build в frontend.", status_code=503)

    return app
