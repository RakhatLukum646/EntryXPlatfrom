#!/usr/bin/env bash
# Entryx: установка одной командой на Linux / macOS (Docker + Python 3.10+).
#   ./install.sh
# Повторный запуск безопасен: готовые шаги пропускаются, пароли и ключи не перезаписываются.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"

step() { printf '\n\033[36m== %s\033[0m\n' "$1"; }
rand_hex() { python3 -c "import secrets,sys; print(secrets.token_hex(int(sys.argv[1])))" "$1"; }
rand_b64() { python3 -c "import secrets,base64,sys; print(base64.b64encode(secrets.token_bytes(int(sys.argv[1]))).decode())" "$1"; }
env_get() { [ -f "$1" ] && grep -E "^$2=" "$1" | head -1 | cut -d= -f2- | tr -d '\r' || true; }
env_set() {
  python3 - "$1" "$2" "$3" <<'PY'
import re, sys
from pathlib import Path
path, key, value = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text = path.read_text(encoding="utf-8") if path.exists() else ""
line = f"{key}={value}"
if re.search(rf"^{re.escape(key)}=", text, re.M):
    text = re.sub(rf"^{re.escape(key)}=.*$", lambda _: line, text, flags=re.M)
else:
    text = (text.rstrip("\n") + "\n" if text else "") + line + "\n"
path.write_text(text, encoding="utf-8")
PY
}
copy_if_missing() { [ -f "$2" ] || { cp "$1" "$2"; echo "  создан $2"; }; }
wait_http() {
  local url=$1 name=$2 minutes=${3:-10} end=$(( $(date +%s) + ${3:-10} * 60 ))
  until curl -fsS -o /dev/null --max-time 5 "$url"; do
    [ "$(date +%s)" -lt "$end" ] || { echo "$name не ответил за $minutes мин ($url). Журнал: docker compose logs"; exit 1; }
    echo "  жду $name…"; sleep 10
  done
}

step "Проверяю Docker и Python"
docker info >/dev/null 2>&1 || { echo "Docker не запущен."; exit 1; }
docker compose version >/dev/null
python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" || { echo "Нужен Python 3.10 или новее."; exit 1; }

step "Готовлю пароли и настройки (.env)"
mkdir -p .secrets
ACC=.secrets/local-accounts.env
copy_if_missing local-accounts.env.example "$ACC"
[ -n "$(env_get "$ACC" DIFY_PASSWORD)" ] || env_set "$ACC" DIFY_PASSWORD "Entryx$(rand_hex 8)7"
copy_if_missing dify/.env.example dify/.env
[ -n "$(env_get dify/.env DIFY_AGENT_LOCAL_SANDBOX_AUTH_TOKEN)" ] || env_set dify/.env DIFY_AGENT_LOCAL_SANDBOX_AUTH_TOKEN "$(rand_hex 32)"
env_set dify/.env SSRF_PROXY_ALLOW_PRIVATE_DOMAINS host.docker.internal   # агенты читают сделки у моста на хосте
copy_if_missing crm/.env.example crm/.env
[ -n "$(env_get crm/.env PG_DATABASE_PASSWORD)" ] || env_set crm/.env PG_DATABASE_PASSWORD "$(rand_hex 24)"
[ -n "$(env_get crm/.env ENCRYPTION_KEY)" ] || env_set crm/.env ENCRYPTION_KEY "$(rand_b64 32)"
copy_if_missing bridge/.env.example bridge/.env

step "Ставлю Python-зависимости в .venv"
[ -d .venv ] || python3 -m venv .venv
VPY=.venv/bin/python; [ -x "$VPY" ] || VPY=.venv/Scripts/python.exe   # Git Bash на Windows
"$VPY" -m pip install --quiet --disable-pip-version-check -r bridge/requirements.txt

step "Запускаю Dify (первый раз скачивает образы, 5–15 минут)"
(cd dify && docker compose up -d)
step "Запускаю Twenty CRM"
(cd crm && docker compose up -d)

step "Настраиваю Dify: администратор, плагин Gemini, агенты"
"$VPY" tools/dify_setup.py

step "Twenty CRM"
wait_http http://localhost:3000/healthz Twenty
if [ -z "$(env_get bridge/.env TWENTY_API_KEY)" ]; then
  cat <<'TXT'
  Один раз вручную (Twenty не даёт сделать это скриптом):
    1. Откройте http://localhost:3000 — зарегистрируйтесь и создайте рабочее пространство.
    2. Settings → APIs & Webhooks → Create API key (срок — подольше).
    3. Скопируйте ключ и вставьте сюда.
TXT
  key=""
  while [ -z "$key" ]; do read -r -p "  API key Twenty: " key; done
  env_set bridge/.env TWENTY_API_KEY "$key"
fi
step "Настраиваю воронку и поля сделки в Twenty"
(cd bridge && "../$VPY" setup_crm.py)

step "Запускаю мост Entryx"
(cd bridge && docker compose up -d --build)
wait_http http://localhost:8787/health мост 3

cat <<'TXT'

Готово.
  Чат Entryx (роль выбирается в шапке):  http://localhost:8787/app
  Трекер проектов:                       http://localhost:8787/
  Dify:                                  http://localhost       (вход: .secrets/local-accounts.env)
  Twenty CRM:                            http://localhost:3000

Остался один шаг руками: ключ модели.
  Dify → аватар → Настройки → Поставщик моделей → Gemini → ключ Google AI Studio.
  Без ключа кнопки и задачи работают, а ответы на свободные вопросы — нет.
TXT
