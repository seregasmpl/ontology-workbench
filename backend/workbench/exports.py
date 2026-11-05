from __future__ import annotations

import csv
import io
import json
import zipfile

from .storage import encode


def csv_bytes(rows, fields):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        safe = {}
        for k in fields:
            value = row.get(k, "")
            if isinstance(value, (dict, list)):
                value = encode(value)
            # Spreadsheet formula injection protection (JSONL preserves the exact original).
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
                value = "'" + value
            safe[k] = value
        writer.writerow(safe)
    return output.getvalue().encode("utf-8-sig")


def ontology_package(service, version_id):
    version = service.version(version_id)
    if not version["frozen"]:
        raise ValueError("Экспортируйте зафиксированную версию")
    config = version["parameters"]["connection"]
    return {"format": "intuitive-ontology", "schema_version": 1, "revision": version["revision"],
            "ontology": version["body"], "prompt_format": 1,
            "model_parameters": {k: config[k] for k in ("provider", "model", "structured", "max_tokens", "temperature")},
            "approved_synthetic_examples": [{"text": e["text"], "context": e["context"], "annotation": e["annotation"]}
                                            for e in version["examples"] if e["provenance"] == "synthetic" and not e["restricted"]],
            "excluded": "Ключи, адреса подключений, реальные и проверочные тексты, закрытые материалы исключены."}


def export_job(service, job_id, include_restricted=False, include_text=False):
    db = service.db
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    rows = db.query("SELECT t.*,i.source_id,i.group_id,i.text,i.context,i.provenance,i.restricted "
                    "FROM tasks t JOIN items i ON i.id=t.item_id WHERE t.job_id=? ORDER BY i.source_id", (job_id,))
    annotations, entities, relations = [], [], []
    excluded = 0
    for row in rows:
        if row["restricted"] and not include_restricted:
            excluded += 1
            continue
        body = json.loads(row["result"]) if row["result"] else None
        entry = {"item_id": row["item_id"], "source_id": row["source_id"], "group_id": row["group_id"],
                 "version_id": job["version_id"], "provenance": row["provenance"], "split": job["split"],
                 "processing_status": row["status"], "error": row["error"], "annotation": body}
        if include_text:
            entry.update(text=row["text"], context=row["context"])
        annotations.append(entry)
        if body:
            for entity in body["entities"]:
                entities.append({"item_id": row["item_id"], "source_id": row["source_id"], **entity,
                                 **entity["span"]})
            for relation in body["relations"]:
                relations.append({"item_id": row["item_id"], "source_id": row["source_id"], **relation})
    # No prompts / private connection settings in the archive.
    manifest = {"schema_version": 1, "job_id": job_id, "version_id": job["version_id"],
                "exported": len(annotations), "restricted_excluded": excluded,
                "includes_text": include_text, "includes_restricted": include_restricted,
                "evidence_warning": "Доказательные фрагменты присутствуют и без полного текста.",
                "csv_warning": "Опасные для таблиц начальные символы экранированы апострофом. JSONL не изменяет строки."}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", encode(manifest))
        archive.writestr("annotations.jsonl", "\n".join(encode(a) for a in annotations))
        fields = ["item_id", "source_id", "group_id", "version_id", "provenance", "split", "processing_status", "error", "annotation"]
        if include_text:
            fields += ["text", "context"]
        archive.writestr("annotations.csv", csv_bytes(annotations, fields))
        archive.writestr("entities.csv", csv_bytes(entities, ["item_id", "source_id", "id", "type_id", "field", "start", "end", "quote"]))
        archive.writestr("relations.csv", csv_bytes(relations, ["item_id", "source_id", "type_id", "source", "target", "evidence"]))
        counts = {}
        for entry in annotations:
            body = entry["annotation"]
            status = body["status"] if body else entry["processing_status"]
            counts[status] = counts.get(status, 0) + 1
            if body:
                for label in body["labels"]:
                    name = "category:" + label["concept_id"]
                    counts[name] = counts.get(name, 0) + 1
                for relation in body["relations"]:
                    name = "relation:" + relation["type_id"]
                    counts[name] = counts.get(name, 0) + 1
        archive.writestr("aggregates.csv", csv_bytes([{"measure": k, "count": v} for k, v in counts.items()], ["measure", "count"]))
    return output.getvalue()
