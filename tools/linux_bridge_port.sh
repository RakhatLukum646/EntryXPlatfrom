#!/usr/bin/env bash
# Linux-сервер: открывает порт моста и на адресе Docker-сети, чтобы контейнеры Dify до него доходили.
# Без этого кнопки в чате отвечают «Мост Entryx недоступен»: порт 127.0.0.1:8787 из контейнеров не виден.
# В интернет порт не открывается. Windows, macOS и Docker Desktop: ничего не делает.
#   ./tools/linux_bridge_port.sh && (cd bridge && docker compose up -d)
set -euo pipefail
cd "$(dirname "$0")/.."

[ "$(uname -s)" = "Linux" ] || exit 0
case "$(docker info -f '{{.OperatingSystem}}')" in *"Docker Desktop"*) exit 0 ;; esac

FILE=bridge/docker-compose.override.yml
MARK="# создано tools/linux_bridge_port.sh"
if [ -f "$FILE" ] && ! grep -qF "$MARK" "$FILE"; then
  echo "  $FILE уже есть и написан не этим скриптом: не трогаю. Добавьте порт моста на адресе Docker-сети сами."
  exit 0
fi
GW="$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}')"
[ -n "$GW" ] || { echo "Не нашёл адрес Docker-сети bridge."; exit 1; }
cat > "$FILE" <<YML
$MARK
services:
  bridge:
    ports:
      - "$GW:8787:8787"
YML
echo "  мост будет доступен контейнерам по $GW:8787"
