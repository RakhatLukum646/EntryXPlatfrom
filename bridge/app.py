"""Мост Entryx: Twenty CRM <-> процесс <-> агенты Dify.

Запуск: в Docker — `docker compose up -d --build` в папке bridge (перезапускается сам, работает без сессии).
Для отладки локально: ..\\.venv\\Scripts\\python -m uvicorn app:app --port 8787
Где сделки и на ком они: http://localhost:8787/status

События приходят двумя путями:
- вебхук Twenty на POST /twenty/webhook (opportunity.*, task.updated);
- опрос CRM раз в POLL_SECONDS — подстраховка, если вебхук не дошёл.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

load_dotenv(Path(__file__).with_name(".env"))

from engine import PROCESS, STAGES, Agents, Engine, Store, slim_deal  # noqa: E402
from chat_views import (  # noqa: E402
    CMD_ACTIONS, CMD_CONFIRM, CMD_DRAFT, CMD_OPTION, CMD_PROJECTS, COMMANDS, PENDING_MARK, ChatViews, kick,
)
from inbox import hot_count, inbox  # noqa: E402
from tracker import tracker  # noqa: E402
from twenty import Twenty  # noqa: E402
import appdata  # noqa: E402
import webchat  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("entryx.bridge")

crm = Twenty(os.getenv("TWENTY_URL", "http://localhost:3000"), os.environ["TWENTY_API_KEY"])
DB_PATH = Path(os.getenv("BRIDGE_DB") or Path(__file__).with_name("bridge.db"))
engine = Engine(crm, Agents(os.getenv("DIFY_API", "http://localhost/v1")), Store(DB_PATH))
lock = threading.Lock()
app = FastAPI(title="Entryx bridge")


def sync_once() -> dict:
    with lock:
        deals = crm.list("opportunities")
        for deal in deals:
            engine.on_deal(deal)
        closed = 0
        for task_id in engine.store.open_task_ids():
            try:
                task = crm.get("tasks", task_id)
            except RuntimeError:
                continue
            if task and task.get("status") == "DONE":
                engine.on_task_done(task_id)
                closed += 1
        # Поля бота в карточке сделки: обновляем, только если что-то изменилось.
        for deal in deals:
            fields = engine.crm_fields(deal["id"])
            if fields and any((deal.get(k) or "") != v for k, v in fields.items()):
                crm.update("opportunities", deal["id"], fields)
        return {"deals": len(deals), "tasks_closed": closed}


def poll_forever() -> None:
    interval = int(os.getenv("POLL_SECONDS", "10"))
    while True:
        try:
            sync_once()
        except Exception:
            log.exception("sync failed")
        time.sleep(interval)


@app.on_event("startup")
def start_polling() -> None:
    if os.getenv("POLL_SECONDS", "10") != "0":
        threading.Thread(target=poll_forever, daemon=True).start()


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/context", response_class=PlainTextResponse)
def context(agent: str = "") -> str:
    """Живые данные для агента Dify: сделки и открытые задачи ролей агента. Без lock: Dify зовёт это, пока мост ждёт Dify."""
    roles = PROCESS["agents"].get(agent, {}).get("roles", [])
    people = {f"[{PROCESS['roles'][r]['person']}]" for r in roles}
    # Первая строка-метка: флоу Dify по ней отправит следующее сообщение в мост как причину ответа.
    lines = [PENDING_MARK] if engine.store.pending(agent) else []
    lines.append("Сделки:")
    for deal in crm.list("opportunities"):
        d = slim_deal(deal)
        lines.append("- " + "; ".join(f"{k}: {v}" for k, v in d.items()))
    lines.append("\nОткрытые задачи роли:" if people else "\nОткрытые задачи:")
    for task in crm.list("tasks"):
        title = task.get("title") or ""
        if task.get("status") == "DONE" or (people and not any(title.startswith(p) for p in people)):
            continue
        due = f" · срок {task['dueAt'][:10]}" if task.get("dueAt") else ""
        lines.append(f"- {title}{due}")
    return "\n".join(lines)


@app.get("/", include_in_schema=False)
def tracker_page() -> FileResponse:
    """Вкладка «Трекер проектов»: доска по 7 фазам, на ком сделка, что дальше, история."""
    return FileResponse(Path(__file__).with_name("static") / "tracker.html")


@app.get("/api/tracker")
def tracker_api() -> dict:
    return tracker(crm, engine)


@app.get("/inbox", include_in_schema=False)
def inbox_page() -> FileResponse:
    """Вкладка «Уведомления» роли: на неё ведёт ссылка из чата агента."""
    return FileResponse(Path(__file__).with_name("static") / "inbox.html")


@app.get("/api/inbox")
def inbox_api(agent: str = "manager") -> dict:
    return inbox(crm, engine, agent)


@app.get("/app", include_in_schema=False)
def chat_page() -> FileResponse:
    """Веб-чат с агентом роли: кнопки снизу, уведомления в той же вкладке."""
    return FileResponse(Path(__file__).with_name("static") / "chat.html")


@app.get("/app.css", include_in_schema=False)
def app_css() -> FileResponse:
    return FileResponse(Path(__file__).with_name("static") / "app.css", media_type="text/css")


@app.get("/api/me")
def me_api(agent: str) -> dict:
    """Экраны приложения: мои дела с вариантами ответа, мои проекты, на ком шаг, журнал сделки."""
    return appdata.me(views, agent)


@app.get("/api/agents")
def agents_api() -> list[dict]:
    return webchat.agents()


@app.get("/api/history")
def history_api(agent: str, conversation_id: str = "", scope: str = "") -> dict:
    try:
        return webchat.history(agent, conversation_id, scope)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Dify недоступен: {exc}") from exc


@app.post("/api/chat")
async def chat_api(request: Request) -> StreamingResponse:
    body = await request.json()
    return StreamingResponse(
        webchat.stream(body.get("agent", ""), (body.get("query") or "").strip(), body.get("conversation_id") or "",
                       body.get("scope") or ""),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/status", response_class=PlainTextResponse)
def status() -> str:
    """Где каждая сделка и на ком она сейчас: для ручной проверки процесса."""
    out = []
    for deal in crm.list("opportunities"):
        key = deal.get("stage")
        if key not in STAGES:
            continue
        stage = STAGES[key]
        _, wave, held = engine.store.deal(deal["id"])
        waves = stage.get("waves", [])
        out.append(f"■ {deal.get('name')}\n  Этап: {stage['label']} · волна {min(wave + 1, len(waves))} из {len(waves)}")
        if held:
            out.append(f"  СТОИТ: {held}")
        rows = engine.store.db.execute(
            "select id, role, card, blocking, stage from task where deal_id=? and done=0", (deal["id"],)
        ).fetchall()
        current = [r for r in rows if r[4] == key and r[3]]
        side = [r for r in rows if not (r[4] == key and r[3])]
        out.append("  Сейчас на ком (держат переход):" if current else "  Блокирующих задач нет.")
        for _, role, card, _, _ in current:
            out.append(f"    - {PROCESS['roles'][role]['person']} ({PROCESS['roles'][role]['name']}), карточка {card}")
        for _, role, card, blocking, st in side:
            who = f"{PROCESS['roles'][role]['person']}, карточка {card}"
            if st == key:
                out.append(f"  Параллельно: {who}, переход не держит")
            elif blocking:
                out.append(f"  Хвост с этапа «{STAGES[st]['label']}»: {who} — сделку перевели дальше, задача осталась открытой")
            else:
                out.append(f"  Параллельно с этапа «{STAGES[st]['label']}»: {who}, переход не держит")
        nxt = [
            f"{PROCESS['roles'][s['role']]['person']} ({s['card']})"
            for w in waves[wave + 1:] for s in w if engine.needed(deal, s)
        ]
        if nxt:
            out.append("  Дальше на этом этапе: " + " → ".join(nxt))
        if stage.get("next"):
            out.append(f"  После этапа: {STAGES[stage['next']]['label']}")
        out.append("")
    return "\n".join(out) or "Сделок в процессе нет."


views = ChatViews(crm, engine, kick(sync_once), lambda agent: hot_count(crm, engine, agent))


@app.get("/chat", response_class=PlainTextResponse)
def chat(agent: str, q: str = "") -> str:
    """Ответы чата агента Dify без LLM: «Сегодня», варианты ответа, «Черновик», «Мои проекты», причина, «готово» текстом."""
    q = q.strip()
    if q == CMD_ACTIONS:
        return views.actions(agent)
    if engine.store.pending(agent):
        if not q.startswith(COMMANDS):
            return views.reason(agent, q)
        engine.store.set_pending(agent, None)  # нажали другую кнопку — причину больше не ждём
    if q.startswith(CMD_OPTION):
        return views.option(agent, q)
    if q.startswith(CMD_CONFIRM):
        return views.confirm(agent, q)
    if q.startswith(CMD_DRAFT):
        return views.draft(agent, q)
    if q.startswith(CMD_PROJECTS):
        return views.projects(agent)
    if q and not q.startswith(COMMANDS):
        # «Готово», «подтверждаю», «переведи дальше» текстом: закрываем, если понятно, какое дело.
        return views.free_text(agent, q)
    return views.today(agent)


@app.post("/sync")
def sync() -> dict:
    return sync_once()


@app.post("/twenty/webhook")
async def webhook(request: Request) -> dict:
    event = await request.json()
    name = event.get("eventName", "")
    record = event.get("record") or {}
    with lock:
        if name.startswith("opportunity.") and record.get("id"):
            engine.on_deal(crm.get("opportunities", record["id"]))
        elif name == "task.updated" and record.get("status") == "DONE":
            engine.on_task_done(record["id"])
    return {"ok": True}
