import logging
import sqlite3
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from telegram import Update, ForceReply, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
import openai
import config
import datetime
import uuid
import re
import threading
import warnings
warnings.filterwarnings("ignore", message="'pin_memory' argument is set as true but not supported on MPS now")
warnings.filterwarnings("ignore", message="Using CPU. Note: This module is much faster with a GPU.")

_easyocr_reader = None
_easyocr_lock = threading.Lock()

def _get_easyocr_reader():
    """EasyOCR и модели подгружаются только при первой попытке OCR по фото."""
    global _easyocr_reader
    if _easyocr_reader is not None:
        return _easyocr_reader
    with _easyocr_lock:
        if _easyocr_reader is None:
            import easyocr
            _easyocr_reader = easyocr.Reader(['ru', 'en'], gpu=False)
    return _easyocr_reader

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Подключение к БД пользователей
conn = sqlite3.connect('users.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS users (chat_id INTEGER, email TEXT)''')
conn.commit()

# Состояния для ConversationHandler
EMAIL, CONFIRM_EVENT, CHOOSE_FIELD, EDIT_NAME, EDIT_DATE, EDIT_TIME, EDIT_LOCATION, EDIT_COMMENT = range(8)

# Установка ключа OpenAI
client = openai.OpenAI(api_key=config.OPENAI_API_KEY)

def add_user(chat_id, email):
    c.execute('INSERT INTO users (chat_id, email) VALUES (?, ?)', (chat_id, email))
    conn.commit()

def get_emails_in_chat(chat_id):
    c.execute('SELECT email FROM users WHERE chat_id=?', (chat_id,))
    return [row[0] for row in c.fetchall()]

def clear_emails_in_chat(chat_id):
    c.execute('DELETE FROM users WHERE chat_id=?', (chat_id,))
    conn.commit()

def remove_email_in_chat(chat_id, email):
    c.execute('DELETE FROM users WHERE chat_id=? AND email=?', (chat_id, email))
    conn.commit()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    context.user_data['in_conversation'] = True
    emails = get_emails_in_chat(chat_id)
    if emails:
        await update.message.reply_text(
            f'В чате уже зарегистрированы email: {", ".join(emails)}.\nМожешь сразу отправить информацию о событии.'
        )
        context.user_data.clear()
        return ConversationHandler.END
    else:
        await update.message.reply_text('Привет! Пожалуйста, пришли свой email для регистрации.')
        return EMAIL

async def register_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    email = update.message.text.strip()
    chat_id = update.effective_chat.id
    # Простая валидация email
    if email.lower() in ['Да', 'Нет', 'name', 'date', 'time', 'location', 'comment']:
        await update.message.reply_text('Пожалуйста, введите корректный email.')
        return EMAIL
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        await update.message.reply_text('Похоже, это не email. Пожалуйста, введите корректный email.')
        return EMAIL
    add_user(chat_id, email)
    await update.message.reply_text(f'Email {email} сохранён! Теперь отправьте информацию о событии.')
    context.user_data.clear()
    return ConversationHandler.END

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Не реагируем, если пользователь в процессе диалога подтверждения/редактирования
    if context.user_data.get('in_conversation'):
        return
    # Если не в процессе ConversationHandler, очищаем event (чтобы не было ложных блокировок)
    if not context.user_data.get('in_conversation'):
        context.user_data.pop('event', None)
    if not context.user_data.get('in_conversation') and context.user_data.get('event'):
        await update.message.reply_text(
            'Вы уже редактируете событие. Пожалуйста, завершите подтверждение или редактирование текущего события.'
        )
        return
    chat_id = update.effective_chat.id
    text = update.message.text
    emails = get_emails_in_chat(chat_id)
    if not emails:
        await update.message.reply_text('Сначала зарегистрируйте email командой /start.')
        return
    await update.message.reply_text('Получено текстовое сообщение. Отправляю в LLM для извлечения параметров события...')
    # Запрос к LLM
    prompt = f"""
    Извлеки из следующего сообщения параметры события:
    - Name (название события)
    - Date (дата)
    - Time (интервал времени, например, 13:30-15:00)
    - Location (место проведения)
    Сообщение: {text}
    Ответь в формате:
    Name: ...\nDate: ...\nTime: ...\nLocation: ...
    """
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}]
        )
        result = response.choices[0].message.content
    #    await update.message.reply_text(f'Параметры события успешно извлечены!\n{result}')
        event = parse_event_info(result)
        event['comment'] = ''
        context.user_data['event'] = event
        return await show_event_and_confirm(update, context)
    except Exception as e:
        await update.message.reply_text(f'Ошибка LLM: {e}')
        return ConversationHandler.END

