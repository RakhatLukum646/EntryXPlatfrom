"""Тонкий клиент REST API Twenty CRM (только то, что нужно мосту).

У Twenty лимит на API-ключ: 100 запросов в минуту. Мост читает сделки и задачи из опроса, экранов /app,
уведомлений, трекера и инструментов агентов. Чтобы нагрузка не росла с числом задач и открытых вкладок:
- снимок: все сделки и все открытые задачи читаются целиком (по 200 за запрос) не чаще раза в SNAPSHOT_SECONDS;
  get/list сделок и задач отвечают из снимка, к Twenty идёт только то, чего в снимке нет (например, закрытая задача);
- записи моста сразу попадают в снимок — свои изменения видны без перечитывания;
- прочие чтения (GET) кэшируются на CACHE_SECONDS;
- на 429 ждём, сколько сказал Twenty, и повторяем — вместо ошибки «мост недоступен».
"""

from __future__ import annotations

import copy
import json
import threading
import time
from typing import Any

import httpx

CACHE_SECONDS = 5
SNAPSHOT_SECONDS = 8
RETRIES_429 = 8
PAGE = 200
# Что держим в снимке: объект -> фильтр REST. Задачи — только открытые: закрытых со временем тысячи.
SNAPSHOT = {"opportunities": None, "tasks": "status[neq]:DONE"}


class Twenty:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=30,
        )
        self._cache: dict[str, tuple[float, Any]] = {}
        self._snap: dict[str, tuple[float, dict[str, dict]]] = {}
        self._lock = threading.RLock()

    # --- helpers -------------------------------------------------------------
    def _send(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            return self.http.request(method, path, **kw)
        except (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError):
            # Соединение из пула закрыто сервером (простой, перезапуск Twenty) — один повтор.
            return self.http.request(method, path, **kw)

    def _req(self, method: str, path: str, cache: bool = True, **kw: Any) -> Any:
        key = ""
        if method == "GET" and cache:
            key = path + "?" + json.dumps(kw.get("params") or {}, sort_keys=True)
            with self._lock:
                hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
                return copy.deepcopy(hit[1])  # копия: вызывающий может менять словарь
        elif method != "GET":
            with self._lock:
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
            with self._lock:
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

    def _fetch_all(self, plural: str, params: dict | None, cache: bool = True) -> list[dict]:
        out: list[dict] = []
        cursor = None
        while True:
            q = {"limit": 60, **(params or {})}
            if cursor:
                q["starting_after"] = cursor
            payload = self._req("GET", f"/rest/{plural}", cache=cache, params=q)
            items = self._unwrap(payload) or []
            out.extend(items)
            info = payload.get("pageInfo") or {}
            if not info.get("hasNextPage") or not items:
                return out
            cursor = info.get("endCursor")

    # --- снимок сделок и открытых задач ----------------------------------------
    def _snapshot(self, plural: str) -> dict[str, dict]:
        with self._lock:
            hit = self._snap.get(plural)
            if hit and time.monotonic() - hit[0] < SNAPSHOT_SECONDS:
                return hit[1]
            params = {"limit": PAGE, **({"filter": SNAPSHOT[plural]} if SNAPSHOT[plural] else {})}
            snap = {r["id"]: r for r in self._fetch_all(plural, params, cache=False)}
            self._snap[plural] = (time.monotonic(), snap)
            return snap

    def _remember(self, plural: str, record: dict | None, body: dict | None = None) -> None:
        """Своя запись — сразу в снимок (если он уже есть), чтобы экраны и опрос видели её без перечитывания."""
        if plural not in SNAPSHOT or not record or not record.get("id"):
            return
        with self._lock:
            hit = self._snap.get(plural)
            if hit:
                old = hit[1].get(record["id"], {})
                hit[1][record["id"]] = {**old, **(body or {}), **record}

    def refresh(self, plural: str | None = None) -> None:
        """Забыть снимок: следующее чтение возьмёт свежие данные (вебхук об изменении в CRM)."""
        with self._lock:
            for p in [plural] if plural else list(self._snap):
                self._snap.pop(p, None)

    def open_tasks(self) -> list[dict]:
        return copy.deepcopy(list(self._snapshot("tasks").values()))

    # --- records -------------------------------------------------------------
    def list(self, plural: str, params: dict | None = None) -> list[dict]:
        if plural == "opportunities" and not params:
            return copy.deepcopy(list(self._snapshot(plural).values()))
        return self._fetch_all(plural, params)

    def get(self, plural: str, record_id: str) -> dict:
        if plural in SNAPSHOT:
            rec = self._snapshot(plural).get(record_id)
            if rec is not None:
                return copy.deepcopy(rec)
        rec = self._unwrap(self._req("GET", f"/rest/{plural}/{record_id}"))
        if plural == "opportunities":
            self._remember(plural, rec)  # новая сделка, которой ещё нет в снимке
        return rec

    def create(self, plural: str, body: dict) -> dict:
        rec = self._unwrap(self._req("POST", f"/rest/{plural}", json=body))
        self._remember(plural, rec, body)
        return rec

    def update(self, plural: str, record_id: str, body: dict) -> dict:
        rec = self._unwrap(self._req("PATCH", f"/rest/{plural}/{record_id}", json=body))
        self._remember(plural, {**(rec or {}), "id": record_id}, body)
        return rec

    # --- metadata ------------------------------------------------------------
    def objects(self) -> list[dict]:
        return self._unwrap(self._req("GET", "/rest/metadata/objects"))

    def create_field(self, body: dict) -> dict:
        return self._unwrap(self._req("POST", "/rest/metadata/fields", json=body))

    def update_field(self, field_id: str, body: dict) -> dict:
        return self._unwrap(self._req("PATCH", f"/rest/metadata/fields/{field_id}", json=body))
