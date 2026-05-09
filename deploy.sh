#!/bin/bash

# Скрипт для развертывания бота на виртуальной машине Google Cloud

set -e

BOT_USER="addcalendrbot"
BOT_DIR="/opt/addcalendrbot"
SERVICE_FILE="addcalendrbot.service"
VENV_DIR="$BOT_DIR/venv"

echo "=== Развертывание AddCalendar Bot ==="

# Проверка, что скрипт запущен от root
if [ "$EUID" -ne 0 ]; then 
    echo "Пожалуйста, запустите скрипт с правами root (sudo ./deploy.sh)"
    exit 1
fi

# Создание пользователя для бота (если не существует)
if ! id "$BOT_USER" &>/dev/null; then
    echo "Создание пользователя $BOT_USER..."
    useradd -r -s /bin/bash -d "$BOT_DIR" -m "$BOT_USER"
else
    echo "Пользователь $BOT_USER уже существует"
fi

# Создание директории для бота
echo "Создание директории $BOT_DIR..."
mkdir -p "$BOT_DIR"

# Копирование файлов бота
echo "Копирование файлов..."
cp bot.py config.py requirements.txt "$BOT_DIR/"
if [ -f "users.db" ]; then
    cp users.db "$BOT_DIR/"
fi

# Установка прав доступа
echo "Установка прав доступа..."
chown -R "$BOT_USER:$BOT_USER" "$BOT_DIR"
chmod +x "$BOT_DIR/bot.py"

# Установка зависимостей Python через venv
echo "Создание виртуального окружения Python..."
if command -v python3 &> /dev/null; then
    # Установка python3-venv если не установлен
    if ! python3 -m venv --help &> /dev/null; then
        echo "Установка python3-venv..."
        apt-get update
        apt-get install -y python3-venv
    fi
    
    # Создание venv
    sudo -u "$BOT_USER" python3 -m venv "$VENV_DIR"
    
    # Установка зависимостей в venv
    echo "Установка зависимостей Python в виртуальное окружение..."
    sudo -u "$BOT_USER" "$VENV_DIR/bin/pip" install --upgrade pip
    # Сначала устанавливаем torch CPU-only (намного меньше по размеру)
    echo "Установка PyTorch CPU-only версии..."
    if ! sudo -u "$BOT_USER" "$VENV_DIR/bin/pip" install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu; then
        echo "ОШИБКА: Не удалось установить PyTorch"
        exit 1
    fi
    # Затем остальные пакеты
    echo "Установка остальных зависимостей..."
    if ! sudo -u "$BOT_USER" "$VENV_DIR/bin/pip" install -r "$BOT_DIR/requirements.txt"; then
        echo "ОШИБКА: Не удалось установить зависимости из requirements.txt"
        exit 1
    fi
    # Устанавливаем easyocr после torch (чтобы использовал CPU версию)
    echo "Установка easyocr..."
    if ! sudo -u "$BOT_USER" "$VENV_DIR/bin/pip" install easyocr; then
        echo "ОШИБКА: Не удалось установить easyocr"
        exit 1
    fi
else
    echo "ОШИБКА: python3 не найден. Установите Python 3."
    exit 1
fi

# Копирование и установка systemd service
echo "Установка systemd service..."
cp "$SERVICE_FILE" /etc/systemd/system/
systemctl daemon-reload

# Включение автозапуска
echo "Включение автозапуска..."
systemctl enable addcalendrbot.service

# Запуск сервиса
echo "Запуск сервиса..."
systemctl restart addcalendrbot.service

# Проверка статуса
sleep 2
if systemctl is-active --quiet addcalendrbot.service; then
    echo "✓ Бот успешно запущен!"
    echo ""
    echo "Полезные команды:"
    echo "  sudo systemctl status addcalendrbot  - проверить статус"
    echo "  sudo systemctl restart addcalendrbot - перезапустить бота"
    echo "  sudo systemctl stop addcalendrbot    - остановить бота"
    echo "  sudo systemctl start addcalendrbot  - запустить бота"
    echo "  sudo journalctl -u addcalendrbot -f  - просмотр логов в реальном времени"
else
    echo "✗ ОШИБКА: Бот не запустился. Проверьте логи:"
    echo "  sudo journalctl -u addcalendrbot -n 50"
    exit 1
fi

