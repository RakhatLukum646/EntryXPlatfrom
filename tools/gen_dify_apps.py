"""Генерирует агентов Dify (DSL YAML, chatflow) из process/stages.yaml.

Граф каждого агента:

  Start → Данные CRM (HTTP к мосту) → Режим?
     ├─ «РЕЖИМ: ЗАДАЧИ» (мост) → Этап сделки? ─┬─ Этап A → LLM «A: задачи» → Answer
     │                                          ├─ Этап B → LLM «B: задачи» → Answer
     │                                          └─ иначе  → LLM «Задачи: общий» → Answer
     ├─ кнопка, вариант ответа, «готово» или причина → мост /chat → Answer (без LLM)
     └─ чат сотрудника → Дела с кнопками (мост) → Тип вопроса? ─┬─ статус / что горит → LLM → Answer + дела
                                         ├─ письмо или сообщение  → LLM → Answer
                                         ├─ проверка документа    → LLM → Answer
                                         └─ вопрос по процессу    → LLM → Answer

На каждый этап, где у ролей агента есть шаги, свой узел со своими шагами, правилами и
черновиками — так флоу агента повторяет его путь по воронке.

Запуск:  python tools/gen_dify_apps.py [--provider langgenius/gemini/google --model gemini-flash-latest]
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dify-apps"

TASK_MODE = "РЕЖИМ: ЗАДАЧИ"
BRIDGE_URL = "http://host.docker.internal:8787"
PUBLIC_BRIDGE = "http://localhost:8787"  # ссылки, которые открывает браузер сотрудника
S, H, MODE, STAGE_SW, QC, BTN, ACT = "start", "crm", "mode", "stage_switch", "intent", "buttons", "actions"
# Команды кнопок чата — те же строки, что в bridge/chat_views.py.
CMD_TODAY, CMD_PROJECTS, CMD_CONFIRM, CMD_DRAFT = "Сегодня", "Мои проекты", "Подтвердить:", "Черновик:"
CMD_OPTION, CMD_ACTIONS, PENDING_MARK = "Вариант:", "__actions__", "ОЖИДАЮ ПРИЧИНУ"


def menu(agent_key: str) -> str:
    return (f'<button data-variant="primary" data-message="{CMD_TODAY}">Что на мне сегодня</button> '
            f'<button data-message="{CMD_PROJECTS}">Мои проекты</button>\n\n'
            f'[🔔 Уведомления]({PUBLIC_BRIDGE}/app?agent={agent_key}#inbox)')

PRINCIPLES = """Правила Entryx AI:
- Бот ничего не делает сам. Ты готовишь задачи, черновики писем и проверки; человек подтверждает.
- Документ принимается только после проверки по пунктам: тип, подпись и печать, совпадение с заявкой, даты, сумма.
- Пиши по-русски, коротко, конкретно. Подставляй данные сделки (номер, объект, завод, даты, количество). Если данных не хватает, перечисли, что нужно уточнить, и не выдумывай.
- Письма внешним участникам (завод, заказчик) — деловым тоном, с номером заказа и счёта. Сообщения в WhatsApp (водитель, начальник участка, грузчики) — коротко.
- Письма и сообщения подписывай именем и должностью сотрудника («Юлия, закупщик ENTRYX»), а не агентом: их отправляет человек.
- Отвечай только про свою роль. Вопрос по чужой роли — скажи, чей это шаг и кого спросить.
- Ты не закрываешь задачи и не переводишь сделки: у тебя нет такого доступа. Никогда не пиши «закрываю задачу», «перевёл сделку», «отметил в CRM».
  Задачу закрывает сотрудник: кнопкой варианта под твоим ответом («Ваши дела сейчас») или словом «готово». Тогда бот передаёт карточку следующему.
  Если просят закрыть, подтвердить или перевести — скажи нажать кнопку варианта под этим ответом."""

# Слова, которыми сотрудник подтверждает текстом: такие сообщения уходят в список дел с кнопками, а не в LLM,
# чтобы модель не «подтверждала» то, чего не делала.
CONFIRM_WORDS = ["подтвержда", "Подтвержда", "готово", "Готово", "сделал", "Сделал", "выполнил", "Выполнил",
                 "закрой", "Закрой", "перевед", "Перевед", "переведи", "Переведи", "перекин", "Перекин"]

TASK_CONTRACT = """Ответ — только JSON без пояснений и без ```:
{"tasks": [{"card": "<номер карточки>", "title": "<до 80 символов, начинается с глагола>",
  "body": "<markdown: что сделать по пунктам; что проверить; черновик письма или сообщения, если шаг его требует; что произойдёт после подтверждения>"}]}
Одна задача на каждый шаг из «Шаги, которые открылись», в том же порядке."""

LETTER_WORDS = re.compile(r"письм|сообщени|запрос|отправ|напомина|требовани|уточн|чек-лист", re.I)
DOC_WORDS = re.compile(r"провер|свер|скан|документ|доверенност|накладн|акт|спецификац|счёт|СНТ|ведомост|платёжк", re.I)

INTENTS = [
    ("status", "Статус и приоритеты: что на мне, какие задачи, что горит или просрочено, где сделка, какие сроки"),
    ("letter", "Подготовить письмо, сообщение, напоминание или ответ заводу, заказчику, водителю, перевозчику, начальнику участка, грузчикам"),
    ("doc", "Проверить документ или комплект: спецификацию, счёт, накладную, доверенность, акт, сканы; что должно быть в документе"),
    ("process", "Вопрос по процессу: как сделать шаг, какие правила, чей шаг, что будет дальше, другие вопросы"),
]


# ---------- знания из процесса ----------------------------------------------------------

def agent_steps(process: dict, roles: list[str]) -> list[tuple[dict, dict]]:
    return [
        (stage, step)
        for stage in process["stages"]
        for wave in stage.get("waves", [])
        for step in wave
        if step["role"] in roles
    ]


def step_line(process: dict, stage: dict, step: dict) -> str:
    person = process["roles"][step["role"]]["person"]
    cond = f" (если: {process['flags'][step['when']]})" if step.get("when") else ""
    due = f" Срок: {step['due_days']} дн." if step.get("due_days") else ""
    lines = [f"- [{stage['label']} · карточка {step['card']} · {person}]{cond} {step['task']}{due}"]
    lines += [f"    правило: {r}" for r in step.get("rules", [])]
    return "\n".join(lines)


def header(process: dict, agent_key: str) -> str:
    agent = process["agents"][agent_key]
    people = ", ".join(f"{process['roles'][r]['person']} ({process['roles'][r]['name']})" for r in agent["roles"])
    funnel = " → ".join(s["label"] for s in process["stages"] if not s.get("service"))
    return (
        f"Ты — {agent['title']} в Entryx AI. Компания ENTRYX поставляет двери от подписанной спецификации заказчика до оплаты.\n"
        f"Ты работаешь для: {people}.\nВоронка сделки: {funnel}.\n\n{PRINCIPLES}"
    )


def crm_block() -> str:
    return f"Данные CRM сейчас (сделки и открытые задачи твоей роли):\n{{{{#{H}.body#}}}}"


def bridge_block() -> str:
    return (
        f"Контекст от моста:\nРоль: {{{{#{S}.role#}}}}\nЭтап: {{{{#{S}.stage#}}}}\n"
        f"Сделка: {{{{#{S}.deal#}}}}\nШаги, которые открылись: {{{{#{S}.steps#}}}}"
    )


def stage_prompt(process: dict, agent_key: str, stage: dict, steps: list[dict]) -> str:
    body = "\n".join(step_line(process, stage, s) for s in steps)
    flags = ", ".join(f"{k} — {v}" for k, v in process["flags"].items())
    return (
        f"{header(process, agent_key)}\n\n"
        f"Сделка на этапе «{stage['label']}». Твои шаги на этом этапе:\n{body}\n\n"
        f"Как готовить задачу этапа:\n"
        f"- Начни с того, что именно сделать сегодня по этой сделке, с цифрами из сделки.\n"
        f"- Если шаг про письмо или сообщение — дай готовый черновик (Кому, Тема, Текст).\n"
        f"- Если шаг про документ — дай чек-лист проверки по пунктам и что делать, если пункт не прошёл.\n"
        f"- Напомни правила и эскалации этапа, если они есть.\n"
        f"- В конце: что произойдёт после подтверждения (какой шаг откроется дальше).\n"
        f"Флаги сделки: {flags}.\n\n"
        f"{bridge_block()}\n\n{crm_block()}\n\n{TASK_CONTRACT}"
    )


def generic_task_prompt(process: dict, agent_key: str, all_steps: str) -> str:
    return (
        f"{header(process, agent_key)}\n\nТвои шаги в процессе:\n{all_steps}\n\n"
        f"{bridge_block()}\n\n{crm_block()}\n\n{TASK_CONTRACT}"
    )


def intent_prompt(process: dict, agent_key: str, intent: str, pairs: list[tuple[dict, dict]]) -> str:
    base = header(process, agent_key)
    all_steps = "\n".join(step_line(process, st, sp) for st, sp in pairs)
    if intent == "status":
        rules = "\n".join(f"- [{st['label']} · {sp['card']}] {r}" for st, sp in pairs for r in sp.get("rules", []) if "эскалац" in r.lower() or "дн" in r.lower())
        return (
            f"{base}\n\n{crm_block()}\n\n"
            "Ответь как сводка на утро:\n"
            "1. Сначала горящее: просрочено, срок сегодня или завтра, что держит переход сделки.\n"
            "2. Потом по каждой сделке: этап, твои открытые задачи, срок, следующий шаг.\n"
            "3. В конце одна строка: с чего начать прямо сейчас.\n"
            "Если CRM недоступна — скажи это и перечисли, что обычно проверять на твоих этапах.\n\n"
            f"Нормы и эскалации твоей роли:\n{rules or '- нет'}"
        )
    if intent == "letter":
        letters = "\n".join(step_line(process, st, sp) for st, sp in pairs if LETTER_WORDS.search(sp["task"])) or all_steps
        return (
            f"{base}\n\n{crm_block()}\n\n"
            f"Письма и сообщения, которые отправляет твоя роль:\n{letters}\n\n"
            "Подготовь черновик:\n"
            "- Канал: почта (завод, заказчик) или WhatsApp (водитель, начальник участка, грузчики), УчётЭДО (перевозчик).\n"
            "- Формат: Кому · Тема (для почты) · Текст · Что приложить.\n"
            "- Подставь данные сделки из CRM. Чего нет — оставь [в квадратных скобках] и перечисли после черновика.\n"
            "- Ниже одной строкой: что ответить адресату и что сделать после ответа."
        )
    if intent == "doc":
        docs = "\n".join(step_line(process, st, sp) for st, sp in pairs if DOC_WORDS.search(sp["task"])) or all_steps
        return (
            f"{base}\n\n{crm_block()}\n\n"
            f"Документы и проверки твоей роли:\n{docs}\n\n"
            "Дай чек-лист проверки документа:\n"
            "☐ тип документа · ☐ подпись и печать · ☐ совпадение с заявкой и спецификацией (позиции, количество, размеры) · ☐ даты и сроки · ☐ сумма и реквизиты · ☐ особые правила этапа.\n"
            "Для каждого пункта: что именно сверить с чем. Затем: что делать, если пункт не прошёл (кому написать, держит ли это переход)."
        )
    return (
        f"{base}\n\nТвои шаги в процессе (карта процессов v4):\n{all_steps}\n\n{crm_block()}\n\n"
        "Отвечай по процессу: какой шаг, кто делает, срок, правила, что откроется дальше. Если шаг не твой — назови роль."
    )


# ---------- узлы DSL --------------------------------------------------------------------

def node(node_id: str, x: int, y: int, data: dict) -> dict:
    return {
        "data": {"desc": "", "selected": False, **data}, "height": 90, "id": node_id,
        "position": {"x": x, "y": y}, "positionAbsolute": {"x": x, "y": y},
        "selected": False, "sourcePosition": "right", "targetPosition": "left",
        "type": "custom", "width": 244,
    }


def edge(src: str, src_type: str, dst: str, dst_type: str, handle: str = "source") -> dict:
    return {
        "data": {"isInIteration": False, "isInLoop": False, "sourceType": src_type, "targetType": dst_type},
        "id": f"{src}-{handle}-{dst}-target", "source": src, "sourceHandle": handle,
        "target": dst, "targetHandle": "target", "type": "custom", "zIndex": 0,
    }


def hidden_var(name: str, label: str, kind: str = "text-input", max_length: int = 256) -> dict:
    # hide: поле заполняет только мост, в веб-чате сотрудник его не видит.
    return {"label": label, "max_length": max_length, "options": [], "required": False, "hide": True, "type": kind, "variable": name}


def llm(title: str, system: str, model: dict, memory: bool) -> dict:
    data = {
        "context": {"enabled": False, "variable_selector": []},
        "model": model,
        "prompt_template": [{"role": "system", "text": system}],
        "title": title, "type": "llm", "variables": [], "vision": {"enabled": False},
    }
    if memory:
        data["memory"] = {"query_prompt_template": "{{#sys.query#}}", "role_prefix": {"assistant": "", "user": ""}, "window": {"enabled": True, "size": 20}}
    else:
        data["memory"] = {"query_prompt_template": "{{#sys.query#}}", "role_prefix": {"assistant": "", "user": ""}, "window": {"enabled": False, "size": 1}}
    return data


def cond(var: list[str], op: str, value: str, cid: str) -> dict:
    return {"comparison_operator": op, "id": cid, "value": value, "varType": "string", "variable_selector": var}


def build_app(process: dict, agent_key: str, provider: str, model_name: str) -> dict:
    agent = process["agents"][agent_key]
    person = process["roles"][agent["roles"][0]]["person"]
    pairs = agent_steps(process, agent["roles"])
    all_steps = "\n".join(step_line(process, st, sp) for st, sp in pairs)
    def model_cfg(thinking: str) -> dict:
        # thinking_level у Gemini по умолчанию High — это ~15 с на ответ. Нам хватает Low.
        # Minimal gemini-flash-latest не принимает («not supported for this model»).
        params: dict = {"temperature": 0.2}
        if "gemini" in model_name:
            params["thinking_level"] = thinking
        return {"completion_params": params, "mode": "chat", "name": model_name, "provider": provider}

    model = model_cfg("Low")

    stages: dict[str, tuple[dict, list[dict]]] = {}
    for st, sp in pairs:
        stages.setdefault(st["key"], (st, []))[1].append(sp)

    nodes: list[dict] = []
    edges: list[dict] = []
    X = [30, 330, 630, 930, 1230, 1530]
    row = 0

    def place_branch(src: str, src_type: str, handle: str, llm_id: str, title: str, system: str, memory: bool) -> None:
        nonlocal row
        y = 60 + row * 130
        nodes.append(node(llm_id, X[4], y, llm(title, system, model_cfg("Low"), memory)))
        ans = f"ans_{llm_id}"
        # В чате под ответом — «Ваши дела сейчас» с кнопками вариантов (мост): поговорил и сразу передал карточку дальше.
        # Мосту (JSON задач) — только текст.
        answer = f"{{{{#{llm_id}.text#}}}}" + (f"\n\n{{{{#{ACT}.body#}}}}" if memory else "")
        nodes.append(node(ans, X[5], y, {"answer": answer, "title": f"Ответ · {title}", "type": "answer", "variables": []}))
        edges.append(edge(src, src_type, llm_id, "llm", handle))
        edges.append(edge(llm_id, "llm", ans, "answer"))
        row += 1

    # Мост: ветка по этапу сделки.
    stage_cases = []
    for key, (st, steps) in stages.items():
        cid = key.lower()
        stage_cases.append({"case_id": cid, "id": cid, "logical_operator": "and",
                            "conditions": [cond([S, "stage_key"], "is", key, f"c_{cid}")]})
        place_branch(STAGE_SW, "if-else", cid, f"llm_{cid}", f"{st['label']}: задачи", stage_prompt(process, agent_key, st, steps), False)
    place_branch(STAGE_SW, "if-else", "false", "llm_tasks_other", "Задачи: другой этап", generic_task_prompt(process, agent_key, all_steps), False)
    bridge_rows = row

    # Чат: ветка по типу вопроса.
    for cid, _ in INTENTS:
        title = {"status": "Чат: статус и что горит", "letter": "Чат: письмо или сообщение", "doc": "Чат: проверка документа", "process": "Чат: вопрос по процессу"}[cid]
        place_branch(QC, "question-classifier", cid, f"llm_chat_{cid}", title, intent_prompt(process, agent_key, cid, pairs), True)

    mid_bridge = 60 + (bridge_rows - 1) * 65
    mid_chat = 60 + (bridge_rows + (row - bridge_rows - 1) / 2) * 130
    nodes[:0] = [
        node(S, X[0], 300, {"title": "Start", "type": "start", "variables": [
            hidden_var("role", "Роль (заполняет мост)"),
            hidden_var("stage", "Этап (заполняет мост)"),
            hidden_var("stage_key", "Код этапа (заполняет мост)"),
            hidden_var("deal", "Сделка JSON (заполняет мост)", "paragraph", 20000),
            hidden_var("steps", "Шаги JSON (заполняет мост)", "paragraph", 20000),
        ]}),
        node(H, X[1], 300, {
            "title": "Данные CRM", "type": "http-request", "desc": "Живые сделки и задачи роли из Twenty через мост",
            "method": "get", "url": f"{BRIDGE_URL}/context?agent={agent_key}",
            "authorization": {"type": "no-auth", "config": None},
            "headers": "", "params": "", "body": {"type": "none", "data": []},
            "timeout": {"connect": 5, "read": 15, "write": 15},
            "retry_config": {"enabled": False, "max_retries": 1, "retry_interval": 1000},
            "error_strategy": "default-value",
            "default_value": [
                {"key": "body", "type": "string", "value": "CRM недоступна: живых данных нет. Скажи об этом и отвечай по процессу."},
                {"key": "status_code", "type": "number", "value": 0},
                {"key": "headers", "type": "object", "value": {}},
            ],
            "variables": [],
        }),
        node(MODE, X[2], 300, {"title": "Режим: мост, кнопка или чат", "type": "if-else", "cases": [
            {"case_id": "bridge", "id": "bridge", "logical_operator": "and",
             "conditions": [cond(["sys", "query"], "start with", TASK_MODE, "c_bridge")]},
            {"case_id": "buttons", "id": "buttons", "logical_operator": "or", "conditions": [
                cond(["sys", "query"], "is", CMD_TODAY, "c_today"),
                cond(["sys", "query"], "start with", "Что на мне сегодня", "c_today2"),
                cond(["sys", "query"], "is", CMD_PROJECTS, "c_projects"),
                cond(["sys", "query"], "start with", CMD_CONFIRM, "c_confirm"),
                cond(["sys", "query"], "start with", CMD_OPTION, "c_option"),
                cond(["sys", "query"], "start with", CMD_DRAFT, "c_draft"),
                # Бот ждёт причину к варианту ответа: любое следующее сообщение — в мост.
                cond([H, "body"], "start with", PENDING_MARK, "c_pending"),
                *[cond(["sys", "query"], "contains", w, f"c_word{i}") for i, w in enumerate(CONFIRM_WORDS)],
            ]},
        ]}),
        # Кнопки чата: ответ собирает мост без LLM (мгновенно). Подтверждение закрывает задачу в CRM.
        node(BTN, X[3], -120, {
            "title": "Кнопки: Сегодня / Подтвердить / Черновик / Проекты", "type": "http-request",
            "method": "get", "url": f"{BRIDGE_URL}/chat",
            "authorization": {"type": "no-auth", "config": None},
            "headers": "", "params": f"agent:{agent_key}\nq:{{{{#sys.query#}}}}", "body": {"type": "none", "data": []},
            "timeout": {"connect": 5, "read": 20, "write": 20},
            "retry_config": {"enabled": False, "max_retries": 1, "retry_interval": 1000},
            "error_strategy": "default-value",
            "default_value": [
                {"key": "body", "type": "string", "value": "Мост Entryx недоступен, список дел сейчас не показать. Проверьте, что контейнер entryx-bridge запущен."},
                {"key": "status_code", "type": "number", "value": 0},
                {"key": "headers", "type": "object", "value": {}},
            ],
            "variables": [],
        }),
        node(f"ans_{BTN}", X[4], -120, {"answer": f"{{{{#{BTN}.body#}}}}", "title": "Ответ · кнопки", "type": "answer", "variables": []}),
        # Дела сотрудника с кнопками вариантов: добавляются под каждым ответом чата.
        node(ACT, X[2] + 150, int(mid_chat) + 160, {
            "title": "Дела с кнопками (мост)", "type": "http-request",
            "method": "get", "url": f"{BRIDGE_URL}/chat",
            "authorization": {"type": "no-auth", "config": None},
            "headers": "", "params": f"agent:{agent_key}\nq:{CMD_ACTIONS}", "body": {"type": "none", "data": []},
            "timeout": {"connect": 5, "read": 15, "write": 15},
            "retry_config": {"enabled": False, "max_retries": 1, "retry_interval": 1000},
            "error_strategy": "default-value",
            "default_value": [
                {"key": "body", "type": "string", "value": menu(agent_key)},
                {"key": "status_code", "type": "number", "value": 0},
                {"key": "headers", "type": "object", "value": {}},
            ],
            "variables": [],
        }),
        node(STAGE_SW, X[3], int(mid_bridge), {"title": "Этап сделки", "type": "if-else", "cases": stage_cases}),
        node(QC, X[3], int(mid_chat), {
            "title": "Тип вопроса", "type": "question-classifier",
            "query_variable_selector": ["sys", "query"], "model": model,
            "classes": [{"id": cid, "name": name} for cid, name in INTENTS],
            "instruction": "Классифицируй сообщение сотрудника ENTRYX. Если сомневаешься — «Вопрос по процессу».",
            "vision": {"enabled": False},
        }),
    ]
    edges[:0] = [
        edge(S, "start", H, "http-request"),
        edge(H, "http-request", MODE, "if-else"),
        edge(MODE, "if-else", STAGE_SW, "if-else", "bridge"),
        edge(MODE, "if-else", BTN, "http-request", "buttons"),
        edge(BTN, "http-request", f"ans_{BTN}", "answer"),
        edge(MODE, "if-else", ACT, "http-request", "false"),
        edge(ACT, "http-request", QC, "question-classifier"),
    ]

    return {
        "app": {
            "description": f"{agent['title']}: флоу по этапам сделки для моста и чат для сотрудника. Сгенерировано из process/stages.yaml.",
            "icon": "🚪", "icon_background": "#E6EBFB", "icon_type": "emoji", "mode": "advanced-chat",
            "name": f"entryx-{agent_key}", "use_icon_as_answer_icon": False,
        },
        "dependencies": [],
        "kind": "app",
        "version": "0.6.0",
        "workflow": {
            "conversation_variables": [], "environment_variables": [],
            "features": {
                "file_upload": {"enabled": False},
                "opening_statement": f"Здравствуйте, {person}. Я {agent['title'].lower()}. Вижу ваши сделки и задачи в CRM. Нажмите «Что на мне сегодня»: у каждого дела кнопки ответа, и карточка уйдёт следующему. Или спросите что угодно: что горит, черновик письма, проверка документа; под ответом будут ваши дела с кнопками.\n\n{menu(agent_key)}",
                "retriever_resource": {"enabled": False},
                "sensitive_word_avoidance": {"enabled": False}, "speech_to_text": {"enabled": False},
                "suggested_questions": suggested(process, agent_key, pairs),
                "suggested_questions_after_answer": {"enabled": False},
                "text_to_speech": {"enabled": False, "language": "", "voice": ""},
            },
            "graph": {"edges": edges, "nodes": nodes, "viewport": {"x": 0, "y": 0, "zoom": 0.6}},
        },
    }


def suggested(process: dict, agent_key: str, pairs: list[tuple[dict, dict]]) -> list[str]:
    person = process["roles"][process["agents"][agent_key]["roles"][0]]["person"]
    out = [f"Что горит у меня ({person})?"]
    letter = next((sp for _, sp in pairs if LETTER_WORDS.search(sp["task"])), None)
    if letter:
        out.append(f"Подготовь черновик: {letter['task'].split('.')[0].lower()}")
    doc = next((sp for _, sp in pairs if DOC_WORDS.search(sp["task"])), None)
    if doc:
        out.append(f"Как проверить документ на шаге {doc['card']}?")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="langgenius/gemini/google")
    ap.add_argument("--model", default="gemini-flash-latest")
    args = ap.parse_args()
    process = yaml.safe_load((ROOT / "process" / "stages.yaml").read_text(encoding="utf-8"))
    OUT.mkdir(exist_ok=True)
    for key in process["agents"]:
        app = build_app(process, key, args.provider, args.model)
        path = OUT / f"entryx-{key}.yml"
        path.write_text(yaml.safe_dump(app, allow_unicode=True, sort_keys=False, width=1000), encoding="utf-8")
        n = len(app["workflow"]["graph"]["nodes"])
        print("wrote", path.relative_to(ROOT), f"({n} nodes)")


if __name__ == "__main__":
    main()