async def confirm_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"confirm_event called with text: {update.message.text}")
    answer = update.message.text.strip().lower()
    event = context.user_data.get('event', {})
    if answer.strip().lower() in ['да', 'yes']:
        logger.info(f"context.user_data: {context.user_data}")
        await update.message.reply_text('Отправляю приглашения на email...', reply_markup=ReplyKeyboardRemove())
        emails = get_emails_in_chat(update.effective_chat.id)
        for email in emails:
            error = send_email(email, event)
            if error:
                await update.message.reply_text(f"Ошибка: {error}")
                context.user_data.clear()
                return ConversationHandler.END
        await update.message.reply_text(f'Приглашения отправлены: {", ".join(emails)}')
        context.user_data.clear()
        return ConversationHandler.END
    elif answer.strip().lower() in ['нет', 'no']:
        reply_markup = ReplyKeyboardMarkup([
            ['Name', 'Date'],
            ['Time', 'Location'],
            ['Comment']
        ], one_time_keyboard=True, resize_keyboard=True)
        await update.message.reply_text(
            'Какой параметр хотите исправить?',
            reply_markup=reply_markup
        )
        return CHOOSE_FIELD
    else:
        # Если пользователь ввел комментарий
        event['comment'] = update.message.text.strip()
        context.user_data['event'] = event
        return await show_event_and_confirm(update, context)

async def edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = update.message.text.strip().lower()
    field_map = {
        'name': ('name', EDIT_NAME),
        'date': ('date', EDIT_DATE),
        'time': ('time', EDIT_TIME),
        'location': ('location', EDIT_LOCATION),
        'comment': ('comment', EDIT_COMMENT),
    }
    if field in field_map:
        context.user_data['edit_field'] = field_map[field][0]
        prompts = {
            'name': 'Введите новое название события:',
            'date': 'Введите новую дату события (например, 10.06.2024):',
            'time': 'Введите новое время события (например, 13:30-15:00):',
            'location': 'Введите новое место проведения события:',
            'comment': 'Введите новый комментарий:',
        }
        await update.message.reply_text(prompts[field_map[field][0]], reply_markup=ReplyKeyboardRemove())
        return field_map[field][1]
    else:
        reply_markup = ReplyKeyboardMarkup([
            ['Name', 'Date'],
            ['Time', 'Location'],
            ['Comment']
        ], one_time_keyboard=True, resize_keyboard=True)
        await update.message.reply_text('Пожалуйста, выберите одно из полей: Name/Date/Time/Location/Comment', reply_markup=reply_markup)
        return CHOOSE_FIELD

async def set_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data.get('edit_field')
    if not field:
        return await show_event_and_confirm(update, context)
    context.user_data['event'][field] = update.message.text.strip()
    # После изменения любого поля снова показываем все параметры и спрашиваем подтверждение
    return await show_event_and_confirm(update, context)

