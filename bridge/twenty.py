"""Тонкий клиент REST API Twenty CRM (только то, что нужно мосту).

У Twenty лимит на API-ключ: 100 запросов в минуту. Мост читает одно и то же из опроса, экранов /app,
уведомлений и инструментов агентов, поэтому:
- на 429 ждём, сколько сказал Twenty, и повторяем — вместо ошибки «мост недоступен»;
- чтения (GET) кэшируем на CACHE_SECONDS; любая запись моста сбрасывает кэш, свои изменения видны сразу.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from typing import Any

import httpx

CACHE_SECONDS = 5
RETRIES_429 = 6


class Twenty:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=30,
        )
        self._cache: dict[str, tuple[float, Any]] = {}
        self._cache_lock = threading.Lock()

    # --- helpers -------------------------------------------------------------
    def _send(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            return self.http.request(method, path, **kw)
        except (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError):
            # Соединение из пула закрыто сервером (простой, перезапуск Twenty) — один повтор.
            return self.http.request(method, path, **kw)

    def _req(self, method: str, path: str, **kw: Any) -> Any:
        key = ""
        if method == "GET":
            key = path + "?" + json.dumps(kw.get("params") or {}, sort_keys=True)
            with self._cache_lock:
                hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
                return copy.deepcopy(hit[1])  # копия: вызывающий может менять словарь сделки
        else:
            with self._cache_lock:
                self._cache.clear()
        for attempt in range(RETRIES_429 + 1):
            r = self._send(method, path, **kw)
            if r.status_code != 429 or attempt == RETRIES_429:
                break
            try:
                wait = float(r.json().get("retryAfterSeconds") or 1)
            except ValueError:
                wait = 1.0
            time.sleep(min(max(wait, 1.0), 10.0) * (1 + attempt / 2))
        if r.status_code >= 400:
            raise RuntimeError(f"Twenty {method} {path} -> {r.status_code}: {r.text[:500]}")
        data = r.json() if r.content else None
        if key:
            with self._cache_lock:
                if len(self._cache) > 2000:
                    self._cache.clear()
                self._cache[key] = (time.monotonic(), copy.deepcopy(data))
        return data

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        """REST отвечает {"data": {"<имя>": ...}}; возвращаем значение под единственным ключом."""
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        if isinstance(data, dict) and len(data) == 1:
            return next(iter(data.values()))
        return data

    # --- records -------------------------------------------------------------
    def list(self, plural: str, params: dict | None = None) -> list[dict]:
        out: list[dict] = []
        cursor = None
        while True:
            q = {"limit": 60, **(params or {})}
            if cursor:
                q["starting_after"] = cursor
            payload = self._req("GET", f"/rest/{plural}", params=q)
            items = self._unwrap(payload) or []
            out.extend(items)
            info = payload.get("pageInfo") or {}
            if not info.get("hasNextPage") or not items:
                return out
            cursor = info.get("endCursor")

    def get(self, plural: str, record_id: str) -> dict:
        return self._unwrap(self._req("GET", f"/rest/{plural}/{record_id}"))

    def create(self, plural: str, body: dict) -> dict:
        return self._unwrap(self._req("POST", f"/rest/{plural}", json=body))

    def update(self, plural: str, record_id: str, body: dict) -> dict:
        return self._unwrap(self._req("PATCH", f"/rest/{plural}/{record_id}", json=body))

    # --- metadata ------------------------------------------------------------
    def objects(self) -> list[dict]:
        return self._unwrap(self._req("GET", "/rest/metadata/objects"))

    def create_field(self, body: dict) -> dict:
        return self._unwrap(self._req("POST", "/rest/metadata/fields", json=body))

    def update_field(self, field_id: str, body: dict) -> dict:
        return self._unwrap(self._req("PATCH", f"/rest/metadata/fields/{field_id}", json=body))
