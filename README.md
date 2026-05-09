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
2. Скопируйте пример и заполните `config.py` своими данными:
   ```
   cp config.example.py config.py
   ```
   Поля в `config.py`:
   - TELEGRAM_TOKEN — токен Telegram-бота
   - OPENAI_API_KEY — ключ OpenAI
   - EMAIL_LOGIN, EMAIL_PASSWORD — email и пароль для отправки писем
3. Запустите бота:
   ```
   python bot.py
   ```

## Развертывание на Google Cloud VM (production)

### Быстрый деплой с ноутбука (та же VM и SSH-ключ, что у DarionPass)

На машине должен быть ключ `~/.ssh/darionpass_gcp` (или задайте `SSH_KEY`).  
Параметры по умолчанию: `gregorypogosyan@34.41.134.183`, каталог на VM `~/addcalendrbot/`.

**Первый раз на новой VM** (пока нет `/opt/addcalendrbot/venv`):

1. Синхронизируйте код: `chmod +x deploy.sh && ./deploy.sh` — rsync пройдёт, `update.sh` напомнит про bootstrap.
2. По SSH на VM: `cd ~/addcalendrbot`, создайте `config.py` (реальные токены), например `cp config.example.py config.py` и отредактируйте.
3. Выполните: `sudo ./bootstrap-vm.sh` — создаст пользователя сервиса, venv, systemd и запустит бота.

Дальше достаточно `./deploy.sh` с ноутбука.

```bash
chmod +x deploy.sh
./deploy.sh
```

Скрипт синхронизирует проект в `~/addcalendrbot/` на VM и выполняет `sudo ./update.sh`.  
**`config.py` и файлы `*.db` в rsync не входят** — продакшен-секреты в `/opt/addcalendrbot/` не затираются.

Чтобы один раз обновить `config.py` с диска на VM, на сервере в каталоге с копией проекта:

```bash
sudo UPDATE_CONFIG=1 ./update.sh
```

### Автодеплой из GitHub Actions

В репозитории на GitHub добавьте secrets (те же имена, что у DarionPass: `VM_HOST`, `VM_USER`, `VM_SSH_PRIVATE_KEY`) и **`VM_DEPLOY_PATH`** = `/home/gregorypogosyan/addcalendrbot/` (путь к каталогу синхронизации на VM).  
При пуше в `main` сработает workflow `.github/workflows/deploy.yml`.

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