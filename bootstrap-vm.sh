#!/bin/bash
# Одноразовая полная установка на VM в /opt/addcalendrbot (запуск на сервере с sudo).
# Перед запуском создайте .env (секреты не в git):
#   cd ~/addcalendrbot && cp .env.example .env && nano .env
# Затем:
#   sudo ./bootstrap-vm.sh

set -e

BOT_USER="addcalendrbot"
BOT_DIR="/opt/addcalendrbot"
SERVICE_FILE="addcalendrbot.service"
VENV_DIR="$BOT_DIR/venv"

echo "=== Развертывание AddCalendar Bot (bootstrap на VM) ==="

if [ "$EUID" -ne 0 ]; then
    echo "Запустите: sudo ./bootstrap-vm.sh"
    exit 1
fi

if [ ! -f .env ]; then
    echo "ОШИБКА: нет .env в $(pwd). Создайте: cp .env.example .env и заполните ключи"
    exit 1
fi

if ! id "$BOT_USER" &>/dev/null; then
    echo "Создание пользователя $BOT_USER..."
    useradd -r -s /bin/bash -d "$BOT_DIR" -m "$BOT_USER"
else
    echo "Пользователь $BOT_USER уже существует"
fi

echo "Создание директории $BOT_DIR..."
mkdir -p "$BOT_DIR"

echo "Копирование файлов..."
cp bot.py config.py requirements.txt "$BOT_DIR/"
cp .env "$BOT_DIR/.env"
chmod 600 "$BOT_DIR/.env"
if [ -f "users.db" ]; then
    cp users.db "$BOT_DIR/"
fi

echo "Установка прав доступа..."
chown -R "$BOT_USER:$BOT_USER" "$BOT_DIR"
chmod +x "$BOT_DIR/bot.py"
chmod 600 "$BOT_DIR/.env"

echo "Создание виртуального окружения Python..."
if command -v python3 &> /dev/null; then
    if ! python3 -m venv --help &> /dev/null; then
        echo "Установка python3-venv..."
        apt-get update
        apt-get install -y python3-venv
    fi

    sudo -u "$BOT_USER" python3 -m venv "$VENV_DIR"

    echo "Установка зависимостей Python в venv..."
    sudo -u "$BOT_USER" "$VENV_DIR/bin/pip" install --upgrade pip
    echo "Установка PyTorch CPU-only..."
    if ! sudo -u "$BOT_USER" "$VENV_DIR/bin/pip" install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu; then
        echo "ОШИБКА: не удалось установить PyTorch"
        exit 1
    fi
    echo "Установка зависимостей из requirements.txt..."
    if ! sudo -u "$BOT_USER" "$VENV_DIR/bin/pip" install -r "$BOT_DIR/requirements.txt"; then
        echo "ОШИБКА: requirements.txt"
        exit 1
    fi
    echo "Установка easyocr..."
    if ! sudo -u "$BOT_USER" "$VENV_DIR/bin/pip" install easyocr; then
        echo "ОШИБКА: easyocr"
        exit 1
    fi
else
    echo "ОШИБКА: python3 не найден"
    exit 1
fi

echo "Установка systemd service..."
cp "$SERVICE_FILE" /etc/systemd/system/
systemctl daemon-reload

echo "Включение автозапуска..."
systemctl enable addcalendrbot.service

echo "Запуск сервиса..."
systemctl restart addcalendrbot.service

sleep 2
if systemctl is-active --quiet addcalendrbot.service; then
    echo "✓ Бот успешно запущен!"
else
    echo "✗ Бот не запустился. Логи:"
    echo "  sudo journalctl -u addcalendrbot -n 50"
    exit 1
fi
