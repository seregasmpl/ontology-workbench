from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Concept(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["category", "entity"] = "category"
    definition: str = Field(min_length=1, max_length=6000)
    include: str = Field(min_length=1, max_length=6000)
    exclude: str = Field(min_length=1, max_length=6000)
    context: str = Field(default="", max_length=4000)


class RelationType(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=200)
    source_type: str
    target_type: str
    definition: str = Field(min_length=1, max_length=6000)
    include: str = Field(min_length=1, max_length=6000)
    exclude: str = Field(min_length=1, max_length=6000)


class Ontology(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    task: str = Field(min_length=1, max_length=12000)
    unit: str = Field(min_length=1, max_length=2000)
    context_policy: str = Field(default="Только переданный контекст; не домысливать.", max_length=4000)
    concepts: list[Concept] = Field(min_length=1, max_length=100)
    relations: list[RelationType] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def consistent(self):
        ids = [c.id for c in self.concepts] + [r.id for r in self.relations]
        if len(ids) != len(set(ids)):
            raise ValueError("Идентификаторы понятий и связей должны быть уникальны")
        entities = {c.id for c in self.concepts if c.kind == "entity"}
        for relation in self.relations:
            if relation.source_type not in entities or relation.target_type not in entities:
                raise ValueError("Концы связи должны ссылаться на типы сущностей")
        return self


class Span(StrictModel):
    field: Literal["text", "context"] = "text"
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    quote: str = Field(min_length=1)


class CategoryLabel(StrictModel):
    concept_id: str
    evidence: list[Span] = Field(min_length=1)


class Entity(StrictModel):
    id: str = Field(min_length=1, max_length=64)
    type_id: str
    span: Span


class Relation(StrictModel):
    type_id: str
    source: str
    target: str
    evidence: list[Span] = Field(min_length=1)


class Annotation(StrictModel):
    status: Literal["labeled", "no_match", "needs_context", "uncertain"]
    labels: list[CategoryLabel] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    rationale: str = Field(min_length=1, max_length=6000)

    @model_validator(mode="after")
    def status_content(self):
        content = self.labels or self.entities or self.relations
        if self.status != "labeled" and content:
            raise ValueError("Для неопределённого ответа или отсутствия категории метки должны быть пусты")
        if self.status == "labeled" and not content:
            raise ValueError("Укажите хотя бы категорию, сущность или связь")
        if len({x.concept_id for x in self.labels}) != len(self.labels):
            raise ValueError("Категория указана дважды")
        if len({x.id for x in self.entities}) != len(self.entities):
            raise ValueError("Идентификатор сущности указан дважды")
        rels = [(r.type_id, r.source, r.target) for r in self.relations]
        if len(rels) != len(set(rels)):
            raise ValueError("Связь указана дважды")
        return self


def validate_annotation(ontology: Ontology, item: dict, annotation: Annotation) -> Annotation:
    """Verify references and exact Unicode code-point offsets, never infer negative labels."""
    concepts = {c.id: c for c in ontology.concepts}
    relation_types = {r.id: r for r in ontology.relations}

    def span_ok(span: Span):
        text = item.get(span.field, "")
        if span.end <= span.start or span.end > len(text) or text[span.start:span.end] != span.quote:
            raise ValueError("Фрагмент не совпадает с исходным текстом или его границами")

    for label in annotation.labels:
        if label.concept_id not in concepts or concepts[label.concept_id].kind != "category":
            raise ValueError("Неизвестная категория")
        for span in label.evidence:
            span_ok(span)
    entities = {e.id: e for e in annotation.entities}
    for entity in annotation.entities:
        if entity.type_id not in concepts or concepts[entity.type_id].kind != "entity":
            raise ValueError("Неизвестный тип сущности")
        span_ok(entity.span)
    for relation in annotation.relations:
        kind = relation_types.get(relation.type_id)
        source, target = entities.get(relation.source), entities.get(relation.target)
        if not kind or not source or not target:
            raise ValueError("Неизвестная связь или её участник")
        if source.type_id != kind.source_type or target.type_id != kind.target_type:
            raise ValueError("Типы участников не соответствуют определению связи")
        for span in relation.evidence:
            span_ok(span)
    return annotation


class Connection(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    provider: Literal["responses", "chat", "anthropic", "gemini"]
    base_url: str
    model: str = Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9._:/-]+$")
    structured: bool = True
    max_tokens: int = Field(default=4096, ge=256, le=64000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    timeout: int = Field(default=120, ge=10, le=600)

    @field_validator("base_url")
    @classmethod
    def safe_url(cls, value):
        parsed = urlsplit(value)
        if (parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        )) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Нужен HTTPS-адрес без ключа, логина, query и fragment; HTTP разрешён только localhost")
        return value.rstrip("/")


class SyntheticExample(StrictModel):
    kind: Literal["positive", "negative", "boundary"]
    text: str = Field(min_length=1, max_length=12000)
    context: str = Field(default="", max_length=12000)
    expected: Annotation


class ExampleBatch(StrictModel):
    examples: list[SyntheticExample] = Field(min_length=1, max_length=12)


class RuleSuggestion(StrictModel):
    explanation: str = Field(min_length=1, max_length=6000)
    ontology: Ontology


def api_schema(model: type[BaseModel]) -> dict:
    schema = model.model_json_schema()

    def strict(node):
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for child in node.values():
                strict(child)
        elif isinstance(node, list):
            for child in node:
                strict(child)

    strict(schema)
    return schema