async def show_event_and_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    event = context.user_data['event']
    reply_markup = ReplyKeyboardMarkup([
        ['Да', 'Нет']
    ], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        f"Проверьте параметры события:\n"
        f"Name: {event['name']}\n"
        f"Date: {event['date']}\n"
        f"Time: {event['time']}\n"
        f"Location: {event['location']}\n"
        f"Comments: {event['comment']}\n"
        "\nВсе верно?",
        reply_markup=reply_markup
    )
    return CONFIRM_EVENT

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    emails = get_emails_in_chat(chat_id)
    if not emails:
        await update.message.reply_text('Сначала зарегистрируйте email командой /start.')
        return
    # Если не в процессе ConversationHandler, очищаем event (чтобы не было ложных блокировок)
    if not context.user_data.get('in_conversation'):
        context.user_data.pop('event', None)
    if not context.user_data.get('in_conversation') and context.user_data.get('event'):
        await update.message.reply_text(
            'Вы уже редактируете событие. Пожалуйста, завершите подтверждение или редактирование текущего события.'
        )
        return
    await update.message.reply_text('Получено изображение. Сохраняю и извлекаю текст с помощью OCR...')
    photo_file = await update.message.photo[-1].get_file()
    photo_path = f"photo_{chat_id}.jpg"
    await photo_file.download_to_drive(photo_path)
    try:
        reader = _get_easyocr_reader()
        ocr_result = reader.readtext(photo_path, detail=0, paragraph=True)
        text = '\n'.join(ocr_result)
    #    await update.message.reply_text(f'Текст, извлечённый из изображения:\n{text}\n\nОтправляю в LLM для извлечения параметров события...')
    except Exception as e:
        await update.message.reply_text(f'Ошибка OCR: {e}')
        return
    # Запрос к LLM
    prompt = f"""
    Извлеки из следующего сообщения параметры события:
    - Name (название события)
    - Date (дата)
    - Time (интервал времени, например, 13:30-15:00)
    - Location (место проведения)
    Сообщение: {text}
    Ответь в формате:
    Name: ...\nDate: ...\nTime: ...\nLocation: ...
    """
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}]
        )
        result = response.choices[0].message.content
    #    await update.message.reply_text(f'Параметры события успешно извлечены!\n{result}')
        event = parse_event_info(result)
        event['comment'] = ''
        context.user_data['event'] = event
        return await show_event_and_confirm(update, context)
    except Exception as e:
        await update.message.reply_text(f'Ошибка LLM: {e}')
        return ConversationHandler.END

