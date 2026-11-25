"""Готовит локальный Dify для Entryx: ждёт запуска, создаёт администратора (первый запуск),
ставит плагин Gemini, импортирует агентов dify-apps/*.yml, публикует их и прописывает ключи в bridge/.env.

Также ставит плагин Gemini из маркетплейса Dify (ключ модели вводит человек в интерфейсе Dify).
Повторный запуск: существующие приложения entryx-* пропускаются, у них берётся имеющийся ключ.

Запуск:  .venv\\Scripts\\python tools\\dify_setup.py
"""

from __future__ import annotations

import base64
import re
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
ACC = dotenv_values(ROOT / ".secrets" / "local-accounts.env")
BRIDGE_ENV = ROOT / "bridge" / ".env"


class Console:
    def __init__(self, base: str, email: str, password: str) -> None:
        self.http = httpx.Client(base_url=base.rstrip("/") + "/console/api", timeout=120)
        r = self.http.post("/login", json={
            "email": email, "password": base64.b64encode(password.encode()).decode(), "remember_me": True,
        })
        r.raise_for_status()
        csrf = next((v for k, v in self.http.cookies.items() if k.endswith("csrf_token")), None)
        if csrf:
            self.http.headers["X-CSRF-Token"] = csrf

    def req(self, method: str, path: str, **kw):
        r = self.http.request(method, path, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:400]}")
        return r.json() if r.content else None


def wait_ready(base: str, minutes: int = 10) -> str:
    """Ждём, пока Dify ответит. Первый запуск (скачивание образов, миграции) занимает несколько минут."""
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base.rstrip('/')}/console/api/setup", timeout=5)
            if r.status_code == 200:
                return r.json().get("step", "finished")
        except httpx.HTTPError:
            pass
        print("  жду Dify…", flush=True)
        time.sleep(10)
    raise SystemExit(f"Dify не ответил за {minutes} мин: проверьте `docker compose logs` в папке dify")


def ensure_admin(base: str, email: str, password: str, name: str) -> None:
    """Первый запуск Dify: создаём администратора из .secrets/local-accounts.env (вместо страницы /install)."""
    if wait_ready(base) == "finished":
        return
    r = httpx.post(f"{base.rstrip('/')}/console/api/setup", timeout=60,
                   json={"email": email, "name": name, "password": password, "language": "ru-RU"})
    if r.status_code >= 400:
        raise SystemExit(f"Dify: не удалось создать администратора: {r.status_code} {r.text[:300]}")
    print(f"Dify: создан администратор {email}")


MODEL_PLUGIN = "langgenius/gemini"  # агенты работают на Gemini; ключ Google AI Studio вводит человек в Dify


def install_model_plugin(c: Console) -> None:
    installed = c.req("GET", "/workspaces/current/plugin/list", params={"page": 1, "page_size": 100})
    names = {p.get("plugin_id") for p in (installed or {}).get("plugins", [])}
    if MODEL_PLUGIN in names:
        print(f"plugin {MODEL_PLUGIN}: already installed")
        return
    meta = httpx.get(f"https://marketplace.dify.ai/api/v1/plugins/{MODEL_PLUGIN}", timeout=30).json()
    ident = meta["data"]["plugin"]["latest_package_identifier"]
    c.req("POST", "/workspaces/current/plugin/install/marketplace", json={"plugin_unique_identifiers": [ident]})
    print(f"plugin {MODEL_PLUGIN}: install started", ident.split("@")[0])


def ensure_app(c: Console, path: Path, existing: dict[str, str], overwrite: bool) -> tuple[str, str]:
    name = path.stem
    if name in existing and not overwrite:
        app_id = existing[name]
        print(f"{name}: exists")
    else:
        body = {"mode": "yaml-content", "yaml_content": path.read_text(encoding="utf-8")}
        if name in existing:
            body["app_id"] = existing[name]
        res = c.req("POST", "/apps/imports", json=body)
        if res.get("status") == "pending":
            res = c.req("POST", f"/apps/imports/{res['id']}/confirm")
        app_id = res["app_id"]
        print(f"{name}: imported ({res.get('status')})")
    c.req("POST", f"/apps/{app_id}/workflows/publish", json={"marked_name": "", "marked_comment": ""})
    keys = c.req("GET", f"/apps/{app_id}/api-keys").get("data", [])
    token = keys[0]["token"] if keys else c.req("POST", f"/apps/{app_id}/api-keys")["token"]
    return app_id, token


def write_bridge_keys(tokens: dict[str, str]) -> None:
    text = BRIDGE_ENV.read_text(encoding="utf-8")
    for agent, token in tokens.items():
        var = f"DIFY_KEY_{agent.upper()}"
        text = re.sub(rf"^{var}=.*$", f"{var}={token}", text, flags=re.M) if re.search(rf"^{var}=", text, re.M) else text + f"\n{var}={token}"
    BRIDGE_ENV.write_text(text, encoding="utf-8")


def main() -> None:
    import sys

    overwrite = "--overwrite" in sys.argv  # перезаписать уже импортированных агентов из dify-apps/
    ensure_admin(ACC["DIFY_URL"], ACC["DIFY_EMAIL"], ACC["DIFY_PASSWORD"], ACC.get("DIFY_NAME") or "Entryx Admin")
    c = Console(ACC["DIFY_URL"], ACC["DIFY_EMAIL"], ACC["DIFY_PASSWORD"])
    try:
        install_model_plugin(c)
    except Exception as exc:
        print("plugin install failed:", exc)
    apps = c.req("GET", "/apps", params={"page": 1, "limit": 100})["data"]
    # Перезапись в Dify сохраняет тип приложения. Агенты теперь чаты (advanced-chat):
    # старые workflow-версии не удаляем, а переименовываем в архив.
    for a in apps:
        if a["name"].startswith("entryx-") and a["mode"] != "advanced-chat":
            c.req("PUT", f"/apps/{a['id']}", json={
                "name": f"архив · {a['name']} (workflow)", "description": "Старая workflow-версия агента, заменена чатом.",
                "icon_type": a.get("icon_type") or "emoji", "icon": a.get("icon") or "🚪", "icon_background": a.get("icon_background") or "#EEEEEE",
            })
            print(f"{a['name']}: renamed to archive")
    existing = {a["name"]: a["id"] for a in apps if a["mode"] == "advanced-chat"}
    tokens = {}
    for path in sorted((ROOT / "dify-apps").glob("entryx-*.yml")):
        _, token = ensure_app(c, path, existing, overwrite)
        tokens[path.stem.removeprefix("entryx-")] = token
    write_bridge_keys(tokens)
    print(f"keys written to bridge/.env for {len(tokens)} agents")


if __name__ == "__main__":
    main()
