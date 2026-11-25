"""Данные для приложения Entryx (/app, экраны как в прототипе): «Сегодня», «Мои проекты», чат проекта.

Только чтение. Ответы на задачи идут через агента Dify теми же командами, что и кнопки в чате
(«Подтвердить: … [id]», «Вариант: … #n»), поэтому журнал разговоров остаётся в Dify.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from chat_views import CMD_CONFIRM, CMD_DRAFT, CMD_OPTION, ChatViews, clean_title
from engine import PROCESS, STAGES, options_of, step_of

COLORS = ["#2457D6", "#7C3AED", "#0E7490", "#15803D", "#B45309", "#BE185D", "#4338CA", "#16181D"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
# Служебные строки, которые мост дописывает в задачу для CRM: в приложении они лишние (там есть кнопки).
NOISE = re.compile(
    r"^\s*(\*\*Варианты ответа:\*\*.*|Ответ — кнопкой.*|\*\*Откуда задача:\*\*.*|\*\*Подготовил бот.*|Роль: .*|Этап: .*)$", re.M)


def short_name(name: str) -> str:
    """«ЖК Атлант · ENX-152» → «ЖК Атлант»."""
    return (name or "Сделка").split(" · ")[0].strip()


def letter_of(name: str) -> str:
    words = [w for w in re.split(r"[\s«»\"'·-]+", short_name(name)) if w and w.upper() not in {"ЖК", "ТОО", "ТЦ", "БЦ"}]
    return (words[0][0] if words else (name or "?")[0]).upper()


def color_of(deal_id: str) -> str:
    return COLORS[sum(map(ord, deal_id or "")) % len(COLORS)]


def phase_of(stage_key: str | None) -> int:
    for i, ph in enumerate(PROCESS.get("phases", [])):
        if stage_key in ph["stages"]:
            return i
    return 0


def day_month(iso: str | None) -> str:
    if not iso:
        return ""
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return f"{d.day} {MONTHS[d.month - 1]}"


def urgency(task: dict) -> tuple[str, bool]:
    """Подпись срока как в прототипе: «Срок — 13 окт», «Сегодня», «Просрочено на 2 дн.»."""
    if not task.get("dueAt"):
        return "Без срока", False
    d = datetime.fromisoformat(task["dueAt"].replace("Z", "+00:00"))
    days = (d.date() - datetime.now(timezone.utc).date()).days
    if days < 0:
        return f"Просрочено на {-days} дн.", True
    if days == 0:
        return "Срок — сегодня", True
    return f"Срок — {d.day} {MONTHS[d.month - 1][:3]}", days <= 1


def options(row: tuple, title: str) -> list[dict]:
    short = row[0][:8]
    opts = options_of(step_of(row))
    if len(opts) == 1:
        return [{"label": opts[0]["label"], "cmd": f"{CMD_CONFIRM} {title[:50]} [{short}]", "pri": True}]
    return [
        {"label": o["label"], "cmd": f"{CMD_OPTION} {o['label']} · {title[:30]} [{short}] #{i}", "pri": i == 0,
         "reason": bool(o.get("reason"))}
        for i, o in enumerate(opts)
    ]


def turn_line(views: ChatViews, deal: dict) -> str:
    """«Сейчас шаг Данила: подтвердить деньги…» — на ком держится сделка."""
    key = deal.get("stage")
    cur_stage, wave, held = views.engine.store.deal(deal["id"])
    if held:
        return f"Сделка стоит: {held}"
    rows = views.engine.store.db.execute(
        "select id, role, card from task where deal_id=? and done=0 and blocking=1 and stage=?", (deal["id"], key)
    ).fetchall()
    if not rows:
        return "Бот переводит сделку дальше" if key in STAGES and STAGES[key].get("next") else ""
    parts = []
    for task_id, role, card in rows[:3]:
        try:
            title = clean_title(views.crm.get("tasks", task_id).get("title"))
        except RuntimeError:
            title = f"карточка {card}"
        parts.append(f"{PROCESS['roles'][role]['person']} — {title[:1].lower() + title[1:]}")
    return "Сейчас шаг: " + "; ".join(parts)


def me(views: ChatViews, agent: str) -> dict:
    roles = views.roles(agent)
    deals = {d["id"]: d for d in views.crm.list("opportunities")}
    tasks = []
    for _, _, _, row, task, _, holds in views.live_items(agent):
        deal = deals.get(row[1]) or {}
        title = clean_title(task.get("title"))
        raw = (task.get("bodyV2") or {}).get("markdown") or ""
        body = re.sub(r"\n{3,}", "\n\n", NOISE.sub("", raw)).strip()
        lab, hot = urgency(task)
        step = step_of(row) or {}
        tasks.append({
            "id": row[0], "deal_id": row[1], "deal": short_name(deal.get("name")), "deal_full": deal.get("name") or "",
            "color": color_of(row[1]), "letter": letter_of(deal.get("name")),
            "title": title, "body": body[:2500], "card": row[5], "person": PROCESS["roles"][row[4]]["person"],
            "stage": STAGES[row[2]]["label"] if row[2] in STAGES else row[2],
            "due": lab, "due_date": day_month(task.get("dueAt")), "hot": hot, "holds": holds,
            "branch": bool(step.get("branch")), "options": options(row, title), "by_bot": "Подготовил бот" in raw,
            "draft": f"{CMD_DRAFT} {title[:50]} [{row[0][:8]}]",
        })
    mine = {d for (d,) in views.engine.store.db.execute(
        f"select distinct deal_id from task where role in ({','.join('?' * len(roles)) or 'null'})", tuple(roles))} if roles else set()
    projects = []
    for deal in deals.values():
        key = deal.get("stage")
        if key not in STAGES or (agent != "rukovoditel" and deal["id"] not in mine):
            continue
        events = views.engine.store.db.execute(
            "select at, kind, text from event where deal_id=? order by id desc limit 40", (deal["id"],)
        ).fetchall()
        projects.append({
            "id": deal["id"], "name": short_name(deal.get("name")), "full": deal.get("name") or "",
            "color": color_of(deal["id"]), "letter": letter_of(deal.get("name")),
            "stage": STAGES[key]["label"], "phase": phase_of(key), "turn": turn_line(views, deal),
            "events": [{"at": at, "kind": kind, "text": text} for at, kind, text in reversed(events)],
        })
    projects.sort(key=lambda p: p["name"])
    return {
        "agent": agent, "title": PROCESS["agents"].get(agent, {}).get("title", agent),
        "people": [PROCESS["roles"][r]["person"] for r in roles],
        "roles": [PROCESS["roles"][r]["name"] for r in roles],
        "phases": [p["name"] for p in PROCESS.get("phases", [])],
        "tasks": tasks, "projects": projects,
    }
