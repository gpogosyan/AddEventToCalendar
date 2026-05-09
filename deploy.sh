#!/usr/bin/env bash
# Синхронизация проекта с GCP VM и обновление systemd-сервиса (аналог DarionPass).
# Usage: ./deploy.sh
#
# Локальные переменные (как в DarionPass):
#   VM_USER, VM_HOST, VM_PATH, SSH_KEY
#
# На VM каталог VM_PATH — рабочая копия; update.sh копирует код в /opt/addcalendrbot
# и перезапускает сервис. Файл config.py на сервере не перезаписывается (исключён из rsync).

set -euo pipefail

VM_USER="${VM_USER:-gregorypogosyan}"
VM_HOST="${VM_HOST:-34.41.134.183}"
VM_PATH="${VM_PATH:-~/addcalendrbot/}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/darionpass_gcp}"

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
    --exclude='config.py' \
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

echo "==> Running update on VM (keeps /opt/addcalendrbot/config.py)"
ssh ${SSH_OPTS} "${VM_USER}@${VM_HOST}" \
    "cd ${VM_PATH} && chmod +x update.sh 2>/dev/null || true && sudo ./update.sh"

echo "==> Service status"
ssh ${SSH_OPTS} "${VM_USER}@${VM_HOST}" "sudo systemctl --no-pager status addcalendrbot || true"

echo "==> Last log lines"
ssh ${SSH_OPTS} "${VM_USER}@${VM_HOST}" "sudo journalctl -u addcalendrbot -n 30 --no-pager"

echo "==> Done"
