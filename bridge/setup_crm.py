"""Настраивает Twenty под процесс ENTRYX: этапы воронки, флаги и поля сделки.

Повторный запуск безопасен: существующие поля пропускаются, этапы переписываются.
Запуск из папки bridge:  ..\\.venv\\Scripts\\python setup_crm.py
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

from engine import PROCESS  # noqa: E402
from twenty import Twenty  # noqa: E402

COLORS = ["gray", "blue", "sky", "turquoise", "green", "yellow", "orange", "red", "pink", "purple"]


def sync_kanban_columns(crm: Twenty, stage_field_id: str, values: list[str]) -> None:
    """Колонки канбана «по этапам» — отдельные viewGroups; после смены этапов их надо пересоздать."""
    views = crm._unwrap(crm._req("GET", "/rest/metadata/views"))
    groups = crm._unwrap(crm._req("GET", "/rest/metadata/viewGroups"))
    for view in views:
        if view.get("type") != "KANBAN" or view.get("mainGroupByFieldMetadataId") != stage_field_id:
            continue
        have = {g["fieldValue"] for g in groups if g["viewId"] == view["id"]}
        for pos, value in enumerate(values):
            if value not in have:
                crm._req("POST", "/rest/metadata/viewGroups",
                         json={"viewId": view["id"], "fieldValue": value, "isVisible": True, "position": pos})
        print(f"kanban '{view.get('name')}': columns synced")


def main() -> None:
    crm = Twenty(os.getenv("TWENTY_URL", "http://localhost:3000"), os.environ["TWENTY_API_KEY"])
    opp = next(o for o in crm.objects() if o["nameSingular"] == "opportunity")
    fields = {f["name"]: f for f in opp["fields"]}

    stage_field = fields["stage"]
    # Варианты с тем же value сохраняют свой id: иначе Twenty считает их новыми и сбрасывает этап у сделок.
    existing = {o["value"]: o for o in stage_field.get("options") or []}
    options = []
    for i, stage in enumerate(PROCESS["stages"]):
        color = "gray" if stage.get("service") else COLORS[(i % (len(COLORS) - 1)) + 1]
        opt = {"value": stage["key"], "label": stage["label"], "position": i, "color": color}
        if stage["key"] in existing:
            opt["id"] = existing[stage["key"]]["id"]
        options.append(opt)
    unchanged = [(o["value"], o["label"], o["position"]) for o in options] == [
        (o["value"], o["label"], o["position"]) for o in sorted(existing.values(), key=lambda o: o["position"])
    ]
    if unchanged:
        print("stages: unchanged")
    else:
        crm.update_field(stage_field["id"], {"options": options, "defaultValue": "'DOGOVOR'"})
        print("stages:", ", ".join(o["label"] for o in options))

    sync_kanban_columns(crm, stage_field["id"], [o["value"] for o in options])

    wanted = [(n, "BOOLEAN", lbl) for n, lbl in PROCESS["flags"].items()]
    wanted += [(n, "TEXT", lbl) for n, lbl in PROCESS["fields"].items()]
    wanted += [(n, "TEXT", lbl) for n, lbl in PROCESS.get("bot_fields", {}).items()]
    for name, ftype, label in wanted:
        if name in fields:
            print("exists:", name)
            continue
        body = {"objectMetadataId": opp["id"], "type": ftype, "name": name, "label": label}
        if ftype == "BOOLEAN":
            body["defaultValue"] = False
        fields[name] = crm.create_field(body)
        print("created:", name, ftype)

    show_on_kanban(crm, stage_field["id"], [fields[n]["id"] for n in PROCESS.get("bot_fields", {})])


def show_on_kanban(crm: Twenty, stage_field_id: str, field_ids: list[str]) -> None:
    """Поля бота («Сейчас на ком», «Дальше», «Статус процесса») — на карточке канбана по этапам, сразу под названием."""
    views = crm._unwrap(crm._req("GET", "/rest/metadata/views"))
    view_fields = crm._unwrap(crm._req("GET", "/rest/metadata/viewFields"))
    for view in views:
        if view.get("type") != "KANBAN" or view.get("mainGroupByFieldMetadataId") != stage_field_id:
            continue
        have = {vf["fieldMetadataId"]: vf for vf in view_fields if vf["viewId"] == view["id"]}
        for pos, fid in enumerate(field_ids, start=1):
            if fid in have:
                if not have[fid].get("isVisible"):
                    crm._req("PATCH", f"/rest/metadata/viewFields/{have[fid]['id']}", json={"isVisible": True, "position": pos})
                continue
            crm._req("POST", "/rest/metadata/viewFields",
                     json={"viewId": view["id"], "fieldMetadataId": fid, "isVisible": True, "position": pos, "size": 200})
        print(f"kanban '{view.get('name')}': bot fields on cards")


if __name__ == "__main__":
    main()
