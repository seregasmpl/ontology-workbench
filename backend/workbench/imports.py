from __future__ import annotations

import csv
import hashlib
import io
import json
import random
import unicodedata

MAX_BYTES = 25 * 1024 * 1024
MAX_ROWS = 100_000
MAX_TEXT = 80_000


def normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def digest(text: str) -> str:
    return hashlib.sha256(normalized(text).encode("utf-8")).hexdigest()


def read_records(name: str, data: bytes, txt_mode: str = "lines") -> list[dict]:
    if len(data) > MAX_BYTES:
        raise ValueError("Файл больше 25 МБ. Разделите его перед импортом.")
    suffix = name.lower().rsplit(".", 1)[-1]
    if suffix == "parquet":
        import pyarrow.parquet as pq
        file = pq.ParquetFile(io.BytesIO(data))
        if file.metadata.num_rows > MAX_ROWS:
            raise ValueError("Не больше 100 000 строк за один импорт")
        rows = []
        for batch in file.iter_batches(batch_size=500):
            rows.extend(batch.to_pylist())
            if sum(len(str(r)) for r in rows[-500:]) > MAX_BYTES:
                raise ValueError("Слишком большие распакованные данные")
    else:
        text = data.decode("utf-8-sig")
        if suffix == "txt":
            if txt_mode not in {"lines", "document"}:
                raise ValueError("Единица TXT должна быть строкой или документом")
            rows = [{"id": str(i + 1), "text": t} for i, t in enumerate(
                text.splitlines() if txt_mode == "lines" else [text]) if t.strip()]
        elif suffix == "csv":
            try:
                dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            rows = list(csv.DictReader(io.StringIO(text), dialect=dialect))
        elif suffix == "jsonl":
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            raise ValueError("Поддерживаются TXT, CSV, JSONL и Parquet")
    if not rows or len(rows) > MAX_ROWS or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Нужен непустой набор объектов, не больше 100 000 строк")
    # Keep nested fields available as JSON strings; dates/numbers round-trip predictably.
    result = [{str(k): ("" if v is None else v if isinstance(v, str) else
                       json.dumps(v, ensure_ascii=False, default=str)) for k, v in row.items()} for row in rows]
    if sum(sum(len(v) for v in row.values()) for row in result) > 50_000_000:
        raise ValueError("Распакованные данные больше 50 млн символов")
    return result


def map_records(rows: list[dict], mapping: dict) -> list[dict]:
    text_field = mapping.get("text_field")
    context_field = mapping.get("context_field")
    id_field = mapping.get("id_field")
    group_field = mapping.get("group_field")
    result, ids = [], set()
    for i, row in enumerate(rows):
        if text_field not in row:
            raise ValueError("Поле текста отсутствует в одной из строк")
        source_id = str(row.get(id_field, "") if id_field else i + 1)
        if not source_id or source_id in ids:
            raise ValueError("Идентификаторы должны быть непустыми и уникальными внутри файла")
        ids.add(source_id)
        context = row.get(context_field, "") if context_field else ""
        text = row[text_field]
        if not text.strip() or len(text) + len(context) > MAX_TEXT:
            raise ValueError("Пустая запись или больше 80 000 символов вместе с контекстом")
        group = row.get(group_field, "") if group_field else source_id
        if not group:
            raise ValueError("Поле группы не должно быть пустым")
        result.append({"source_id": source_id, "group_id": group, "text": text,
                       "context": context, "digest": digest(text)})
    return result


def split_components(items: list[dict]):
    """Linear-size union-find; never compare every record against every other record."""
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    seen = {}
    for i, item in enumerate(items):
        keys = [("group", item["group_id"]), ("text", item["digest"])]
        if item.get("context", "").strip():
            keys.append(("context", digest(item["context"])))
        for key in keys:
            if key in seen:
                parent[find(i)] = find(seen[key])
            seen[key] = i
    components = {}
    for i, item in enumerate(items):
        components.setdefault(find(i), []).append(item)
    free, result = [], {}
    for component in components.values():
        fixed = {x["split"] for x in component if x["split"] in {"dev", "test"}}
        if any(x["provenance"] == "synthetic" for x in component):
            fixed.add("dev")
        if len(fixed) > 1:
            raise ValueError("Связанные записи соединяют настройку и проверку. Разделение небезопасно.")
        if fixed:
            for item in component:
                result[item["id"]] = next(iter(fixed))
        else:
            free.append(component)
    return free, result


def inherited_splits(items: list[dict]) -> dict[str, str]:
    free, result = split_components(items)
    for component in free:
        for item in component:
            result[item["id"]] = "pool"
    return result


def assign_splits(items: list[dict], fraction: float, seed: int) -> dict[str, str]:
    """Connected components of explicit groups, exact-normalized duplicates and shared context."""
    if not 0.05 <= fraction <= 0.5:
        raise ValueError("Доля итоговой проверки должна быть от 0.05 до 0.5")
    free, result = split_components(items)
    random.Random(seed).shuffle(free)
    if len(free) < 2 and not any(x["split"] == "test" for x in items):
        raise ValueError("Нужны хотя бы две независимые группы реальных текстов")
    count = max(1, round(len(free) * fraction)) if free else 0
    if len(free) > 1:
        count = min(count, len(free) - 1)
    for i, component in enumerate(free):
        for item in component:
            result[item["id"]] = "test" if i < count else "dev"
    return result
