"""Собирает страницу-артефакт на каждого агента из dify-apps/*.yml и process/stages.yaml.

На странице: кто пользуется агентом, модель, его шаги в процессе, подсказки для чата и полный YAML с кнопкой «Скопировать».
Ссылки опубликованных артефактов хранятся в artifacts/urls.json — обновление идёт по тем же ссылкам.

Запуск:  .venv\\Scripts\\python tools\\gen_artifacts.py
"""

from __future__ import annotations

import html
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "artifacts"

STYLE = """
<style>
/* Layout: one column dossier — header with who/model, then the agent's process steps, then the DSL listing. */
:root{
  --bg:#F2F4F7; --surface:#FFFFFF; --sunken:#EAEEF3; --ink:#141922; --muted:#586172; --faint:#8A93A2;
  --line:#D9DEE6; --accent:#2747C9; --accent-soft:#E6EBFB; --code-bg:#0F1420; --code-ink:#D7DEEA; --ok:#16774A;
  --f-ui:"Onest",system-ui,-apple-system,"Segoe UI",sans-serif; --f-mono:"JetBrains Mono",ui-monospace,Consolas,monospace;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#12151B; --surface:#1A1F28; --sunken:#222833; --ink:#E7EAF0; --muted:#A3ACBB; --faint:#78818F;
  --line:#2E3542; --accent:#8EA4FF; --accent-soft:#232C4D; --code-bg:#0B0E15; --code-ink:#D7DEEA; --ok:#7AD3A5; color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#12151B; --surface:#1A1F28; --sunken:#222833; --ink:#E7EAF0; --muted:#A3ACBB; --faint:#78818F;
  --line:#2E3542; --accent:#8EA4FF; --accent-soft:#232C4D; --code-bg:#0B0E15; --code-ink:#D7DEEA; --ok:#7AD3A5; color-scheme:dark}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--ink);font-family:var(--f-ui);font-size:15px;line-height:1.5}
.wrap{max-width:980px;margin:0 auto;padding-block:28px 56px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));display:grid;gap:28px}
h1,h2{margin:0;line-height:1.2;text-wrap:balance}
h1{font-size:28px;font-weight:700}
h2{font-size:18px;font-weight:650}
p{margin:0;max-width:70ch}
.eyebrow{font-family:var(--f-mono);font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--faint)}
header{display:grid;gap:10px}
.meta{display:flex;flex-wrap:wrap;gap:8px}
.chip{font-family:var(--f-mono);font-size:12px;padding:3px 9px;border-radius:999px;background:var(--sunken);color:var(--muted)}
.chip.a{background:var(--accent-soft);color:var(--accent)}
section{display:grid;gap:12px}
.steps{display:grid;gap:0;background:var(--surface);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.step{display:grid;grid-template-columns:150px 1fr;gap:12px;padding:10px 14px;border-top:1px solid var(--line)}
.step:first-child{border-top:0}
.step .where{font-size:12px;color:var(--faint)}
.step .where b{display:block;font-family:var(--f-mono);font-size:13px;color:var(--accent);font-weight:500}
.step .what{min-width:0}
.step ul{margin:4px 0 0;padding-left:18px;color:var(--muted);font-size:13px}
.qs{display:flex;flex-wrap:wrap;gap:8px}
.q{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:6px 10px;font-size:14px}
.code-head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
button{font:500 13px var(--f-ui);background:var(--accent);color:#fff;border:0;border-radius:6px;padding:7px 12px;cursor:pointer}
button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.copied{font-size:13px;color:var(--ok)}
pre{margin:0;background:var(--code-bg);color:var(--code-ink);border-radius:10px;padding:16px;overflow:auto;max-height:70vh;font:12.5px/1.55 var(--f-mono);white-space:pre}
.how{color:var(--muted);font-size:14px;display:grid;gap:4px}
.how code{font-family:var(--f-mono);font-size:12.5px;background:var(--sunken);padding:1px 5px;border-radius:4px;color:var(--ink)}
.flow{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px;overflow-x:auto}
.flow pre.mermaid{background:transparent;color:var(--ink);max-height:none;padding:0;white-space:pre}
@media (max-width:560px){.step{grid-template-columns:1fr;gap:4px}}
</style>
"""

SCRIPT = """
<script>
const btn=document.getElementById('copy'), note=document.getElementById('copied'), src=document.getElementById('yaml');
btn.addEventListener('click',async()=>{
  try{await navigator.clipboard.writeText(src.textContent);note.hidden=false;note.textContent='Скопировано';}
  catch(e){const r=document.createRange();r.selectNodeContents(src);const s=getSelection();s.removeAllRanges();s.addRange(r);note.hidden=false;note.textContent='Выделено: нажмите Ctrl+C';}
});
</script>
"""


def steps_html(process: dict, roles: list[str]) -> str:
    rows = []
    for stage in process["stages"]:
        for wave in stage.get("waves", []):
            for step in wave:
                if step["role"] not in roles:
                    continue
                person = process["roles"][step["role"]]["person"]
                cond = f" · если {html.escape(process['flags'][step['when']].lower())}" if step.get("when") else ""
                rules = "".join(f"<li>{html.escape(r)}</li>" for r in step.get("rules", []))
                rows.append(
                    f'<div class="step"><div class="where"><b>{html.escape(step["card"])}</b>{html.escape(stage["label"])} · {html.escape(person)}{cond}</div>'
                    f'<div class="what">{html.escape(step["task"])}{f"<ul>{rules}</ul>" if rules else ""}</div></div>'
                )
    return "\n".join(rows)


