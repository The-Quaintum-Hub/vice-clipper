#!/usr/bin/env bash
# Instala/actualiza los servicios de usuario (systemd). Idempotente.
# Uso: bash ~/vice/systemd/install.sh
set -euo pipefail
dst=~/.config/systemd/user
mkdir -p "$dst" ~/.local/bin
printf '#!/bin/sh\ncd ~/vice && exec ~/vice/.venv/bin/python -m vice "$@"\n' > ~/.local/bin/vice
chmod +x ~/.local/bin/vice
cp ~/vice/systemd/*.service ~/vice/systemd/*.timer "$dst/"
loginctl show-user "$USER" -p Linger | grep -q yes || sudo loginctl enable-linger "$USER"  # corre sin sesión abierta
systemctl --user daemon-reload
systemctl --user enable --now vice-work.service vice-live.service vice-watch.timer vice-views.timer vice-report.timer vice-viral.timer
# Ghost (opcional): un Chrome por marca en el escritorio (sesiones a mano) + el que hace las tareas. Solo con VICE_GHOST=1
if grep -q '^VICE_GHOST=1' ~/vice/.env 2>/dev/null; then
  ~/vice/.venv/bin/python -c "import playwright" 2>/dev/null || ~/vice/.venv/bin/pip install -q playwright
  marcas=$(grep '^VICE_CUENTAS=' ~/vice/.env | cut -d= -f2- | tr -d ' \r' | tr ',' '\n' | cut -d= -f1)   # un Chrome por cuenta
  systemctl --user enable --now $(printf 'vice-chrome@%s.service ' $marcas) vice-ghost.service
else
  echo "Ghost NO activado (VICE_GHOST=1 en ~/vice/.env lo enciende): publicas a mano lo que apruebas"
fi
if grep -q '^TELEGRAM_BOT_TOKEN=.' ~/vice/.env 2>/dev/null; then
  systemctl --user enable --now vice-bot.service
else
  echo "vice-bot NO activado: falta TELEGRAM_BOT_TOKEN en ~/vice/.env (fase 3)"
fi
systemctl --user list-units 'vice-*' --all --no-pager
systemctl --user list-timers 'vice-*' --no-pager
