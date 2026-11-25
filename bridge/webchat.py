"""Веб-чат Entryx (/app): страница говорит с агентами Dify через мост.

Ключи агентов (DIFY_KEY_<АГЕНТ>) остаются в мосте, страница их не видит. Ответ агента идёт потоком (SSE),
как в чате Dify. Пользователь Dify у веб-чата свой на каждого агента: web-<агент>.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator

import httpx

from engine import PROCESS

TIMEOUT = httpx.Timeout(180, connect=10)


def _api() -> str:
    return os.getenv("DIFY_API", "http://localhost/v1").rstrip("/")


def _key(agent: str) -> str:
    if agent not in PROCESS["agents"]:
        raise KeyError(f"нет агента {agent}")
    key = os.getenv(f"DIFY_KEY_{agent.upper()}")
    if not key:
        raise KeyError(f"нет ключа DIFY_KEY_{agent.upper()} в bridge/.env")
    return key


def _user(agent: str, scope: str = "") -> str:
    """Пользователь Dify веб-чата: свой на агента, а у чата сделки — ещё и на сделку (scope = первые 8 знаков id)."""
    scope = re.sub(r"[^0-9a-zA-Z-]", "", scope)[:12]
    return f"web-{agent}" + (f"-{scope}" if scope else "")


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def agents() -> list[dict]:
    return [
        {"key": k, "title": a["title"], "people": [PROCESS["roles"][r]["person"] for r in a["roles"]],
         "roles": [PROCESS["roles"][r]["name"] for r in a["roles"]]}
        for k, a in PROCESS["agents"].items()
    ]


def stream(agent: str, query: str, conversation_id: str = "", scope: str = "") -> Iterator[str]:
    """Пересылает поток Dify как есть (строки data: {...}). Чужой или удалённый разговор — начинаем новый."""
    try:
        key = _key(agent)
    except KeyError as exc:
        yield _sse({"event": "error", "message": str(exc)})
        return
    for conv in ([conversation_id, ""] if conversation_id else [""]):
        body = {"inputs": {}, "query": query, "response_mode": "streaming", "user": _user(agent, scope), "conversation_id": conv}
        try:
            with httpx.stream("POST", f"{_api()}/chat-messages", headers={"Authorization": f"Bearer {key}"},
                              json=body, timeout=TIMEOUT) as r:
                if r.status_code == 404 and conv:
                    continue  # разговор не найден — повторяем без него
                if r.status_code >= 400:
                    r.read()
                    yield _sse({"event": "error", "message": f"Dify {r.status_code}: {r.text[:300]}"})
                    return
                for line in r.iter_lines():
                    if line.startswith("data:"):
                        yield line + "\n\n"
                return
        except httpx.HTTPError as exc:
            yield _sse({"event": "error", "message": f"Dify недоступен: {exc}"})
            return


def history(agent: str, conversation_id: str = "", scope: str = "") -> dict:
    """Приветствие агента и сообщения разговора. Без id — последний разговор веб-чата этого агента; «new» — пустой."""
    key = _key(agent)
    headers = {"Authorization": f"Bearer {key}"}
    with httpx.Client(base_url=_api(), headers=headers, timeout=30) as c:
        params = c.get("/parameters").json()
        out = {"opening": params.get("opening_statement") or "", "conversation_id": "", "messages": []}
        if conversation_id == "new":
            return out
        if not conversation_id:
            convs = c.get("/conversations", params={"user": _user(agent, scope), "limit": 1}).json().get("data") or []
            if not convs:
                return out
            conversation_id = convs[0]["id"]
        r = c.get("/messages", params={"user": _user(agent, scope), "conversation_id": conversation_id, "limit": 100})
        if r.status_code >= 400:
            return out
        out["conversation_id"] = conversation_id
        out["messages"] = [
            {"query": m.get("query") or "", "answer": m.get("answer") or "", "at": m.get("created_at")}
            for m in r.json().get("data") or []
        ]
        return out