def flow_mermaid(app: dict) -> str:
    """Схема графа агента из DSL: узлы и переходы с подписями веток."""
    g = app["workflow"]["graph"]
    titles = {n["id"]: n["data"].get("title", n["id"]) for n in g["nodes"]}
    kinds = {n["id"]: n["data"]["type"] for n in g["nodes"]}
    case_names: dict[tuple[str, str], str] = {}
    for n in g["nodes"]:
        d = n["data"]
        if d["type"] == "if-else":
            for c in d.get("cases", []):
                val = c["conditions"][0]["value"]
                case_names[(n["id"], c["case_id"])] = "мост" if c["case_id"] == "bridge" else val
            case_names[(n["id"], "false")] = "чат" if n["id"] == "mode" else "иначе"
        if d["type"] == "question-classifier":
            for c in d["classes"]:
                case_names[(n["id"], c["id"])] = c["id"]

    def label(nid: str) -> str:
        t = titles[nid].replace('"', "'")
        shape = {"if-else": ('{{"', '"}}'), "question-classifier": ('{{"', '"}}'), "answer": ('(["', '"])'), "start": ('(["', '"])')}.get(kinds[nid], ('["', '"]'))
        return f'{nid}{shape[0]}{t}{shape[1]}'

    lines = ["flowchart LR"]
    for e in g["edges"]:
        tag = case_names.get((e["source"], e["sourceHandle"]))
        arrow = f' -->|{tag}| ' if tag else " --> "
        lines.append(f"  {label(e['source'])}{arrow}{label(e['target'])}")
    return "\n".join(lines)


def page(process: dict, agent_key: str, yml_text: str) -> str:
    app = yaml.safe_load(yml_text)
    agent = process["agents"][agent_key]
    people = ", ".join(f"{process['roles'][r]['person']} ({process['roles'][r]['name'].lower()})" for r in agent["roles"])
    llm = next(n["data"] for n in app["workflow"]["graph"]["nodes"] if n["data"]["type"] == "llm")
    model = f"{llm['model']['provider'].split('/')[-1]} · {llm['model']['name']}"
    questions = "".join(f'<span class="q">{html.escape(q)}</span>' for q in app["workflow"]["features"].get("suggested_questions", []))
    name = app["app"]["name"]
    return f"""<title>{html.escape(agent['title'])} Entryx</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500&family=Onest:wght@400;500;600;700&display=swap">
{STYLE}
<div class="wrap">
<header>
  <div class="eyebrow">Агент Dify · {html.escape(name)}.yml</div>
  <h1>{html.escape(agent['title'])}</h1>
  <p>Работает для: {html.escape(people)}. В чате Dify отвечает сотруднику и готовит черновики. Для моста по сообщению «РЕЖИМ: ЗАДАЧИ» возвращает задачи в JSON, человек подтверждает их в CRM.</p>
  <div class="meta"><span class="chip a">{html.escape(model)}</span><span class="chip">chatflow</span><span class="chip">данные CRM: мост /context?agent={html.escape(agent_key)}</span></div>
</header>

<section>
  <h2>Флоу агента</h2>
  <p class="how">Мост присылает «РЕЖИМ: ЗАДАЧИ» с кодом этапа — агент идёт в узел этого этапа. Сотрудник пишет в чат — классификатор выбирает тип вопроса.</p>
  <div class="flow"><pre class="mermaid">
{html.escape(flow_mermaid(app))}
  </pre></div>
</section>

<section>
  <h2>Шаги процесса этого агента</h2>
  <div class="steps">
{steps_html(process, agent['roles'])}
  </div>
</section>

<section>
  <h2>С чего начать в чате</h2>
  <div class="qs">{questions}</div>
</section>

<section>
  <div class="code-head"><h2>DSL для импорта в Dify</h2><div><span id="copied" class="copied" hidden></span> <button id="copy" type="button">Скопировать YAML</button></div></div>
  <pre id="yaml">{html.escape(yml_text)}</pre>
  <div class="how">
    <span>Импорт вручную: Dify → Студия → Импорт DSL → вставить YAML.</span>
    <span>Источник — <code>process/stages.yaml</code>. После правки: <code>tools\\gen_dify_apps.py</code>, <code>tools\\dify_setup.py --overwrite</code>, <code>tools\\gen_artifacts.py</code>, затем переопубликовать эту страницу.</span>
  </div>
</section>
</div>
{SCRIPT}"""


def main() -> None:
    process = yaml.safe_load((ROOT / "process" / "stages.yaml").read_text(encoding="utf-8"))
    OUT.mkdir(exist_ok=True)
    for key in process["agents"]:
        src = ROOT / "dify-apps" / f"entryx-{key}.yml"
        (OUT / f"entryx-{key}.html").write_text(page(process, key, src.read_text(encoding="utf-8")), encoding="utf-8")
        print("wrote", f"artifacts/entryx-{key}.html")
    urls = OUT / "urls.json"
    if not urls.exists():
        urls.write_text(json.dumps({}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
