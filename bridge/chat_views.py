"""Ответы для чата агента в Dify без LLM: «Сегодня», варианты ответа, «Черновик», «Мои проекты».

Мост отдаёт готовый markdown. Кнопки — разметка Dify `<button data-message="…">`: нажатие отправляет
этот текст в чат, флоу агента узнаёт его по префиксу и снова зовёт мост.
"""

from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timezone
from typing import Callable

from engine import PROCESS, STAGES, Engine, options_of, step_of
from twenty import Twenty

CMD_TODAY = "Сегодня"
CMD_PROJECTS = "Мои проекты"
CMD_CONFIRM = "Подтвердить:"
CMD_OPTION = "Вариант:"
CMD_DRAFT = "Черновик:"
CMD_ACTIONS = "__actions__"  # блок кнопок под ответом LLM (его зовёт флоу Dify)
PENDING_MARK = "ОЖИДАЮ ПРИЧИНУ"  # в /context: флоу отправит следующее сообщение в мост как причину
COMMANDS = (CMD_TODAY, "Что на мне сегодня", CMD_PROJECTS, CMD_CONFIRM, CMD_OPTION, CMD_DRAFT)
ID_RE = re.compile(r"\[([0-9a-f]{8})\]")
OPT_RE = re.compile(r"#(\d+)\s*$")
PREFIX_RE = re.compile(r"^\[[^\]]+\]\s*(?:[0-9][0-9.]*[а-яa-z]?\s*·\s*)?")  # «[Юлия] 5 · » из названия задачи
PUBLIC_URL = os.getenv("BRIDGE_PUBLIC_URL", "http://localhost:8787")


def clean_title(title: str | None) -> str:
    return PREFIX_RE.sub("", title or "") or "задача"


def button(label: str, message: str, primary: bool = False) -> str:
    variant = ' data-variant="primary"' if primary else ""
    return f'<button{variant} data-message="{message.replace(chr(34), "")}">{label}</button>'


def inbox_link(agent: str, count: int | None = None) -> str:
    label = "🔔 Уведомления" + (f" ({count})" if count else "")
    return f"[{label}]({PUBLIC_URL}/app?agent={agent}#inbox)"


def menu(agent: str = "", count: int | None = None) -> str:
    out = " ".join([button("Что на мне сегодня", CMD_TODAY, True), button("Мои проекты", CMD_PROJECTS)])
    return out + (f"\n\n{inbox_link(agent, count)}" if agent else "")


