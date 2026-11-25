"""Тонкий клиент REST API Twenty CRM (только то, что нужно мосту)."""

from __future__ import annotations

from typing import Any

import httpx


class Twenty:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=30,
        )

    # --- helpers -------------------------------------------------------------
    def _req(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = self.http.request(method, path, **kw)
        except (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError):
            # Соединение из пула закрыто сервером (простой, перезапуск Twenty) — один повтор.
            r = self.http.request(method, path, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Twenty {method} {path} -> {r.status_code}: {r.text[:500]}")
        return r.json() if r.content else None

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
