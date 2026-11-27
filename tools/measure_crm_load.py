"""Сколько запросов к Twenty делает мост на каждый экран и на один опрос.

Только для локальной проверки, на рабочем сервере не запускать: скрипт выполняет настоящий опрос моста
(sync_once) вторым процессом, а тот может создавать задачи и двигать сделки одновременно с работающим мостом.

Запуск из корня репозитория, внутри контейнера моста (там .env и доступ к Twenty):
    docker exec -i -w /app/bridge entryx-bridge-1 python - < tools/measure_crm_load.py

Лимит Twenty — 100 запросов в минуту на API-ключ.
"""

import os
import sys
import time

os.environ["POLL_SECONDS"] = "0"  # фоновый опрос не запускаем, считаем сами
sys.path.insert(0, "/app/bridge")

import twenty  # noqa: E402

calls: list[str] = []
_orig = twenty.Twenty._send


def _counted(self, method, path, **kw):
    calls.append(f"{method} {path.split('?')[0]}")
    return _orig(self, method, path, **kw)


twenty.Twenty._send = _counted

from fastapi.testclient import TestClient  # noqa: E402

import app as bridge  # noqa: E402

client = TestClient(bridge.app)
AGENTS = ["zakupka", "manager", "logistika", "finance", "priemka", "montazh", "rukovoditel"]


def measure(label, fn, pause=0.0):
    calls.clear()
    fn()
    n = len(calls)
    kinds = {}
    for c in calls:
        key = c.split("/rest/")[-1].split("/")[0]
        kinds[key] = kinds.get(key, 0) + 1
    print(f"{label:<38} {n:>4} запросов  {kinds}")
    if pause:
        time.sleep(pause)
    return n


time.sleep(6)  # кэш чтений от импорта истёк
total = 0
total += measure("опрос (sync_once)", bridge.sync_once, 6)
total += measure("/api/me, одна роль", lambda: client.get("/api/me", params={"agent": "manager"}), 6)
total += measure("/api/inbox, одна роль", lambda: client.get("/api/inbox", params={"agent": "manager"}), 6)
total += measure("/status", lambda: client.get("/status"), 6)
total += measure("/api/tracker", lambda: client.get("/api/tracker"), 6)
measure("/api/me все 7 ролей подряд", lambda: [client.get("/api/me", params={"agent": a}) for a in AGENTS], 6)
measure("/api/inbox все 7 ролей подряд", lambda: [client.get("/api/inbox", params={"agent": a}) for a in AGENTS])

# /app обновляет «Сегодня» и уведомления раз в ~15 с; опрос моста — раз в 10 с.
me = measure("_", lambda: client.get("/api/me", params={"agent": "logistika"}), 6)
ib = measure("_", lambda: client.get("/api/inbox", params={"agent": "logistika"}), 6)
sy = measure("_", bridge.sync_once)
per_min_poll = sy * 6
per_tab = (me + ib) * 4
snap = getattr(twenty, "SNAPSHOT_SECONDS", 0)
if snap:
    # Снимок общий для опроса и всех вкладок: сделки и открытые задачи читаются не чаще раза в SNAPSHOT_SECONDS.
    print(f"\nснимок раз в {snap} с: всё вместе ≈ {round(sy * 60 / snap)} запросов в минуту при любом числе вкладок "
          f"(+ по одному на задачу, закрытую в самой CRM)")
else:
    print(f"\nв минуту: опрос ≈ {per_min_poll}, одна открытая вкладка /app ≈ {per_tab} "
          f"→ лимит 100 съедают {max(0, (100 - per_min_poll)) // max(per_tab, 1)} вкладок")