def normalize_date(date_str):
    """
    Нормализует дату к формату ДД.ММ.ГГГГ.
    Если год не указан, добавляет текущий год.
    Поддерживает текстовые названия месяцев на русском и английском языках.
    """
    if not date_str:
        return ''
    
    date_str = date_str.strip()
    current_year = datetime.datetime.now().year
    
    # Словарь русских названий месяцев
    months_ru = {
        'января': 1, 'февраля': 2, 'марта': 3, 'апреля': 4,
        'мая': 5, 'июня': 6, 'июля': 7, 'августа': 8,
        'сентября': 9, 'октября': 10, 'ноября': 11, 'декабря': 12,
        'январь': 1, 'февраль': 2, 'март': 3, 'апрель': 4,
        'май': 5, 'июнь': 6, 'июль': 7, 'август': 8,
        'сентябрь': 9, 'октябрь': 10, 'ноябрь': 11, 'декабрь': 12
    }
    
    # Словарь английских названий месяцев
    months_en = {
        'january': 1, 'february': 2, 'march': 3, 'april': 4,
        'may': 5, 'june': 6, 'july': 7, 'august': 8,
        'september': 9, 'october': 10, 'november': 11, 'december': 12,
        'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4,
        'jun': 6, 'jul': 7, 'aug': 8, 'sep': 9, 'sept': 9,
        'oct': 10, 'nov': 11, 'dec': 12
    }
    
    # Объединяем словари для проверки
    all_months = {**months_ru, **months_en}
    
    # Пробуем распарсить дату с текстовым названием месяца (например, "13 декабря" или "December 13, 2024")
    date_lower = date_str.lower()
    for month_name, month_num in all_months.items():
        if month_name in date_lower:
            # Извлекаем все числа из строки
            numbers = re.findall(r'\d+', date_str)
            if numbers:
                day = int(numbers[0])
                # Извлекаем год, если есть
                year = current_year
                # Ищем год (4-значное число или 2-значное, которое может быть годом)
                year_pattern = re.search(r'\b(19|20)\d{2}\b', date_str)
                if year_pattern:
                    year = int(year_pattern.group())
                elif len(numbers) >= 2:
                    # Проверяем последнее число - если оно больше 31, это скорее всего год
                    year_candidate = int(numbers[-1])
                    if year_candidate > 31:
                        year = year_candidate
                        if year < 100:
                            year = 2000 + year if year < 50 else 1900 + year
                try:
                    dt = datetime.datetime(year, month_num, day)
                    return dt.strftime('%d.%m.%Y')
                except ValueError:
                    pass
            break
    
    # Пробуем разные форматы (включая английские текстовые форматы)
    formats_with_year = [
        '%d.%m.%Y',      # ДД.ММ.ГГГГ
        '%d.%m.%y',      # ДД.ММ.ГГ
        '%d/%m/%Y',      # ДД/ММ/ГГГГ
        '%d/%m/%y',      # ДД/ММ/ГГ
        '%Y-%m-%d',      # ГГГГ-ММ-ДД
        '%d-%m-%Y',      # ДД-ММ-ГГГГ
        '%B %d, %Y',     # December 13, 2024
        '%d %B %Y',      # 13 December 2024
        '%b %d, %Y',     # Dec 13, 2024
        '%d %b %Y',      # 13 Dec 2024
    ]
    
    for fmt in formats_with_year:
        try:
            dt = datetime.datetime.strptime(date_str, fmt)
            return dt.strftime('%d.%m.%Y')
        except ValueError:
            continue
    
    # Форматы без года (английские текстовые)
    formats_no_year_en = [
        ('%B %d', ', ', '%B %d, %Y'),      # December 13 -> December 13, 2025
        ('%d %B', ' ', '%d %B %Y'),        # 13 December -> 13 December 2025
        ('%b %d', ', ', '%b %d, %Y'),      # Dec 13 -> Dec 13, 2025
        ('%d %b', ' ', '%d %b %Y'),        # 13 Dec -> 13 Dec 2025
    ]
    
    for fmt_short, separator, fmt_full in formats_no_year_en:
        try:
            # Добавляем текущий год к строке и парсим с полным форматом
            date_with_year = f"{date_str}{separator}{current_year}"
            dt = datetime.datetime.strptime(date_with_year, fmt_full)
            return dt.strftime('%d.%m.%Y')
        except ValueError:
            continue
    
    # Форматы без года - добавляем год к строке перед парсингом, чтобы избежать DeprecationWarning
    formats_no_year = [
        ('%d.%m', '.', '%d.%m.%Y'),         # ДД.ММ (без года)
        ('%d/%m', '/', '%d/%m/%Y'),        # ДД/ММ (без года)
        ('%d-%m', '-', '%d-%m-%Y'),        # ДД-ММ (без года)
    ]
    
    for fmt_short, separator, fmt_full in formats_no_year:
        try:
            # Добавляем текущий год к строке и парсим с полным форматом
            date_with_year = f"{date_str}{separator}{current_year}"
            dt = datetime.datetime.strptime(date_with_year, fmt_full)
            return dt.strftime('%d.%m.%Y')
        except ValueError:
            continue
    
    # Если не удалось распарсить, пробуем извлечь числа вручную
    numbers = re.findall(r'\d+', date_str)
    if len(numbers) >= 2:
        day = int(numbers[0])
        month = int(numbers[1])
        year = int(numbers[2]) if len(numbers) >= 3 else current_year
        # Если год двухзначный, преобразуем в четырехзначный
        if year < 100:
            year = 2000 + year if year < 50 else 1900 + year
        try:
            dt = datetime.datetime(year, month, day)
            return dt.strftime('%d.%m.%Y')
        except ValueError:
            pass
    
    # Если ничего не помогло, возвращаем исходную строку
    return date_str

