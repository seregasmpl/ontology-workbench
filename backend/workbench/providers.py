from __future__ import annotations

import json
import threading
from urllib.parse import quote

import httpx
import keyring

from .models import Connection


class ProviderError(Exception):
    MESSAGES = {
        "auth": "Ключ отклонён или нет доступа к модели. Проверьте подключение.",
        "rate_limit": "Провайдер ограничил частоту запросов или исчерпан баланс. Продолжите позже.",
        "network": "Сеть недоступна или время ожидания истекло. Запрос мог быть тарифицирован.",
        "server": "Ошибка сервера провайдера. Продолжите позже.",
        "request": "Провайдер отклонил запрос. Проверьте адрес, модель и поддержку JSON Schema.",
        "format": "Ответ не соответствует ожидаемому JSON-формату.",
        "refusal": "Модель отказалась отвечать. Это не отрицательная метка.",
        "truncated": "Ответ оборван по лимиту токенов. Увеличьте лимит в новом подключении.",
        "no_key": "Введите API-ключ для этого подключения.",
        "secret_store": "Системное хранилище секретов недоступно. Выберите хранение на время сеанса.",
    }

    def __init__(self, code):
        self.code = code
        super().__init__(self.MESSAGES.get(code, "Ошибка внешнего сервиса"))


class SecretStore:
    SERVICE = "ontology-workbench"

    def __init__(self):
        self.memory: dict[str, str] = {}
        self.lock = threading.Lock()

    def _secure_backend(self):
        backend = keyring.get_keyring()
        module = type(backend).__module__
        if not any(name in module for name in ("Windows", "macOS", "SecretService")):
            raise ProviderError("secret_store")
        return backend

    def put(self, profile_id: str, key: str, mode: str):
        if not key or len(key) > 4096 or "\n" in key or "\r" in key:
            raise ValueError("Введите корректный ключ")
        if mode == "system":
            try:
                self._secure_backend().set_password(self.SERVICE, profile_id, key)
            except Exception as exc:
                raise ProviderError("secret_store") from exc
        with self.lock:
            self.memory[profile_id] = key

    def get(self, profile_id: str, mode: str) -> str:
        with self.lock:
            value = self.memory.get(profile_id)
        if not value and mode == "system":
            try:
                value = self._secure_backend().get_password(self.SERVICE, profile_id)
            except Exception as exc:
                raise ProviderError("secret_store") from exc
        if not value:
            raise ProviderError("no_key")
        return value

    def forget(self, profile_id: str, mode: str):
        if mode == "system":
            try:
                backend = self._secure_backend()
                if backend.get_password(self.SERVICE, profile_id):
                    backend.delete_password(self.SERVICE, profile_id)
            except Exception as exc:
                raise ProviderError("secret_store") from exc
        with self.lock:
            self.memory.pop(profile_id, None)

    def clear_session(self):
        with self.lock:
            self.memory.clear()


class ProviderClient:
    """No SDK telemetry, redirects, environment proxies, raw response logging or tools."""

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.transport = transport

    def generate(self, profile: Connection, key: str, system: str, prompt: str, schema: dict):
        system += "\nReturn one JSON object, no markdown. JSON schema: " + json.dumps(schema, ensure_ascii=False)
        headers = {"Content-Type": "application/json"}
        p = profile
        body = {"model": p.model}
        if p.provider == "responses":
            url = p.base_url + "/responses"
            headers["Authorization"] = "Bearer " + key
            body.update(instructions=system, input=prompt, max_output_tokens=p.max_tokens, store=False)
            body["text"] = {"format": {"type": "json_schema", "name": "annotation",
                                       "schema": schema, "strict": True}} if p.structured else {
                                           "format": {"type": "json_object"}}
        elif p.provider == "chat":
            url = p.base_url + "/chat/completions"
            headers["Authorization"] = "Bearer " + key
            body.update(messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                        max_tokens=p.max_tokens)
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "annotation", "schema": schema, "strict": True}} if p.structured else {"type": "json_object"}
        elif p.provider == "anthropic":
            url = p.base_url + "/messages"
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
            body.update(system=system, messages=[{"role": "user", "content": prompt}], max_tokens=p.max_tokens)
            if p.structured:
                body["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
        else:
            model = p.model.removeprefix("models/")
            url = p.base_url + "/models/" + quote(model, safe="-._") + ":generateContent"
            headers["x-goog-api-key"] = key
            body = {"systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"maxOutputTokens": p.max_tokens, "responseMimeType": "application/json"}}
            if p.structured:
                body["generationConfig"]["responseJsonSchema"] = schema
        if p.temperature is not None:
            if p.provider == "gemini":
                body["generationConfig"]["temperature"] = p.temperature
            else:
                body["temperature"] = p.temperature
        try:
            with httpx.Client(transport=self.transport, timeout=p.timeout, follow_redirects=False,
                              trust_env=False) as client:
                with client.stream("POST", url, headers=headers, json=body) as response:
                    code = response.status_code
                    if code in {401, 403}:
                        raise ProviderError("auth")
                    if code == 429:
                        raise ProviderError("rate_limit")
                    if code >= 500:
                        raise ProviderError("server")
                    if code != 200:
                        raise ProviderError("request")
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 4_000_000:
                            raise ProviderError("format")
                    data = json.loads(raw)
        except httpx.RequestError as exc:
            raise ProviderError("network") from exc
        except (ValueError, UnicodeError) as exc:
            raise ProviderError("format") from exc

        try:
            if p.provider == "responses":
                if data.get("status") == "incomplete":
                    raise ProviderError("truncated")
                if data.get("status") in {"failed", "cancelled"}:
                    raise ProviderError("server")
                blocks = [b for out in data.get("output", []) for b in out.get("content", [])]
                if any(b.get("type") == "refusal" for b in blocks):
                    raise ProviderError("refusal")
                text = "".join(b["text"] for b in blocks if b.get("type") == "output_text")
            elif p.provider == "chat":
                choice = data["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise ProviderError("truncated")
                if choice.get("finish_reason") == "content_filter" or choice["message"].get("refusal"):
                    raise ProviderError("refusal")
                text = choice["message"]["content"]
            elif p.provider == "anthropic":
                if data.get("stop_reason") == "refusal":
                    raise ProviderError("refusal")
                if data.get("stop_reason") == "max_tokens":
                    raise ProviderError("truncated")
                text = "".join(b["text"] for b in data["content"] if b.get("type") == "text")
            else:
                if data.get("promptFeedback", {}).get("blockReason"):
                    raise ProviderError("refusal")
                candidate = data["candidates"][0]
                reason = candidate.get("finishReason")
                if reason == "MAX_TOKENS":
                    raise ProviderError("truncated")
                if reason and reason != "STOP":
                    raise ProviderError("refusal")
                text = "".join(b.get("text", "") for b in candidate["content"]["parts"] if not b.get("thought"))
            result = json.loads(text)
            if not isinstance(result, dict):
                raise ValueError("object required")
            return result
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise ProviderError("format") from exc