class ChatViews:
    def __init__(self, crm: Twenty, engine: Engine, kick_sync: Callable[[], None],
                 notif_count: Callable[[str], int] | None = None) -> None:
        self.crm, self.engine, self.kick_sync = crm, engine, kick_sync
        self.notif_count = notif_count or (lambda agent: 0)

    # --- helpers -------------------------------------------------------------
    @staticmethod
    def roles(agent: str) -> list[str]:
        return PROCESS["agents"].get(agent, {}).get("roles", [])

    def menu(self, agent: str) -> str:
        try:
            count = self.notif_count(agent)
        except Exception:
            count = None
        return menu(agent, count)

    def open_rows(self, agent: str) -> list[tuple]:
        roles = self.roles(agent)
        if not roles:
            return []
        marks = ",".join("?" * len(roles))
        return self.engine.store.db.execute(
            f"select id, deal_id, stage, wave, role, card, blocking, spec from task where done=0 and role in ({marks})", roles
        ).fetchall()

    def find_row(self, agent: str, query: str) -> tuple | None:
        m = ID_RE.search(query or "")
        if not m:
            return None
        return next((r for r in self.open_rows(agent) if r[0].startswith(m.group(1))), None)

    def holds_transition(self, row: tuple) -> bool:
        _, deal_id, stage, wave, _, _, blocking, _ = row
        cur_stage, cur_wave, _ = self.engine.store.deal(deal_id)
        return bool(blocking) and stage == cur_stage and wave == cur_wave

    @staticmethod
    def due(task: dict) -> tuple[str, int]:
        if not task.get("dueAt"):
            return "", 99
        d = datetime.fromisoformat(task["dueAt"].replace("Z", "+00:00"))
        days = (d.date() - datetime.now(timezone.utc).date()).days
        if days < 0:
            return f"просрочено на {-days} дн.", -1
        if days == 0:
            return "срок сегодня", 0
        return f"срок {d.strftime('%d.%m')}", days

    def deal_name(self, deal_id: str, cache: dict) -> str:
        if deal_id not in cache:
            try:
                cache[deal_id] = self.crm.get("opportunities", deal_id).get("name") or "сделка"
            except RuntimeError:
                cache[deal_id] = "сделка"
        return cache[deal_id]

    def live_items(self, agent: str) -> list[tuple]:
        """Открытые задачи агента: сначала горящие, потом держащие переход."""
        items = []
        for row in self.open_rows(agent):
            try:
                task = self.crm.get("tasks", row[0])
            except RuntimeError:
                continue
            if not task or task.get("status") == "DONE":
                continue
            due_text, due_rank = self.due(task)
            holds = self.holds_transition(row)
            items.append((0 if due_rank <= 0 else 1, 0 if holds else 1, due_rank, row, task, due_text, holds))
        items.sort(key=lambda x: x[:3])
        return items

    @staticmethod
    def option_buttons(row: tuple, title: str) -> str:
        """Кнопки ответа на задачу: варианты из процесса или одна «Подтвердить»."""
        short = row[0][:8]
        opts = options_of(step_of(row))
        if len(opts) == 1:
            return button(opts[0]["label"], f"{CMD_CONFIRM} {title[:50]} [{short}]", True)
        return " ".join(
            button(o["label"], f"{CMD_OPTION} {o['label']} · {title[:30]} [{short}] #{i}", i == 0)
            for i, o in enumerate(opts)
        )

    def item_text(self, i: int, row: tuple, task: dict, due_text: str, holds: bool, names: dict) -> str:
        tags = [STAGES[row[2]]["label"] if row[2] in STAGES else row[2], f"карточка {row[5]}"]
        if due_text:
            tags.append(due_text)
        tags.append("держит переход" if holds else "переход не держит")
        person = PROCESS["roles"][row[4]]["person"]
        title = clean_title(task.get("title"))
        lead = "⚠️ " if (step_of(row) or {}).get("branch") else ""
        return (
            f"\n{i}. {lead}**{self.deal_name(row[1], names)}** · {person} · {' · '.join(tags)}\n   {title}\n   "
            + self.option_buttons(row, title) + " "
            + button("Черновик", f"{CMD_DRAFT} {title[:50]} [{row[0][:8]}]")
        )

    # --- views ---------------------------------------------------------------
    def today(self, agent: str) -> str:
        people = ", ".join(PROCESS["roles"][r]["person"] for r in self.roles(agent)) or "роль не найдена"
        items = self.live_items(agent)
        if not items:
            return f"**Сегодня · {people}**\n\nОткрытых дел нет. Новые задачи появятся здесь, когда сделка дойдёт до вашего шага.\n\n{self.menu(agent)}"
        out = [f"**Сегодня · {people}** — дел: {len(items)}"]
        names: dict = {}
        for i, (_, _, _, row, task, due_text, holds) in enumerate(items, 1):
            out.append(self.item_text(i, row, task, due_text, holds, names))
        out.append("\nОтвет кнопкой закрывает задачу в CRM, и карточка уходит следующему. "
                   "Если дело одно, можно написать словами: «готово». ⚠️ — задача проблемной ветки.")
        out.append("\n" + self.menu(agent))
        return "\n".join(out)

    def draft(self, agent: str, query: str) -> str:
        row = self.find_row(agent, query)
        if not row:
            return f"Не нашёл эту задачу среди ваших открытых. Возможно, на неё уже ответили.\n\n{self.menu(agent)}"
        task = self.crm.get("tasks", row[0])
        body = (task.get("bodyV2") or {}).get("markdown") or "Текста нет."
        title = clean_title(task.get("title"))
        return f"**{title}**\n\n{body}\n\n" + self.option_buttons(row, title) + " " + button("К списку дел", CMD_TODAY)

    def confirm(self, agent: str, query: str) -> str:
        row = self.find_row(agent, query)
        if not row:
            return f"Не нашёл эту задачу среди ваших открытых: на неё уже ответили или она не ваша.\n\n{self.menu(agent)}"
        if len(options_of(step_of(row))) > 1:
            return self.ask_option(agent, row)
        return self.apply(agent, row, 0)

    def option(self, agent: str, query: str) -> str:
        row = self.find_row(agent, query)
        m = OPT_RE.search(query or "")
        if not row or not m:
            return f"Не нашёл эту задачу среди ваших открытых: на неё уже ответили или она не ваша.\n\n{self.menu(agent)}"
        idx = int(m.group(1))
        opts = options_of(step_of(row))
        if idx >= len(opts):
            return self.ask_option(agent, row)
        if opts[idx].get("reason"):
            self.engine.store.set_pending(agent, row[0], idx)
            task = self.crm.get("tasks", row[0]) or {}
            return (f"«{opts[idx]['label']}» — **{clean_title(task.get('title'))}**.\n\n"
                    "Напишите причину одним сообщением: что случилось и что известно. "
                    "Я запишу её в задачу и передам следующему вместе с карточкой.\n\n"
                    + button("Отмена", CMD_TODAY))
        return self.apply(agent, row, idx)

    def reason(self, agent: str, text: str) -> str:
        pend = self.engine.store.pending(agent)
        self.engine.store.set_pending(agent, None)
        row = self.engine.store.task_row(pend[0]) if pend else None
        if not row or row[0] not in {r[0] for r in self.open_rows(agent)}:
            return f"Не нашёл задачу, для которой ждал причину: на неё уже ответили.\n\n{self.menu(agent)}"
        return self.apply(agent, row, pend[1], text.strip()[:1000])

    def apply(self, agent: str, row: tuple, idx: int, reason: str | None = None) -> str:
        res = self.engine.apply_option(row[0], idx, reason)
        self.kick_sync()
        opt, title = res["option"], clean_title(res["title"])
        out = [f"Готово: **{title}** — «{opt['label']}»."]
        if reason:
            out.append(f"Причина записана: {reason}")
        if opt.get("set"):
            out.append("В сделке отмечено: " + ", ".join(PROCESS["flags"][f] for f, v in opt["set"].items() if v) + ".")
        if res["branch"]:
            b = res["branch"]
            first = b["task"].split(".")[0]
            out.append(f"Открыл задачу: **{PROCESS['roles'][b['role']]['person']}** ({b['card']}) — {first[:1].lower() + first[1:]}.")
        if opt.get("stage"):
            out.append(f"Сделка переведена в «{STAGES[opt['stage']]['label']}». Новые задачи по ней не создаются.")
        if res["forecast"]:
            out.append(res["forecast"])
        out.append("\n" + self.menu(agent))
        return "\n".join(out)

    def ask_option(self, agent: str, row: tuple) -> str:
        task = self.crm.get("tasks", row[0]) or {}
        title = clean_title(task.get("title"))
        return f"Как прошло: **{title}**?\n\n" + self.option_buttons(row, title) + "\n\n" + self.menu(agent)

    def free_text(self, agent: str, text: str) -> str:
        """«Готово», «подтверждаю», «переведи дальше» текстом: закрываем, если понятно, какое дело."""
        items = self.live_items(agent)
        holding = [x for x in items if x[6]]
        pick = holding if len(holding) == 1 else items if len(items) == 1 else []
        if pick:
            row = pick[0][3]
            if len(options_of(step_of(row))) > 1:
                return self.ask_option(agent, row)
            return self.apply(agent, row, 0)
        if not items:
            return f"Открытых дел у вас нет — закрывать нечего.\n\n{self.menu(agent)}"
        return "Дел несколько, поэтому не угадываю. Ответьте кнопкой у нужного дела:\n\n" + self.today(agent)

    def actions(self, agent: str) -> str:
        """Под ответом агента: главные дела с кнопками, чтобы после разговора сразу передать карточку дальше."""
        items = self.live_items(agent)[:2]
        if not items:
            return self.menu(agent)
        names: dict = {}
        out = ["---", "**Ваши дела сейчас:**"]
        for i, (_, _, _, row, task, due_text, holds) in enumerate(items, 1):
            out.append(self.item_text(i, row, task, due_text, holds, names))
        out.append("\n" + self.menu(agent))
        return "\n".join(out)

    def projects(self, agent: str) -> str:
        roles = set(self.roles(agent))
        see_all = agent == "rukovoditel"
        out = []
        for deal in self.crm.list("opportunities"):
            key = deal.get("stage")
            if key not in STAGES:
                continue
            rows = self.engine.store.db.execute(
                "select role, card, blocking, stage from task where deal_id=? and done=0", (deal["id"],)
            ).fetchall()
            mine_ever = self.engine.store.db.execute(
                f"select count(*) from task where deal_id=? and role in ({','.join('?' * len(roles)) or 'null'})",
                (deal["id"], *roles),
            ).fetchone()[0] if roles else 0
            if not see_all and not mine_ever:
                continue
            _, wave, held = self.engine.store.deal(deal["id"])
            now = [f"{PROCESS['roles'][r]['person']} ({c})" for r, c, b, st in rows if b and st == key]
            nxt = [
                f"{PROCESS['roles'][s['role']]['person']} ({s['card']})"
                for w in STAGES[key].get("waves", [])[wave + 1:] for s in w if self.engine.needed(deal, s)
            ]
            line = [f"**{deal.get('name')}** · этап «{STAGES[key]['label']}»"]
            if held:
                line.append(f"   Стоит: {held}")
            line.append(f"   Сейчас шаг: {', '.join(now) if now else 'никого, бот переводит дальше'}")
            if nxt:
                line.append(f"   Дальше на этапе: {' → '.join(nxt)}")
            elif STAGES[key].get("next"):
                line.append(f"   Следующий этап: {STAGES[STAGES[key]['next']]['label']}")
            out.append("\n".join(line))
        if not out:
            return f"У вас пока нет сделок в процессе.\n\n{self.menu(agent)}"
        return "**Мои проекты**\n\n" + "\n\n".join(out) + "\n\n" + self.menu(agent)


def kick(sync_once: Callable[[], dict]) -> Callable[[], None]:
    """Запустить синхронизацию в фоне: чат отвечает сразу, а мост двигает процесс следом."""
    return lambda: threading.Thread(target=sync_once, daemon=True).start()