def normalize_time(time_str):
    """
    Нормализует время к формату ЧЧ:ММ-ЧЧ:ММ.
    Если время завершения не указано, добавляет +2 часа от времени начала.
    Если время не указано, возвращает 18:00-20:00.
    """
    if not time_str:
        return '18:00-20:00'
    
    time_str = time_str.strip()
    
    # Если уже в формате ЧЧ:ММ-ЧЧ:ММ, проверяем и возвращаем
    if '-' in time_str:
        parts = time_str.split('-')
        if len(parts) == 2:
            start_time = parts[0].strip()
            end_time = parts[1].strip()
            # Проверяем формат времени
            try:
                datetime.datetime.strptime(start_time, '%H:%M')
                datetime.datetime.strptime(end_time, '%H:%M')
                return f"{start_time}-{end_time}"
            except ValueError:
                pass
    
    # Пробуем распарсить одно время
    time_formats = ['%H:%M', '%H.%M', '%H:%M:%S', '%H.%M.%S']
    for fmt in time_formats:
        try:
            dt = datetime.datetime.strptime(time_str, fmt)
            start_time = dt.strftime('%H:%M')
            # Добавляем +2 часа для времени завершения
            end_dt = dt + datetime.timedelta(hours=2)
            end_time = end_dt.strftime('%H:%M')
            return f"{start_time}-{end_time}"
        except ValueError:
            continue
    
    # Пробуем извлечь числа вручную
    numbers = re.findall(r'\d+', time_str)
    if len(numbers) >= 2:
        hour = int(numbers[0])
        minute = int(numbers[1])
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            start_time = f"{hour:02d}:{minute:02d}"
            # Добавляем +2 часа
            dt = datetime.datetime(2000, 1, 1, hour, minute)
            end_dt = dt + datetime.timedelta(hours=2)
            end_time = end_dt.strftime('%H:%M')
            return f"{start_time}-{end_time}"
    
    # Если ничего не помогло, возвращаем дефолтное время
    return '18:00-20:00'

def parse_event_info(event_info):
    """
    Парсит параметры события из строки вида:
    Name: ...\nDate: ...\nTime: ...\nLocation: ...
    Возвращает dict с ключами name, date, time, location
    """
    result = {'name': '', 'date': '', 'time': '', 'location': ''}
    for line in event_info.split('\n'):
        if line.lower().startswith('name:'):
            result['name'] = line.split(':', 1)[1].strip()
        elif line.lower().startswith('date:'):
            date_str = line.split(':', 1)[1].strip()
            result['date'] = normalize_date(date_str)
        elif line.lower().startswith('time:'):
            time_str = line.split(':', 1)[1].strip()
            result['time'] = normalize_time(time_str)
        elif line.lower().startswith('location:'):
            result['location'] = line.split(':', 1)[1].strip()
    return result

def create_ics(event, organizer_email, attendee_email):
    """
    Создаёт строку .ics для календарного события с поддержкой RSVP (ответа на приглашение)
    event: dict с ключами name, date, time, location
    organizer_email: email организатора
    attendee_email: email приглашённого
    """
    date = event['date']
    time = event['time']
    try:
        if '-' in time:
            start_time, end_time = [t.strip() for t in time.split('-')]
        else:
            start_time = time.strip()
            end_time = start_time
        dt_start = datetime.datetime.strptime(date + ' ' + start_time, '%d.%m.%Y %H:%M')
        dt_end = datetime.datetime.strptime(date + ' ' + end_time, '%d.%m.%Y %H:%M')
    except Exception as e:
        raise ValueError(f"Ошибка разбора даты/времени: {e}. Проверьте формат даты (ДД.ММ.ГГГГ) и времени (ЧЧ:ММ или ЧЧ:ММ-ЧЧ:ММ).")
    dtstamp = datetime.datetime.now().strftime('%Y%m%dT%H%M%SZ')
    dtstart = dt_start.strftime('%Y%m%dT%H%M%S')
    dtend = dt_end.strftime('%Y%m%dT%H%M%S')
    uid = str(uuid.uuid4())
    description = f"Приглашение на событие. Комментарий: {event.get('comment', '')}\nПожалуйста, выберите ответ: Пойду/Не пойду"
    ics = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//AddCalendarBot//EN
