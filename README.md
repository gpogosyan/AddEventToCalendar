# AddCalendar Telegram Bot

## Возможности
- Регистрация email пользователей
- Приём сообщений о событиях (текст и фото с OCR)
- Извлечение параметров события с помощью LLM
- Рассылка приглашений на email всем зарегистрированным в чате

## Секреты (API-ключи) и Git

**Не храните ключи в репозитории** — даже в приватном: история Git и форки легко раскрывают секреты. В GitHub нет «скрытых полей» для кода; для CI используются отдельные [encrypted secrets](https://docs.github.com/en/actions/security-guides/using-secrets-in-github-actions) (переменные для workflow, не для коммитов в файлы).

В проекте:

- **`config.py`** в git — только загрузка переменных из окружения и файла **`.env`** (без секретов в коде).
- **`.env`** — ваши настоящие ключи, файл **в `.gitignore`**, создаётся один раз на машине.
- **`.env.example`** — шаблон без значений, можно коммитить.

Локально:

```bash
cp .env.example .env
# отредактируйте .env
```

## Локальный запуск (для разработки)
1. Установите зависимости: `pip install -r requirements.txt`
2. Создайте `.env` (см. выше). Переменные: `TELEGRAM_TOKEN`, `OPENAI_API_KEY`, `EMAIL_LOGIN`, `EMAIL_PASSWORD`; при необходимости `SMTP_SERVER`, `SMTP_PORT`.
3. Запуск: `python bot.py`

## Развертывание на Google Cloud VM (production)

### Быстрый деплой с ноутбука (та же VM и SSH-ключ, что у DarionPass)

На машине должен быть ключ `~/.ssh/darionpass_gcp` (или задайте `SSH_KEY`).  
Параметры по умолчанию: `gregorypogosyan@34.41.134.183`, каталог на VM `~/addcalendrbot/`.

**Первый раз на новой VM** (пока нет `/opt/addcalendrbot/venv`):

1. Синхронизируйте код: `chmod +x deploy.sh && ./deploy.sh` — rsync пройдёт, `update.sh` напомнит про bootstrap.
2. По SSH: `cd ~/addcalendrbot`, создайте **`cp .env.example .env`** и заполните секреты.
3. `sudo ./bootstrap-vm.sh` — пользователь сервиса, venv, копирование `.env` в `/opt/addcalendrbot/.env`, systemd, запуск.

Дальше с ноутбука: **`./deploy.sh`** (код обновится, **`/opt/.../.env` не трогается**).

Чтобы **обновить секреты** с ноутбука: скопируйте `.env` в `~/addcalendrbot/`, затем на VM:

```bash
cd ~/addcalendrbot && sudo UPDATE_ENV=1 ./update.sh
```

Или вручную: `sudo nano /opt/addcalendrbot/.env` и `sudo systemctl restart addcalendrbot`.

```bash
chmod +x deploy.sh
./deploy.sh
```

Rsync **не включает** `.env` и `*.db`, чтобы не затереть продакшен.

### Автодеплой из GitHub Actions (без ручной заливки `.env` на VM)

В **Settings → Secrets and variables → Actions** задайте:

**SSH и путь (как у DarionPass):**

| Secret | Пример |
|--------|--------|
| `VM_HOST` | `34.41.134.183` |
| `VM_USER` | `gregorypogosyan` |
| `VM_DEPLOY_PATH` | `/home/gregorypogosyan/addcalendrbot/` |
| `VM_SSH_PRIVATE_KEY` | содержимое приватного ключа (`-----BEGIN …`) |

**Приложение** (workflow сам собирает `/opt/addcalendrbot/.env` на сервере):

| Secret | Обязательно |
|--------|-------------|
| `TELEGRAM_TOKEN` | да |
| `OPENAI_API_KEY` | да |
| `EMAIL_LOGIN` | да |
| `EMAIL_PASSWORD` | да (для Gmail — [пароль приложения](https://myaccount.google.com/apppasswords)) |
| `SMTP_SERVER` | нет (по умолчанию `smtp.gmail.com`) |
| `SMTP_PORT` | нет (по умолчанию `465`) |

При каждом пуше в `main` (или запуске workflow вручную) выполняются: rsync кода → запись `.env` из Secrets → `sudo ./update.sh` → перезапуск сервиса.

Первая установка на **новой** VM по-прежнему требует одного раза **`sudo ./bootstrap-vm.sh`** (venv и systemd). После этого деплой полностью из GitHub.

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