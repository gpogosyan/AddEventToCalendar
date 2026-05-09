# AddCalendar Telegram Bot

## Возможности
- Регистрация email пользователей
- Приём сообщений о событиях (текст и фото с OCR)
- Извлечение параметров события с помощью LLM
- Рассылка приглашений на email всем зарегистрированным в чате

## Локальный запуск (для разработки)
1. Установите зависимости:
   ```
   pip install -r requirements.txt
   ```
2. Заполните `config.py` своими данными:
   - TELEGRAM_TOKEN — токен Telegram-бота
   - OPENAI_API_KEY — ключ OpenAI
   - EMAIL_LOGIN, EMAIL_PASSWORD — email и пароль для отправки писем
3. Запустите бота:
   ```
   python bot.py
   ```

## Развертывание на Google Cloud VM (production)

### Подготовка виртуальной машины
1. Создайте виртуальную машину в Google Cloud (например, Ubuntu 22.04 LTS)
2. Подключитесь к VM по SSH
3. Установите Python 3 и pip (если не установлены):
   ```bash
   sudo apt update
   sudo apt install -y python3 python3-pip
   ```

### Развертывание бота
1. Загрузите файлы проекта на VM (через `scp` или `gcloud compute scp`):
   ```bash
   # С вашего локального компьютера:
   gcloud compute scp --recurse . VM_NAME:/tmp/addcalendrbot
   # Или используйте scp:
   scp -r . user@VM_IP:/tmp/addcalendrbot
   ```

2. На VM перейдите в директорию с проектом:
   ```bash
   cd /tmp/addcalendrbot
   ```

3. Запустите скрипт развертывания:
   ```bash
   sudo chmod +x deploy.sh
   sudo ./deploy.sh
   ```

Скрипт автоматически:
- Создаст пользователя `addcalendrbot`
- Установит бота в `/opt/addcalendrbot`
- Установит все зависимости
- Настроит systemd service для автозапуска
- Запустит бота

### Управление ботом

```bash
# Проверить статус
sudo systemctl status addcalendrbot

# Перезапустить бота
sudo systemctl restart addcalendrbot

# Остановить бота
sudo systemctl stop addcalendrbot

# Запустить бота
sudo systemctl start addcalendrbot

# Просмотр логов в реальном времени
sudo journalctl -u addcalendrbot -f

# Просмотр последних 100 строк логов
sudo journalctl -u addcalendrbot -n 100
```

### Обновление бота

1. Загрузите обновленные файлы на VM
2. Запустите скрипт обновления:
   ```bash
   cd /tmp/addcalendrbot
   sudo chmod +x update.sh
   sudo ./update.sh
   ```

Бот будет работать постоянно, даже после закрытия терминала или перезагрузки сервера.

## TODO
- Обработка аудио, документов
- Улучшение валидации email
- Хранение пользователей в отдельной БД для каждого чата 