METHOD:REQUEST
BEGIN:VEVENT
UID:{uid}
DTSTAMP:{dtstamp}
DTSTART:{dtstart}
DTEND:{dtend}
SUMMARY:{event['name']}
LOCATION:{event['location']}
DESCRIPTION:{description}
ORGANIZER;CN=Организатор:MAILTO:{organizer_email}
ATTENDEE;CN=Гость;RSVP=TRUE;PARTSTAT=NEEDS-ACTION:MAILTO:{attendee_email}
SEQUENCE:0
STATUS:CONFIRMED
TRANSP:OPAQUE
END:VEVENT
END:VCALENDAR"""
    return ics

def send_email(to_email, event):
    logger.info(f"Connecting to SMTP server {config.SMTP_SERVER}:{config.SMTP_PORT} as {config.EMAIL_LOGIN}")
    try:
        ics_content = create_ics(event, config.EMAIL_LOGIN, to_email)
    except ValueError as e:
        logger.error(f'Ошибка создания .ics: {e}')
        return str(e)  # Вернуть ошибку для пользователя
    msg = MIMEText(ics_content, 'calendar', 'utf-8')
    msg['Subject'] = f"Приглашение: {event['name']}"
    msg['From'] = config.EMAIL_LOGIN
    msg['To'] = to_email
    msg.add_header('Content-class', 'urn:content-classes:calendarmessage')
    msg.add_header('Content-Disposition', 'inline; filename="invite.ics"')
    msg.add_header('Content-Type', 'text/calendar; method=REQUEST; charset="UTF-8"')
    try:
        with smtplib.SMTP_SSL(config.SMTP_SERVER, config.SMTP_PORT) as server:
            server.login(config.EMAIL_LOGIN, config.EMAIL_PASSWORD)
            server.sendmail(config.EMAIL_LOGIN, to_email, msg.as_string())
    except Exception as e:
        logger.error(f'Ошибка отправки email: {e}')
        return f'Ошибка отправки email: {e}'
    return None

async def emails_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    emails = get_emails_in_chat(chat_id)
    if emails:
        await update.message.reply_text(f'Список email в чате:\n' + '\n'.join(emails))
    else:
        await update.message.reply_text('В чате нет зарегистрированных email.')

async def clear_emails_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    clear_emails_in_chat(chat_id)
    await update.message.reply_text('Список email очищен.')

async def remove_email_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if context.args:
        email = context.args[0]
        emails = get_emails_in_chat(chat_id)
        if email in emails:
            remove_email_in_chat(chat_id, email)
            await update.message.reply_text(f'Email {email} удалён.')
        else:
            await update.message.reply_text(f'Email {email} не найден в списке.')
    else:
        await update.message.reply_text('Пожалуйста, укажите email для удаления. Пример: /remove_email test@example.com')

async def add_email_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if context.args:
        email = context.args[0]
        emails = get_emails_in_chat(chat_id)
        if email in emails:
            await update.message.reply_text(f'Email {email} уже есть в списке.')
        else:
            add_user(chat_id, email)
            await update.message.reply_text(f'Email {email} добавлен.')
    else:
        await update.message.reply_text('Пожалуйста, укажите email для добавления. Пример: /add_email test@example.com')

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    menu_text = (
        "Доступные команды:\n"
        "/start — регистрация email или начало работы\n"
        "/add_email <email> — добавить email вручную\n"
        "/remove_email <email> — удалить email из списка\n"
        "/clear_emails — очистить весь список email\n"
        "/emails — показать список email\n"
        "/menu — показать это меню\n"
        "\n"
        "Вы также можете отправить текстовое сообщение или фотографию с текстом события — бот распознает параметры и отправит приглашение на email."
    )
    await update.message.reply_text(menu_text)

def main():
    application = Application.builder().token(config.TELEGRAM_TOKEN).build()
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),
            MessageHandler(filters.PHOTO, handle_photo)
        ],
        states={
            EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_email)],
            CONFIRM_EVENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_event)],
            CHOOSE_FIELD: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_field)],
            EDIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_field)],
            EDIT_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_field)],
            EDIT_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_field)],
            EDIT_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_field)],
            EDIT_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_field)],
        },
        fallbacks=[CommandHandler('menu', menu_command)]
    )
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler('emails', emails_command))
    application.add_handler(CommandHandler('clear_emails', clear_emails_command))
    application.add_handler(CommandHandler('remove_email', remove_email_command))
    application.add_handler(CommandHandler('add_email', add_email_command))
    application.add_handler(CommandHandler('menu', menu_command))
    application.run_polling()

if __name__ == '__main__':
    main() 