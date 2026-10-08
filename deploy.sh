#!/usr/bin/env bash
# Синхронизация проекта с Linux VM (GCP/Yandex Cloud/другой VPS)
# и обновление systemd-сервиса.
# Usage: ./deploy.sh
#
# Локальные переменные (как в DarionPass):
#   VM_USER, VM_HOST, VM_PATH, SSH_KEY
#
# На VM каталог VM_PATH — рабочая копия; update.sh копирует код в /opt/addcalendrbot
# и перезапускает сервис. Секреты в .env не синхронизируются (исключены из rsync);
# при необходимости: rsync .env отдельно или UPDATE_ENV=1 sudo ./update.sh на VM.

set -euo pipefail

VM_USER="${VM_USER:-gregorypogosyan}"
VM_HOST="${VM_HOST:-34.41.134.183}"
VM_PATH="${VM_PATH:-~/addcalendrbot/}"
SSH_KEY="${SSH_KEY:-}"

if [ -z "${SSH_KEY}" ]; then
    if [ -f "$HOME/.ssh/addcalendrbot_vm" ]; then
        SSH_KEY="$HOME/.ssh/addcalendrbot_vm"
    elif [ -f "$HOME/.ssh/darionpass_gcp" ]; then
        # Обратная совместимость со старым именем ключа.
        SSH_KEY="$HOME/.ssh/darionpass_gcp"
    else
        echo "ОШИБКА: SSH-ключ не найден."
        echo "Укажите SSH_KEY=/path/to/private_key или создайте ~/.ssh/addcalendrbot_vm"
        exit 1
    fi
fi

SSH_OPTS="-i ${SSH_KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> Syncing ${PROJECT_DIR}/ -> ${VM_USER}@${VM_HOST}:${VM_PATH}"
rsync -avz --delete \
    --exclude='.git' \
    --exclude='.github' \
    --exclude='.venv' \
    --exclude='venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.DS_Store' \
    --exclude='*.db' \
    --exclude='.env' \
    --exclude='agent-transcripts' \
    --exclude='terminals' \
    --exclude='deploy.sh' \
    --exclude='photo_*.jpg' \
    --exclude='photo_*.png' \
    -e "ssh ${SSH_OPTS}" \
    "${PROJECT_DIR}/" \
    "${VM_USER}@${VM_HOST}:${VM_PATH}"

echo "==> Running update on VM (keeps /opt/addcalendrbot/.env unless UPDATE_ENV=1)"
ssh ${SSH_OPTS} "${VM_USER}@${VM_HOST}" \
    "cd ${VM_PATH} && chmod +x update.sh 2>/dev/null || true && sudo ./update.sh"

echo "==> Service status"
ssh ${SSH_OPTS} "${VM_USER}@${VM_HOST}" "sudo systemctl --no-pager status addcalendrbot || true"

echo "==> Last log lines"
ssh ${SSH_OPTS} "${VM_USER}@${VM_HOST}" "sudo journalctl -u addcalendrbot -n 30 --no-pager"

echo "==> Done"
