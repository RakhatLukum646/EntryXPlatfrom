"""Движок: этап сделки в CRM -> волны задач по ролям -> переход на следующий этап.

Состояние процесса хранится в CRM (этап, флаги, задачи). Здесь только служебная
таблица: какие задачи мост создал и какая волна этапа сейчас открыта.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml

from twenty import Twenty

log = logging.getLogger("entryx.engine")
ROOT = Path(__file__).resolve().parent.parent
PROCESS = yaml.safe_load((ROOT / "process" / "stages.yaml").read_text(encoding="utf-8"))
STAGES = {s["key"]: s for s in PROCESS["stages"]}
OPTION_SETS = PROCESS.get("option_sets", {})
BRANCHES = PROCESS.get("branches", {})
DEFAULT_OPTIONS = [{"label": "Подтвердить"}]
BOT_MARK = "Подготовил бот · подтвердите"
DRAFT_WAIT_SECONDS = 5


DEAL_KEYS = ["name", "stage", "amount", "closeDate", *PROCESS["flags"], *PROCESS["fields"], *PROCESS.get("bot_fields", {})]


def slim_deal(deal: dict) -> dict:
    """Только поля сделки, нужные агенту (без служебных полей Twenty)."""
    out = {k: deal.get(k) for k in DEAL_KEYS if deal.get(k) not in (None, "", False)}
    if out.get("stage") in STAGES:
        out["stage"] = STAGES[out["stage"]]["label"]
    amount = deal.get("amount") or {}
    if amount.get("amountMicros"):
        out["amount"] = f"{amount['amountMicros'] / 1_000_000:,.0f} {amount.get('currencyCode', '')}".replace(",", " ")
    return out


class Store:
    def __init__(self, path: Path) -> None:
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()  # одно соединение на потоки опроса, чата и агентов
        self.db.executescript(
            """
            create table if not exists deal(id text primary key, stage text, wave integer, held text);
            create table if not exists task(id text primary key, deal_id text, stage text, wave integer,
                role text, card text, blocking integer, done integer default 0);
            create table if not exists event(id integer primary key autoincrement, deal_id text, at text,
                kind text, text text);
            """
        )
        try:
            self.db.execute("alter table task add column due text")  # срок задачи: для статуса «Просрочено»
        except sqlite3.OperationalError:
            pass
        try:
            self.db.execute("alter table task add column spec text")  # шаг или ветка задачи (JSON): варианты ответа
        except sqlite3.OperationalError:
            pass
        try:
            self.db.execute("alter table task add column at text")  # когда мост создал задачу: для досоздания волны
        except sqlite3.OperationalError:
            pass
        # Вариант ответа, для которого бот ждёт причину следующим сообщением (по агенту чата).
        self.db.execute("create table if not exists pending(agent text primary key, task_id text, opt integer)")

    def log(self, deal_id: str, kind: str, text: str) -> None:
        """Журнал процесса для трекера: stage, task, done, held, manual."""
        with self.lock:
            self.db.execute(
                "insert into event(deal_id, at, kind, text) values (?,?,?,?)",
                (deal_id, datetime.now(timezone.utc).isoformat(timespec="seconds"), kind, text),
            )
            self.db.commit()

    def events(self, deal_id: str, limit: int = 40) -> list[tuple[str, str, str]]:
        return self.db.execute(
            "select at, kind, text from event where deal_id=? order by id desc limit ?", (deal_id, limit)
        ).fetchall()

    def stage_since(self, deal_id: str) -> str | None:
        row = self.db.execute(
            "select at from event where deal_id=? and kind in ('stage','manual') order by id desc limit 1", (deal_id,)
        ).fetchone()
        return row[0] if row else None

    def deal(self, deal_id: str) -> tuple[str | None, int, str | None]:
        row = self.db.execute("select stage, wave, held from deal where id=?", (deal_id,)).fetchone()
        return (row[0], row[1], row[2]) if row else (None, 0, None)

    def set_deal(self, deal_id: str, stage: str, wave: int, held: str | None = None) -> None:
        with self.lock:
            self.db.execute("insert or replace into deal values (?,?,?,?)", (deal_id, stage, wave, held))
            self.db.commit()

    def add_task(self, task_id: str, deal_id: str, stage: str, wave: int, step: dict, due: str | None = None) -> None:
        with self.lock:
            self.db.execute(
                "insert or replace into task(id, deal_id, stage, wave, role, card, blocking, due, spec, at)"
                " values (?,?,?,?,?,?,?,?,?,?)",
                (task_id, deal_id, stage, wave, step["role"], step["card"], int(step.get("blocking", True)), due,
                 json.dumps(step, ensure_ascii=False), datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            self.db.commit()

    def wave_roles(self, deal_id: str, stage: str, wave: int, since: str | None) -> list[tuple[str, str]]:
        """(роль, карточка) задач этой волны, созданных после входа в этап. Старые строки без `at` — считаем созданными."""
        return self.db.execute(
            "select role, card from task where deal_id=? and stage=? and wave=? and coalesce(at, '9999') >= ?",
            (deal_id, stage, wave, since or ""),
        ).fetchall()

    def task_row(self, task_id: str) -> tuple | None:
        return self.db.execute(
            "select id, deal_id, stage, wave, role, card, blocking, spec from task where id=?", (task_id,)
        ).fetchone()

    def set_pending(self, agent: str, task_id: str | None, opt: int = 0) -> None:
        with self.lock:
            if task_id:
                self.db.execute("insert or replace into pending values (?,?,?)", (agent, task_id, opt))
            else:
                self.db.execute("delete from pending where agent=?", (agent,))
            self.db.commit()

    def pending(self, agent: str) -> tuple[str, int] | None:
        row = self.db.execute("select task_id, opt from pending where agent=?", (agent,)).fetchone()
        return (row[0], row[1]) if row else None

    def open_task_ids(self) -> list[str]:
        return [r[0] for r in self.db.execute("select id from task where done=0")]

    def mark_done(self, task_id: str) -> tuple[str, str, int] | None:
        with self.lock:
            row = self.db.execute("select deal_id, stage, wave from task where id=? and done=0", (task_id,)).fetchone()
            if row:
                self.db.execute("update task set done=1 where id=?", (task_id,))
                self.db.commit()
            return row

    def wave_open_blocking(self, deal_id: str, stage: str, wave: int) -> int:
        return self.db.execute(
            "select count(*) from task where deal_id=? and stage=? and wave=? and blocking=1 and done=0",
            (deal_id, stage, wave),
        ).fetchone()[0]


class Agents:
    """Вызов агентов Dify. Нет ключа агента — задача создаётся по шаблону (ручная версия)."""

    def __init__(self, api_base: str) -> None:
        self.api_base = api_base.rstrip("/")

    def key_for(self, agent: str) -> str | None:
        return os.getenv(f"DIFY_KEY_{agent.upper()}") or None

    def draft(self, agent: str, role: str, stage: dict, deal: dict, steps: list[dict]) -> list[dict]:
        key = self.key_for(agent)
        if key:
            try:
                return self._call_dify(key, role, stage, deal, steps)
            except Exception as exc:  # агент недоступен — не останавливаем процесс
                log.warning("agent %s failed, using template: %s", agent, exc)
        return [self._template(step) for step in steps]

    def _call_dify(self, key: str, role: str, stage: dict, deal: dict, steps: list[dict]) -> list[dict]:
        r = httpx.post(
            f"{self.api_base}/chat-messages",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "inputs": {
                    "role": f"{PROCESS['roles'][role]['person']} ({PROCESS['roles'][role]['name']})",
                    "stage": stage["label"],
                    "stage_key": stage["key"],
                    "deal": json.dumps(slim_deal(deal), ensure_ascii=False, default=str)[:19000],
                    "steps": json.dumps([{k: v for k, v in s.items() if k != "opts"} for s in steps], ensure_ascii=False),
                },
                "query": "РЕЖИМ: ЗАДАЧИ. Подготовь задачи по шагам, которые открылись.",
                "response_mode": "blocking",
                "user": "entryx-bridge",
            },
            timeout=DRAFT_WAIT_SECONDS,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code}: {r.text[:300]}")
        text = re.sub(r"^```(?:json)?|```$", "", r.json()["answer"].strip(), flags=re.M).strip()
        drafted = json.loads(text)["tasks"]
        if len(drafted) != len(steps):
            raise RuntimeError(f"agent returned {len(drafted)} tasks for {len(steps)} steps")
        return [{"title": d["title"], "body": d["body"], "by_agent": True} for d in drafted]

    @staticmethod
    def _template(step: dict) -> dict:
        body = step["task"]
        if step.get("rules"):
            body += "\n\n**Правила**\n" + "\n".join(f"- {r}" for r in step["rules"])
        return {"title": step["task"].split(".")[0][:80], "body": body, "by_agent": False}


def options_of(step: dict | None) -> list[dict]:
    """Кнопки ответа на задачу: набор из option_sets или одна «Подтвердить»."""
    return OPTION_SETS.get((step or {}).get("opts"), DEFAULT_OPTIONS)


def step_of(row: tuple) -> dict | None:
    """Шаг процесса задачи: из сохранённого JSON, а для старых задач — поиском по этапу, роли и карточке."""
    _, _, stage, wave, role, card, _, spec = row
    if spec:
        return json.loads(spec)
    waves = STAGES.get(stage, {}).get("waves", [])
    for s in (waves[wave] if wave < len(waves) else []):
        if s["role"] == role and s["card"] == card:
            return s
    return None


class Engine:
    def __init__(self, crm: Twenty, agents: Agents, store: Store) -> None:
        self.crm, self.agents, self.store = crm, agents, store
        self.draft_pool = ThreadPoolExecutor(max_workers=len(PROCESS["roles"]))
        self.default_member = os.getenv("TWENTY_DEFAULT_MEMBER_ID") or None
        if not self.default_member:
            members = crm.list("workspaceMembers")
            self.default_member = members[0]["id"] if members else None

    # --- flags ---------------------------------------------------------------
    @staticmethod
    def flag(deal: dict, name: str | None) -> bool:
        return True if not name else bool(deal.get(name))

    def needed(self, deal: dict, item: dict) -> bool:
        return self.flag(deal, item.get("when"))

    # --- entry points --------------------------------------------------------
    def on_deal(self, deal: dict) -> None:
        stage_key = deal.get("stage")
        if stage_key not in STAGES:
            return
        known_stage, wave, held = self.store.deal(deal["id"])
        if known_stage != stage_key:
            log.info("deal %s: stage %s -> %s", deal.get("name"), known_stage, stage_key)
            if known_stage is None:
                self.store.log(deal["id"], "stage", f"Сделка в процессе: этап «{STAGES[stage_key]['label']}»")
            else:
                prev = STAGES.get(known_stage, {}).get("label", known_stage)
                self.store.log(deal["id"], "manual", f"Сделку перевели вручную: «{prev}» → «{STAGES[stage_key]['label']}»")
            self.open_wave(deal, stage_key, 0)
        elif held:
            self.try_advance(deal, stage_key)
        else:
            self.heal(deal, stage_key, wave)

    def heal(self, deal: dict, stage_key: str, wave: int) -> None:
        """Волна открыта, а задач нет: создание упало (Twenty или сеть не ответили). Досоздаём недостающие.

        Без этого сделка стоит навсегда: мост уже записал этап и волну, а закрывать нечего.
        Пока в волне есть открытая блокирующая задача, ничего не трогаем (флаг мог поменяться позже — это не сбой).
        """
        waves = STAGES[stage_key].get("waves", [])
        if wave >= len(waves):
            self.try_advance(deal, stage_key)  # этап пройден, а перевод в CRM не прошёл — повторяем (без next ничего не делает)
            return
        if self.store.wave_open_blocking(deal["id"], stage_key, wave):
            return
        # Сверяем по роли и карточке, а не по тексту шага: текст в stages.yaml могли поправить после создания.
        have = [(r, c) for r, c in self.store.wave_roles(deal["id"], stage_key, wave, self.store.stage_since(deal["id"]))]
        missing = []
        for s in waves[wave]:
            if not self.needed(deal, s):
                continue
            if (s["role"], s["card"]) in have:
                have.remove((s["role"], s["card"]))
            else:
                missing.append(s)
        if missing:
            log.warning("deal %s: wave %s/%s without tasks, creating %d", deal.get("name"), stage_key, wave, len(missing))
            self.create_tasks(deal, STAGES[stage_key], wave, missing)
        else:
            self.open_wave(deal, stage_key, wave + 1)  # задачи волны закрыты, а следующая не открылась

    def on_task_done(self, task_id: str) -> None:
        row = self.store.mark_done(task_id)
        if not row:
            return
        deal_id, stage_key, wave = row
        role, card = self.store.db.execute("select role, card from task where id=?", (task_id,)).fetchone()
        person = PROCESS["roles"][role]["person"]
        stage_label = STAGES.get(stage_key, {}).get("label", stage_key)
        self.store.log(deal_id, "done", f"{person} подтвердил(а) задачу, карточка {card} · «{stage_label}»")
        known_stage, current_wave, _ = self.store.deal(deal_id)
        if known_stage != stage_key or current_wave != wave:
            return  # закрыли неблокирующую задачу прошлого этапа
        if self.store.wave_open_blocking(deal_id, stage_key, wave) == 0:
            deal = self.crm.get("opportunities", deal_id)
            self.on_deal(deal)

    # --- поля бота в CRM ----------------------------------------------------
    def crm_fields(self, deal_id: str) -> dict | None:
        """«Сейчас на ком», «Дальше», «Статус процесса» по состоянию процесса (без LLM)."""
        stage_key, wave, held = self.store.deal(deal_id)
        if stage_key not in STAGES:
            return None
        stage = STAGES[stage_key]
        rows = self.store.db.execute(
            "select role, card, due from task where deal_id=? and stage=? and wave=? and blocking=1 and done=0",
            (deal_id, stage_key, wave),
        ).fetchall()
        now = datetime.now(timezone.utc)
        late = any(due and datetime.fromisoformat(due.replace("Z", "+00:00")) < now for _, _, due in rows)
        people = " · ".join(f"{PROCESS['roles'][r]['person']} ({c})" for r, c, _ in rows)
        if stage_key == "OPLACHENO":
            return {"nowOn": "Всё сделано", "nextStep": "", "processStatus": "Оплачено"}
        if stage.get("service"):
            return {"nowOn": "", "nextStep": "", "processStatus": stage["label"]}
        if held:
            now_on = f"Стоит: {held}"
        else:
            now_on = people or "Бот переводит дальше"
        nxt = [f"{PROCESS['roles'][s['role']]['person']} ({s['card']})"
               for w in stage.get("waves", [])[wave + 1:] for s in w if s.get("blocking", True)]
        nxt_stage = STAGES[stage["next"]]["label"] if stage.get("next") else ""
        next_step = " → ".join(nxt + ([f"этап «{nxt_stage}»"] if nxt_stage else []))
        status = "Стоит" if held else "Просрочено" if late else "По плану"
        return {"nowOn": now_on, "nextStep": next_step, "processStatus": status}

    # --- process -------------------------------------------------------------
    def open_wave(self, deal: dict, stage_key: str, wave: int) -> None:
        stage = STAGES[stage_key]
        waves = stage.get("waves", [])
        while wave < len(waves):
            steps = [s for s in waves[wave] if self.needed(deal, s)]
            if steps:
                self.store.set_deal(deal["id"], stage_key, wave)
                self.create_tasks(deal, stage, wave, steps)
                if any(s.get("blocking", True) for s in steps):
                    return
            wave += 1
        self.store.set_deal(deal["id"], stage_key, wave)
        self.try_advance(deal, stage_key)

    def try_advance(self, deal: dict, stage_key: str) -> None:
        stage = STAGES[stage_key]
        nxt = stage.get("next")
        if not nxt:
            return
        _, cur_wave, _ = self.store.deal(deal["id"])
        if self.store.wave_open_blocking(deal["id"], stage_key, cur_wave):
            return  # открыта задача проблемной ветки: переход держит она
        gate = stage.get("gate")
        if gate and self.flag(deal, gate.get("when")) and not self.flag(deal, gate["flag"]):
            _, wave, held = self.store.deal(deal["id"])
            if not held:
                log.info("deal %s held at %s: %s", deal.get("name"), stage_key, gate["message"])
                self.store.set_deal(deal["id"], stage_key, wave, held=gate["message"])
                self.store.log(deal["id"], "held", f"Стоит: {gate['message']}")
            return
        while nxt and not self.needed(deal, STAGES[nxt]):
            nxt = STAGES[nxt].get("next")
        log.info("deal %s: advance %s -> %s", deal.get("name"), stage_key, nxt)
        self.crm.update("opportunities", deal["id"], {"stage": nxt})
        self.store.log(deal["id"], "stage", f"Бот перевёл сделку: «{stage['label']}» → «{STAGES[nxt]['label']}»")
        deal = {**deal, "stage": nxt}
        self.open_wave(deal, nxt, 0)

    def create_tasks(self, deal: dict, stage: dict, wave: int, steps: list[dict]) -> None:
        by_role: dict[str, list[dict]] = {}
        for step in steps:
            by_role.setdefault(step["role"], []).append(step)
        # Один срок для всей волны. Карточки запаздывающих агентов создаются по шаблону процесса.
        futures = {
            role: self.draft_pool.submit(self.agents.draft, PROCESS["roles"][role]["agent"], role, stage, deal, role_steps)
            for role, role_steps in by_role.items()
        }
        ready, pending = wait(futures.values(), timeout=DRAFT_WAIT_SECONDS)
        if pending:
            log.info("deal %s: using task templates for %d slow agents at %s/%s",
                     deal["id"], len(pending), stage["key"], wave)
        for future in pending:
            future.cancel()
        for role, role_steps in by_role.items():
            future = futures[role]
            drafts = future.result() if future in ready else [Agents._template(step) for step in role_steps]
            for step, draft in zip(role_steps, drafts):
                self._create_task(deal, stage, wave, step, draft)

    def _create_task(self, deal: dict, stage: dict, wave: int, step: dict, draft: dict, extra: str = "") -> str:
        role = PROCESS["roles"][step["role"]]
        opts = options_of(step)
        if len(opts) > 1:
            extra += "**Варианты ответа:** " + " / ".join(o["label"] for o in opts) + "\n\n"
        source = "агент Dify" if draft["by_agent"] else "шаблон процесса"
        body = (
            f"**{BOT_MARK}** ({source})\n\n"
            f"Роль: {role['person']} · {role['name']}  \nЭтап: {stage['label']} · карточка {step['card']}\n\n"
            f"{draft['body']}\n\n---\n"
            f"{extra}Ответ — кнопкой варианта в чате агента («Что на мне сегодня») или отметкой задачи выполненной. "
            f"{'Когда закрыты все задачи этапа, сделка перейдёт дальше.' if step.get('blocking', True) else 'Задача не держит переход этапа.'}"
        )
        payload: dict = {
            "title": f"[{role['person']}] {step['card']} · {draft['title']}"[:200],
            "bodyV2": {"markdown": body},
            "status": "TODO",
        }
        member = os.getenv(f"TWENTY_MEMBER_{step['role'].upper()}") or self.default_member
        if member:
            payload["assigneeId"] = member
        if step.get("due_days"):
            payload["dueAt"] = (datetime.now(timezone.utc) + timedelta(days=step["due_days"])).isoformat()
        task = self.crm.create("tasks", payload)
        self.crm.create("taskTargets", {"taskId": task["id"], "targetOpportunityId": deal["id"]})
        self.store.add_task(task["id"], deal["id"], stage["key"], wave, step, payload.get("dueAt"))
        self.store.log(deal["id"], "task", f"Задача для {role['person']}: {draft['title']}")
        log.info("task for %s: %s", role["person"], payload["title"])
        return task["id"]

    # --- ответы на задачу: варианты, проблемные ветки -------------------------
    def apply_option(self, task_id: str, idx: int, reason: str | None = None) -> dict:
        """Человек выбрал вариант ответа: флаги в сделку, задача ветки, итог в задачу, задача закрыта.

        Переход по волнам делает обычная синхронизация: она увидит закрытую задачу в CRM.
        """
        row = self.store.task_row(task_id)
        _, deal_id, stage_key, wave, role, card, _, _ = row
        opts = options_of(step_of(row))
        opt = opts[idx] if 0 <= idx < len(opts) else opts[0]
        person = PROCESS["roles"][role]["person"]
        deal = self.crm.get("opportunities", deal_id)
        if opt.get("set"):
            self.crm.update("opportunities", deal_id, opt["set"])
            deal = {**deal, **opt["set"]}
        note = f"{person}: «{opt['label']}»" + (f". Причина: {reason}" if reason else "")
        problem = bool(opt.get("then") or opt.get("stage") or reason)
        self.store.log(deal_id, "problem" if problem else "answer", note)
        branch = None
        cur_stage, cur_wave, _ = self.store.deal(deal_id)
        if opt.get("then") and cur_stage in STAGES:
            branch = {**BRANCHES[opt["then"]], "branch": opt["then"]}
            self._create_task(deal, STAGES[cur_stage], cur_wave, branch, Agents._template(branch),
                              f"**Откуда задача:** {note}\n\n")
        # Прогноз считаем до закрытия задачи в CRM: потом её подхватит опрос и откроет следующий шаг.
        forecast = "" if opt.get("stage") else self.forecast(deal, stage_key, wave, task_id)
        task = self.crm.get("tasks", task_id) or {}
        body = ((task.get("bodyV2") or {}).get("markdown") or "").rstrip()
        body += f"\n\n**Ответ:** {opt['label']}" + (f"  \n**Причина:** {reason}" if reason else "")
        self.crm.update("tasks", task_id, {"status": "DONE", "bodyV2": {"markdown": body}})
        if opt.get("stage"):
            self.move(deal, opt["stage"], note)
        return {"option": opt, "person": person, "title": task.get("title"), "branch": branch,
                "deal": deal, "stage": stage_key, "wave": wave, "card": card, "forecast": forecast}

    def move(self, deal: dict, stage_key: str, why: str) -> None:
        """Перевод в служебный этап по решению человека (Отложена / Отменена)."""
        self.crm.update("opportunities", deal["id"], {"stage": stage_key})
        self.store.set_deal(deal["id"], stage_key, 0)
        self.store.log(deal["id"], "stage", f"Сделка в «{STAGES[stage_key]['label']}» — {why}")

    @staticmethod
    def who(steps: list[dict]) -> str:
        def short(task: str) -> str:
            first = task.split(".")[0]
            return first[:1].lower() + first[1:]
        return "; ".join(f"{PROCESS['roles'][s['role']]['person']} ({s['card']}) — {short(s['task'])}" for s in steps)

    def forecast(self, deal: dict, stage_key: str, wave: int, closing: str) -> str:
        """Что будет после закрытия задачи: кого ещё ждём или кому уходит карточка (без LLM)."""
        cur_stage, cur_wave, _ = self.store.deal(deal["id"])
        if cur_stage != stage_key:
            return f"Сделка сейчас на этапе «{STAGES.get(cur_stage, {}).get('label', cur_stage)}»."
        if cur_wave != wave:
            return "Эта задача переход не держала, сделка остаётся на своём шаге."
        others = self.store.db.execute(
            "select role, card from task where deal_id=? and stage=? and wave=? and blocking=1 and done=0 and id<>?",
            (deal["id"], stage_key, wave, closing),
        ).fetchall()
        if others:
            return "На этом шаге ещё ждём: " + ", ".join(f"{PROCESS['roles'][r]['person']} ({c})" for r, c in others) + "."
        stage = STAGES[stage_key]
        for w in stage.get("waves", [])[wave + 1:]:
            steps = [s for s in w if self.needed(deal, s)]
            if steps:
                return "Карточка уходит дальше: " + self.who(steps) + "."
        gate = stage.get("gate")
        if gate and self.flag(deal, gate.get("when")) and not self.flag(deal, gate["flag"]):
            return f"Шаг закрыт, но сделка встанет: {gate['message']}."
        nxt = stage.get("next")
        while nxt and not self.needed(deal, STAGES[nxt]):
            nxt = STAGES[nxt].get("next")
        if not nxt:
            return "Это был последний шаг процесса."
        first = next(([s for s in w if self.needed(deal, s)] for w in STAGES[nxt].get("waves", [])
                      if any(self.needed(deal, s) for s in w)), [])
        return f"Бот переводит сделку на этап «{STAGES[nxt]['label']}»." + (f" Дальше: {self.who(first)}." if first else "")
