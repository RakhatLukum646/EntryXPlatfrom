"""Данные для вкладки «Трекер»: где каждая сделка, на ком она, что дальше, что горит, история."""

from __future__ import annotations

from datetime import datetime, timezone

from chat_views import clean_title
from engine import PROCESS, STAGES, Engine
from twenty import Twenty

PHASES = PROCESS.get("phases", [])
PHASE_OF = {key: i for i, ph in enumerate(PHASES) for key in ph["stages"]}
SERVICE = {s["key"] for s in PROCESS["stages"] if s.get("service")}
FINAL = "OPLACHENO"


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def _person(role: str) -> dict:
    r = PROCESS["roles"][role]
    return {"person": r["person"], "role_name": r["name"], "role": role}


def tracker(crm: Twenty, engine: Engine) -> dict:
    now = datetime.now(timezone.utc)
    store = engine.store
    deals_out = []
    for deal in crm.list("opportunities"):
        key = deal.get("stage")
        if key not in STAGES:
            continue
        stage = STAGES[key]
        _, wave, held = store.deal(deal["id"])
        rows = store.db.execute(
            "select id, role, card, blocking, stage, wave from task where deal_id=? and done=0", (deal["id"],)
        ).fetchall()
        now_items, side_items, overdue = [], [], False
        for task_id, role, card, blocking, st, wv in rows:
            try:
                task = crm.get("tasks", task_id)
            except RuntimeError:
                continue
            if not task or task.get("status") == "DONE":
                continue
            due = _parse(task.get("dueAt"))
            late = bool(due and due < now)
            item = {**_person(role), "card": card, "title": clean_title(task.get("title")),
                    "due": due.date().isoformat() if due else None, "late": late,
                    "stage_label": STAGES[st]["label"] if st in STAGES else st}
            if blocking and st == key and wv == wave:
                now_items.append(item)
                overdue = overdue or late
            else:
                item["leftover"] = bool(blocking and st != key)
                side_items.append(item)
        nxt = [
            {**_person(s["role"]), "card": s["card"]}
            for w in stage.get("waves", [])[wave + 1:] for s in w if engine.needed(deal, s)
        ]
        if key == FINAL:
            status = "closed"
        elif key in SERVICE:
            status = "service"
        elif held:
            status = "stop"
        elif overdue:
            status = "late"
        else:
            status = "ok"
        since = _parse(store.stage_since(deal["id"]))
        amount = deal.get("amount") or {}
        deals_out.append({
            "id": deal["id"], "name": deal.get("name"), "stage": key, "stage_label": stage["label"],
            "phase": PHASE_OF.get(key), "status": status, "held": held,
            "days_on_stage": (now - since).days if since else None,
            "since": since.isoformat() if since else None,
            "amount": round(amount["amountMicros"] / 1_000_000) if amount.get("amountMicros") else None,
            "currency": amount.get("currencyCode"),
            "factory": deal.get("factory"), "doors": deal.get("doorsCount"), "address": deal.get("objectAddress"),
            "flags": [PROCESS["flags"][f] for f in PROCESS["flags"] if deal.get(f)],
            "now": now_items, "side": side_items, "next": nxt,
            "next_stage": STAGES[stage["next"]]["label"] if stage.get("next") else None,
            "events": [{"at": a, "kind": k, "text": t} for a, k, t in store.events(deal["id"])],
            "crm_url": f"http://localhost:3000/object/opportunity/{deal['id']}",
        })
    active = [d for d in deals_out if d["status"] not in ("closed", "service")]
    in_transit = [d for d in active if d["phase"] is not None and PHASES[d["phase"]]["name"] == "В пути"]
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "phases": [p["name"] for p in PHASES],
        "kpi": {
            "active": len(active),
            "stop": sum(d["status"] == "stop" for d in active),
            "late": sum(d["status"] == "late" for d in active),
            "ok": sum(d["status"] == "ok" for d in active),
            "doors_in_transit": sum(int(d["doors"]) for d in in_transit if str(d.get("doors") or "").isdigit()),
            "closed": sum(d["status"] == "closed" for d in deals_out),
        },
        "deals": deals_out,
    }
