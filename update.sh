#!/bin/bash

# Скрипт для обновления бота (без пересоздания пользователя)

set -e

BOT_USER="addcalendrbot"
BOT_DIR="/opt/addcalendrbot"
VENV_DIR="$BOT_DIR/venv"

echo "=== Обновление AddCalendar Bot ==="

# Проверка, что скрипт запущен от root
if [ "$EUID" -ne 0 ]; then 
    echo "Пожалуйста, запустите скрипт с правами root (sudo ./update.sh)"
    exit 1
fi

# Остановка сервиса
echo "Остановка бота..."
systemctl stop addcalendrbot.service

# Копирование обновленных файлов
echo "Копирование обновленных файлов..."
cp bot.py config.py requirements.txt "$BOT_DIR/"

# Установка прав доступа
chown -R "$BOT_USER:$BOT_USER" "$BOT_DIR"

# Обновление зависимостей в venv
echo "Обновление зависимостей Python..."
# Обновляем torch CPU-only версию
sudo -u "$BOT_USER" "$VENV_DIR/bin/pip" install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu --upgrade
# Обновляем остальные пакеты
sudo -u "$BOT_USER" "$VENV_DIR/bin/pip" install -r "$BOT_DIR/requirements.txt" --upgrade
# Обновляем easyocr
sudo -u "$BOT_USER" "$VENV_DIR/bin/pip" install easyocr --upgrade

# Перезагрузка systemd и перезапуск
echo "Перезапуск бота..."
systemctl daemon-reload
systemctl start addcalendrbot.service

# Проверка статуса
sleep 2
if systemctl is-active --quiet addcalendrbot.service; then
    echo "✓ Бот успешно обновлен и запущен!"
else
    echo "✗ ОШИБКА: Бот не запустился. Проверьте логи:"
    echo "  sudo journalctl -u addcalendrbot -n 50"
    exit 1
fi

