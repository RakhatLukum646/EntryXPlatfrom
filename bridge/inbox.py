"""Уведомления роли: сроки задач, напоминания, эскалации, проблемные ветки, новые задачи.

Считается по CRM и журналу моста при каждом открытии страницы: отдельного планировщика нет,
поэтому уведомление «наступает» само, когда подходит срок.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from chat_views import clean_title
from engine import PROCESS, STAGES, Engine
from twenty import Twenty

ESC = PROCESS.get("escalation", {})
ESC_TO = ESC.get("to", "zhenya")
ESC_AFTER = int(ESC.get("after_days", 1))
REMIND = int(ESC.get("remind_days", 1))
HOT = {"overdue", "today", "escalation", "held", "problem"}


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def inbox(crm: Twenty, engine: Engine, agent: str) -> dict:
    now = datetime.now(timezone.utc)
    roles = set(PROCESS["agents"].get(agent, {}).get("roles", []))
    lead = ESC_TO in roles
    persons = {PROCESS["roles"][r]["person"] for r in roles}
    deals = {d["id"]: d for d in crm.list("opportunities")}
    tasks = {t["id"]: t for t in crm.list("tasks")}
    db = engine.store.db
    items: list[dict] = []

    def add(kind: str, deal_id: str, text: str, at: datetime | str | None = None, who: str = "", title: str = "") -> None:
        deal = deals.get(deal_id) or {}
        # Проблема из журнала срочная первые сутки, дальше это история.
        hot = kind in HOT and not (kind == "problem" and isinstance(at, str) and at < (now - timedelta(days=1)).isoformat())
        items.append({
            "kind": kind, "hot": hot, "deal": deal.get("name") or "сделка", "deal_id": deal_id,
            "stage": STAGES.get(deal.get("stage"), {}).get("label", ""), "who": who, "title": title, "text": text,
            "at": at.isoformat(timespec="seconds") if isinstance(at, datetime) else at,
        })

    # Сроки открытых задач: свои — напоминания, чужие просроченные — эскалация руководителю.
    open_mine: dict[str, list[str]] = {}  # сделка → заголовки открытых задач роли: «новая задача» только пока она открыта
    for task_id, deal_id, role, card in db.execute("select id, deal_id, role, card from task where done=0"):
        task = tasks.get(task_id)
        if not task or task.get("status") == "DONE" or deal_id not in deals:
            continue
        if role in roles:
            open_mine.setdefault(deal_id, []).append(clean_title(task.get("title"))[:40])
        due = _parse(task.get("dueAt"))
        if not due:
            continue
        person = PROCESS["roles"][role]["person"]
        title = clean_title(task.get("title"))
        days_late = (now.date() - due.date()).days
        if role in roles:
            if days_late > 0:
                add("overdue", deal_id, f"Просрочено на {days_late} дн. Ответьте в чате или напишите, что мешает.", due, person, title)
            elif days_late == 0:
                add("today", deal_id, f"Срок сегодня, до {due.strftime('%H:%M')} UTC.", due, person, title)
            elif -days_late <= REMIND:
                add("soon", deal_id, f"Напоминание: срок {due.strftime('%d.%m')}.", due, person, title)
        if lead and role not in roles and days_late >= ESC_AFTER:
            add("escalation", deal_id, f"Эскалация: {person} ({card}) просрочил(а) на {days_late} дн.", due, person, title)

    # Сделки, которые стоят на шлюзе (например, нет аванса).
    if lead:
        for deal_id, stage, held in db.execute("select id, stage, held from deal where held is not null"):
            if deal_id in deals and deals[deal_id].get("stage") == stage:
                add("held", deal_id, f"Сделка стоит: {held}.")

    # Журнал: проблемные ответы и новые задачи за последние дни.
    since = (now - timedelta(days=7)).isoformat(timespec="seconds")
    mine = {d for (d,) in db.execute(
        f"select distinct deal_id from task where role in ({','.join('?' * len(roles)) or 'null'})", tuple(roles))} if roles else set()
    for deal_id, at, kind, text in db.execute(
        "select deal_id, at, kind, text from event where at>=? and kind in ('problem','task','stage') order by id desc limit 300", (since,)
    ):
        if deal_id not in deals:
            continue
        if kind == "problem" and (lead or deal_id in mine) and not any(text.startswith(p + ":") for p in persons):
            add("problem", deal_id, text, at)
        elif kind == "task" and any(text.startswith(f"Задача для {p}:") for p in persons) and any(
            t and text.split(": ", 1)[-1].startswith(t[:30]) for t in open_mine.get(deal_id, [])
        ):
            add("new", deal_id, text.replace("Задача для ", "Новая задача · ", 1), at)
        elif kind == "stage" and ("Отложена" in text or "Отменена" in text) and (lead or deal_id in mine):
            add("info", deal_id, text, at)

    order = {"overdue": 0, "escalation": 1, "held": 2, "today": 3, "problem": 4, "soon": 5, "new": 6, "info": 7}
    items.sort(key=lambda x: x["at"] or "", reverse=True)  # внутри группы свежие выше
    items.sort(key=lambda x: order[x["kind"]])
    return {
        "agent": agent,
        "title": PROCESS["agents"].get(agent, {}).get("title", agent),
        "people": [PROCESS["roles"][r]["person"] for r in PROCESS["agents"].get(agent, {}).get("roles", [])],
        "generated_at": now.isoformat(timespec="seconds"),
        "hot": sum(i["hot"] for i in items),
        "items": items,
        "agents": [{"key": k, "title": a["title"], "people": [PROCESS["roles"][r]["person"] for r in a["roles"]]}
                   for k, a in PROCESS["agents"].items()],
    }


def hot_count(crm: Twenty, engine: Engine, agent: str) -> int:
    return inbox(crm, engine, agent)["hot"]
