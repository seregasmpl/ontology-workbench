from __future__ import annotations

import json
from collections import Counter


def relation_keys(annotation):
    entities = {e["id"]: (e["type_id"], e["span"]["field"], e["span"]["start"], e["span"]["end"])
                for e in annotation.get("entities", [])}
    return {(r["type_id"], entities[r["source"]], entities[r["target"]]) for r in annotation.get("relations", [])}


def evaluate(db, job_id):
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    ontology = json.loads(job["prompt"])["ontology"]
    rows = db.query("SELECT *,gold_snapshot gold,gold_reviewed reviewed FROM tasks WHERE job_id=?", (job_id,))
    metrics = {("category", c["id"]): Counter() for c in ontology["concepts"] if c["kind"] == "category"}
    metrics.update({("relation", r["id"]): Counter() for r in ontology["relations"]})
    totals, statuses, errors = Counter(), Counter(), Counter()
    for row in rows:
        totals["records"] += 1
        if row["error"]:
            errors[row["error"]] += 1
        prediction = json.loads(row["result"]) if row["result"] else None
        statuses[prediction["status"] if prediction else row["status"]] += 1
        if not row["gold"] or not row["reviewed"]:
            totals["without_reviewed_gold"] += 1
            continue
        gold = json.loads(row["gold"])
        if gold["status"] not in {"labeled", "no_match"}:
            totals["gold_unresolved"] += 1
            continue
        totals["gold_resolved"] += 1
        resolved = prediction and prediction["status"] in {"labeled", "no_match"}
        totals["prediction_resolved_on_gold"] += int(bool(resolved))
        gold_labels = {a["concept_id"] for a in gold["labels"]}
        predicted_labels = {a["concept_id"] for a in prediction["labels"]} if resolved else set()
        gold_relations = relation_keys(gold)
        predicted_relations = relation_keys(prediction) if resolved else set()
        for (kind, type_id), metric in metrics.items():
            wanted = ({type_id} if type_id in gold_labels else set()) if kind == "category" else {
                r for r in gold_relations if r[0] == type_id}
            got = ({type_id} if type_id in predicted_labels else set()) if kind == "category" else {
                r for r in predicted_relations if r[0] == type_id}
            metric["reference_positive"] += len(wanted)
            if resolved:
                metric["tp"] += len(wanted & got)
                metric["fp"] += len(got - wanted)
                metric["fn"] += len(wanted - got)
                if not wanted and not got:
                    metric["tn_records"] += 1
            else:
                metric["unresolved_records"] += 1
                metric["missed_unresolved"] += len(wanted)
    result = []
    for (kind, type_id), values in metrics.items():
        row = {k: values[k] for k in ("tp", "fp", "fn", "tn_records", "reference_positive",
                                      "unresolved_records", "missed_unresolved")}
        row.update(kind=kind, type_id=type_id)
        tp, fp, fn = row["tp"], row["fp"], row["fn"]
        row["precision_resolved"] = tp / (tp + fp) if tp + fp else None
        row["recall_resolved"] = tp / (tp + fn) if tp + fn else None
        row["recall_all_gold"] = tp / row["reference_positive"] if row["reference_positive"] else None
        result.append(row)
    if job["split"] == "test":
        db.execute("UPDATE jobs SET evaluation_seen=1 WHERE id=?", (job_id,))
    previous = db.query("SELECT id FROM jobs WHERE project_id=? AND split='test' AND id<>?",
                        (job["project_id"], job_id))
    return {"metrics": result, "totals": dict(totals), "statuses": dict(statuses), "errors": dict(errors),
            "reused_test": bool(previous) and job["split"] == "test",
            "note": "Сравнение с эталоном одного эксперта, не доказательство истинности. Precision/recall resolved "
                    "считаются по определённым ответам. Recall all gold включает пропуски из-за ошибок и неопределённости. "
                    "Связи сравниваются по типу, направлению, типам и точным границам участников."}
