import asyncio
import logging
import os
import sqlite3
import smtplib
import tempfile
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
import openai
import config
import datetime
from zoneinfo import ZoneInfo
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
# Удаляем дубликаты (chat_id, email) и добавляем уникальный индекс
c.execute(
    '''DELETE FROM users WHERE rowid NOT IN (
        SELECT MIN(rowid) FROM users GROUP BY chat_id, email
    )'''
)
conn.commit()
c.execute(
    'CREATE UNIQUE INDEX IF NOT EXISTS ux_users_chat_email ON users(chat_id, email)'
)
conn.commit()

# Отправленные встречи: нужны, чтобы потом изменить или отменить их по тому же UID.
c.execute('''CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    uid TEXT NOT NULL UNIQUE,
    sequence INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    location TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    starts_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_by INTEGER,
    created_by_name TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)''')
c.execute('CREATE INDEX IF NOT EXISTS ix_events_chat ON events(chat_id, status, starts_at)')
c.execute('''CREATE TABLE IF NOT EXISTS event_recipients (
    event_id INTEGER NOT NULL REFERENCES events(id),
    email TEXT NOT NULL,
    PRIMARY KEY (event_id, email)
)''')
conn.commit()

# Состояния для ConversationHandler
EMAIL, CONFIRM_EVENT, CHOOSE_FIELD, EDIT_VALUE, CONFIRM_UPDATE = range(5)

EVENT_FIELDS = [
    ('name', 'Название'),
    ('date', 'Дата'),
    ('time', 'Время'),
    ('location', 'Место'),
    ('comment', 'Комментарий'),
]
FIELD_LABELS = dict(EVENT_FIELDS)
# Текстовые названия полей, которые принимаются вместо нажатия кнопки.
FIELD_ALIASES = {
    **{key: key for key, _ in EVENT_FIELDS},
    **{label.lower(): key for key, label in EVENT_FIELDS},
}
FIELD_PROMPTS = {
    'name': 'Введите новое название события:',
    'date': 'Введите новую дату события (например, 10.06.2026):',
    'time': 'Введите новое время события (например, 13:30-15:00):',
    'location': 'Введите новое место проведения события:',
    'comment': 'Введите новый комментарий (или «-», чтобы очистить):',
}
EVENTS_PAGE_SIZE = 10
EVENT_DB_COLUMNS = (
    'id', 'chat_id', 'uid', 'sequence', 'name', 'date', 'time', 'location',
    'comment', 'starts_at', 'status', 'created_by', 'created_by_name',
)

# Установка ключа OpenAI
client = openai.OpenAI(api_key=config.OPENAI_API_KEY)

def add_user(chat_id, email) -> bool:
    """Возвращает True, если email добавлен, False если такая пара уже была."""
    try:
        c.execute('INSERT INTO users (chat_id, email) VALUES (?, ?)', (chat_id, email))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        conn.rollback()
        return False

def get_emails_in_chat(chat_id):
    c.execute('SELECT email FROM users WHERE chat_id=?', (chat_id,))
    return [row[0] for row in c.fetchall()]

def clear_emails_in_chat(chat_id):
    c.execute('DELETE FROM users WHERE chat_id=?', (chat_id,))
    conn.commit()

def remove_email_in_chat(chat_id, email):
    c.execute('DELETE FROM users WHERE chat_id=? AND email=?', (chat_id, email))
    conn.commit()


def _utc_iso(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _now_iso() -> str:
    return _utc_iso(datetime.datetime.now(datetime.timezone.utc))


def save_event(chat_id, event, uid, user, recipients) -> int:
    dt_start, _ = event_datetimes(event)
    now = _now_iso()
    author = f'@{user.username}' if user.username else user.full_name
    c.execute(
        '''INSERT INTO events (chat_id, uid, sequence, name, date, time, location, comment,
               starts_at, created_by, created_by_name, created_at, updated_at)
           VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            chat_id, uid, event['name'], event['date'], event['time'], event['location'],
            event.get('comment') or '', _utc_iso(dt_start), user.id, author, now, now,
        ),
    )
    event_id = c.lastrowid
    c.executemany(
        'INSERT OR IGNORE INTO event_recipients (event_id, email) VALUES (?, ?)',
        [(event_id, email) for email in recipients],
    )
    conn.commit()
    return event_id


def get_event(event_id):
    c.execute(
        f'SELECT {", ".join(EVENT_DB_COLUMNS)} FROM events WHERE id=?', (event_id,)
    )
    row = c.fetchone()
    return dict(zip(EVENT_DB_COLUMNS, row)) if row else None


def update_event(event_id, event, sequence):
    dt_start, _ = event_datetimes(event)
    c.execute(
        '''UPDATE events SET name=?, date=?, time=?, location=?, comment=?, starts_at=?,
               sequence=?, updated_at=? WHERE id=?''',
        (
            event['name'], event['date'], event['time'], event['location'],
            event.get('comment') or '', _utc_iso(dt_start), sequence, _now_iso(), event_id,
        ),
    )
    conn.commit()


def cancel_event(event_id, sequence):
    c.execute(
        "UPDATE events SET status='cancelled', sequence=?, updated_at=? WHERE id=?",
        (sequence, _now_iso(), event_id),
    )
    conn.commit()


def get_event_recipients(event_id):
    c.execute('SELECT email FROM event_recipients WHERE event_id=? ORDER BY rowid', (event_id,))
    return [row[0] for row in c.fetchall()]


def add_event_recipients(event_id, emails):
    c.executemany(
        'INSERT OR IGNORE INTO event_recipients (event_id, email) VALUES (?, ?)',
        [(event_id, email) for email in emails],
    )
    conn.commit()


def list_upcoming_events(chat_id, offset, limit):
    """Активные встречи чата, которые ещё не начались: (страница, общее число)."""
    now = _now_iso()
    c.execute(
        "SELECT COUNT(*) FROM events WHERE chat_id=? AND status='active' AND starts_at>=?",
        (chat_id, now),
    )
    total = c.fetchone()[0]
    c.execute(
        f'''SELECT {", ".join(EVENT_DB_COLUMNS)} FROM events
            WHERE chat_id=? AND status='active' AND starts_at>=?
            ORDER BY starts_at, id LIMIT ? OFFSET ?''',
        (chat_id, now, limit, offset),
    )
    return [dict(zip(EVENT_DB_COLUMNS, row)) for row in c.fetchall()], total


def event_missing_required_fields(event: dict) -> list:
    """Поля, без которых нельзя собрать приглашение (время подставляется по умолчанию)."""
    missing = []
    if not (event.get('name') or '').strip():
        missing.append('название')
    if not (event.get('date') or '').strip():
        missing.append('дата')
    if not (event.get('location') or '').strip():
        missing.append('место')
    return missing


def _is_group(chat) -> bool:
    return bool(chat) and chat.type in ('group', 'supergroup')


async def _is_chat_admin(context: ContextTypes.DEFAULT_TYPE, chat_id, user_id) -> bool:
    try:
        m = await context.bot.get_chat_member(chat_id, user_id)
        return m.status in ('creator', 'administrator')
    except Exception:
        return False


async def _user_may_manage_recipients(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """В группах список получателей меняют только админы и владелец."""
    chat = update.effective_chat
    if not _is_group(chat):
        return True
    return await _is_chat_admin(context, chat.id, update.effective_user.id)


async def _user_may_manage_event(update: Update, context: ContextTypes.DEFAULT_TYPE, row) -> bool:
    """В группах встречу меняют и удаляют её автор и админы чата."""
    chat = update.effective_chat
    if not _is_group(chat):
        return True
    if row['created_by'] == update.effective_user.id:
        return True
    return await _is_chat_admin(context, chat.id, update.effective_user.id)


async def _check_event_access(update: Update, context: ContextTypes.DEFAULT_TYPE, event_id):
    """(встреча, None), если её можно менять, иначе (None, текст причины)."""
    row = get_event(event_id)
    if not row or row['chat_id'] != update.effective_chat.id:
        return None, 'Встреча не найдена.'
    if row['status'] != 'active':
        return None, 'Встреча уже удалена.'
    if row['starts_at'] < _now_iso():
        return None, 'Встреча уже прошла, её нельзя изменить или удалить.'
    if not await _user_may_manage_event(update, context, row):
        return None, 'Изменять и удалять встречу могут только её автор и админы чата.'
    return row, None


def _ru_plural(n, one, few, many) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def format_event(event) -> str:
    comment = (event.get('comment') or '').strip() or '—'
    return (
        f"«{event['name']}»\n"
        f"📅 {event['date']}  🕙 {event['time']}\n"
        f"📍 {event['location']}\n"
        f"💬 {comment}"
    )


def _delivery_report(groups) -> str:
    """groups: [(заголовок, отправлено, ошибки)] → текст отчёта о рассылке."""
    parts = []
    for title, sent, errors in groups:
        if sent:
            parts.append(f'{title}: ' + ', '.join(sent))
        if errors:
            parts.append('Не удалось:\n' + '\n'.join(errors))
    return '\n\n'.join(parts) if parts else 'Нет результатов.'


def _event_actions_keyboard(event_id, back_to_list=False) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton('✏️ Изменить', callback_data=f'ev:edit:{event_id}'),
        InlineKeyboardButton('🗑 Удалить', callback_data=f'ev:del:{event_id}'),
    ]]
    if back_to_list:
        rows.append([InlineKeyboardButton('⬅️ К списку', callback_data='ev:list:0')])
    return InlineKeyboardMarkup(rows)


FIELD_PICKER = InlineKeyboardMarkup([
    [
        InlineKeyboardButton('Название', callback_data='field:name'),
        InlineKeyboardButton('Дата', callback_data='field:date'),
        InlineKeyboardButton('Время', callback_data='field:time'),
    ],
    [
        InlineKeyboardButton('Место', callback_data='field:location'),
        InlineKeyboardButton('Комментарий', callback_data='field:comment'),
    ],
    [InlineKeyboardButton('❌ Отмена', callback_data='field:cancel')],
])


async def _safe_edit_text(message, text, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if 'not modified' not in str(e).lower():
            raise


async def _drop_buttons(query):
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass


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
    if add_user(chat_id, email):
        await update.message.reply_text(f'Email {email} сохранён! Теперь отправьте информацию о событии.')
    else:
        await update.message.reply_text(
            f'Email {email} уже был в списке. Можете прислать описание события.'
        )
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
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )
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
        missing = event_missing_required_fields(event)
        if missing:
            await update.message.reply_text(
                'В тексте не хватает: '
                + ', '.join(missing)
                + '.\nПришлите описание события ещё раз, явно указав эти данные.'
            )
            return ConversationHandler.END
        context.user_data.clear()
        context.user_data['event'] = event
        return await show_event_and_confirm(update, context)
    except Exception as e:
        logger.exception('LLM error: %s', e)
        await update.message.reply_text(
            'Не получилось разобрать событие. Попробуйте короче указать '
            'название, дату, время и место в одном сообщении.'
        )
        return ConversationHandler.END

async def confirm_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.message.text.strip().lower()
    event = context.user_data.get('event')
    if event is None:
        return await _conversation_lost(update.effective_message, context)
    if answer in ['да', 'yes']:
        return await _send_invites_and_finish(update.effective_message, context, update.effective_user)
    if answer in ['нет', 'no']:
        return await _ask_field(update.effective_message, context)
    # Любой другой текст считаем комментарием к событию
    event['comment'] = update.message.text.strip()
    return await show_event_and_confirm(update, context)


async def _conversation_lost(message, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await message.reply_text('Не нашла черновик события. Пришлите описание события заново.')
    return ConversationHandler.END


async def _ask_field(message, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('edit_event_id'):
        text = f"Что изменить во встрече «{context.user_data['original']['name']}»?"
    else:
        text = 'Какой параметр хотите исправить?'
    await message.reply_text(text, reply_markup=FIELD_PICKER)
    return CHOOSE_FIELD


async def _ask_value(message, context: ContextTypes.DEFAULT_TYPE, field):
    context.user_data['edit_field'] = field
    current = (context.user_data['event'].get(field) or '').strip() or '—'
    await message.reply_text(f'Сейчас: {current}\n{FIELD_PROMPTS[field]}')
    return EDIT_VALUE


async def _cancel_editing(message, context: ContextTypes.DEFAULT_TYPE):
    text = 'Изменение встречи отменено.' if context.user_data.get('edit_event_id') else 'Создание события отменено.'
    context.user_data.clear()
    await message.reply_text(text, reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор поля текстом (Name/Название и т.п.) вместо кнопки."""
    if context.user_data.get('event') is None:
        return await _conversation_lost(update.effective_message, context)
    field = FIELD_ALIASES.get(update.message.text.strip().lower())
    if field:
        return await _ask_value(update.effective_message, context, field)
    await update.message.reply_text('Выберите поле кнопкой ниже.', reply_markup=FIELD_PICKER)
    return CHOOSE_FIELD


async def edit_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await _drop_buttons(query)
    if context.user_data.get('event') is None:
        return await _conversation_lost(query.message, context)
    field = (query.data or '').split(':', 1)[1]
    if field == 'cancel':
        return await _cancel_editing(query.message, context)
    if field not in FIELD_LABELS:
        return await _ask_field(query.message, context)
    return await _ask_value(query.message, context, field)


async def set_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data.get('edit_field')
    event = context.user_data.get('event')
    if event is None:
        return await _conversation_lost(update.effective_message, context)
    if not field:
        return await _ask_field(update.effective_message, context)
    raw = update.message.text.strip()
    if field == 'date':
        val = normalize_date(raw)
        if not re.fullmatch(r'\d{2}\.\d{2}\.\d{4}', val):
            await update.message.reply_text('Не удалось распознать дату. Пример: 10.06.2026')
            return EDIT_VALUE
    elif field == 'time':
        val = normalize_time(raw)
    elif field == 'comment' and raw == '-':
        val = ''
    else:
        val = raw
    event[field] = val
    context.user_data.pop('edit_field', None)
    if context.user_data.get('edit_event_id'):
        return await show_update_preview(update.effective_message, context)
    # После изменения любого поля снова показываем все параметры и спрашиваем подтверждение
    return await show_event_and_confirm(update, context)


async def show_event_and_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    event = context.user_data['event']
    inline = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton('✅ Отправить', callback_data='event_confirm'),
                InlineKeyboardButton('✏️ Изменить', callback_data='event_edit'),
            ],
            [InlineKeyboardButton('❌ Отмена', callback_data='event_cancel')],
        ]
    )
    await update.effective_message.reply_text(
        'Проверьте параметры события:\n\n'
        + format_event(event)
        + '\n\nВсё верно? Нажмите кнопку или напишите «Да» / «Нет». '
        'Любой другой текст станет комментарием к событию.',
        reply_markup=inline,
    )
    return CONFIRM_EVENT


async def confirm_event_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await _drop_buttons(query)
    data = query.data or ''
    if context.user_data.get('event') is None:
        return await _conversation_lost(query.message, context)
    if data == 'event_cancel':
        return await _cancel_editing(query.message, context)
    if data == 'event_edit':
        return await _ask_field(query.message, context)
    if data == 'event_confirm':
        return await _send_invites_and_finish(query.message, context, query.from_user)
    return CONFIRM_EVENT


async def _deliver(emails, event, uid, sequence, method, subject_prefix):
    """Рассылает письмо каждому адресу; возвращает (успешные, ошибки)."""
    sent, errors = [], []
    for email in emails:
        err = await asyncio.to_thread(
            send_email, email, event, uid, sequence, method, subject_prefix
        )
        if err:
            short = err if len(err) <= 300 else err[:297] + '...'
            errors.append(f'{email}: {short}')
        else:
            sent.append(email)
    return sent, errors


async def _send_invites_and_finish(message, context: ContextTypes.DEFAULT_TYPE, user):
    """Отправка приглашений всем адресам чата; итог одним сообщением."""
    event = context.user_data.get('event', {})
    chat_id = message.chat_id
    try:
        event_datetimes(event)
    except ValueError as e:
        await message.reply_text(f'{e}\nИсправьте дату или время.')
        return await _ask_field(message, context)
    emails = get_emails_in_chat(chat_id)
    if not emails:
        await message.reply_text('Нет адресов в списке. Сначала /start или /add_email.')
        context.user_data.clear()
        return ConversationHandler.END
    logger.info('Sending invites for chat_id=%s, event=%s', chat_id, event)
    await message.reply_text('Отправляю приглашения на email...', reply_markup=ReplyKeyboardRemove())
    uid = f'{uuid.uuid4()}@addcalendrbot'
    sent, errors = await _deliver(emails, event, uid, 0, 'REQUEST', 'Приглашение')
    report = _delivery_report([('Отправлено', sent, errors)])
    if sent:
        event_id = save_event(chat_id, event, uid, user, sent)
        await message.reply_text(
            'Встреча отправлена.\n\n' + format_event(event) + '\n\n' + report,
            reply_markup=_event_actions_keyboard(event_id),
        )
    else:
        await message.reply_text(report)
    context.user_data.clear()
    return ConversationHandler.END


async def edit_event_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка «Изменить» у сохранённой встречи: открывает редактор полей."""
    query = update.callback_query
    event_id = int(query.data.split(':')[2])
    row, error = await _check_event_access(update, context, event_id)
    if error:
        await query.answer(error, show_alert=True)
        return None
    await query.answer()
    context.user_data.clear()
    event = {key: row[key] for key, _ in EVENT_FIELDS}
    context.user_data.update(
        event=event,
        original=dict(event),
        edit_event_id=event_id,
        invite_new=False,
    )
    return await _ask_field(query.message, context)


def _changed_fields(old, new):
    return [key for key, _ in EVENT_FIELDS if (old.get(key) or '') != (new.get(key) or '')]


async def show_update_preview(message, context: ContextTypes.DEFAULT_TYPE, edit=False):
    ud = context.user_data
    event, original, event_id = ud['event'], ud['original'], ud['edit_event_id']
    changed = _changed_fields(original, event)
    recipients = get_event_recipients(event_id)
    new_emails = [e for e in get_emails_in_chat(message.chat_id) if e not in recipients]
    invite_new = ud.get('invite_new') and bool(new_emails)

    lines = []
    if changed:
        lines.append(f"Изменения во встрече «{original['name']}»:")
        for key in changed:
            lines.append(f"{FIELD_LABELS[key]}: {original[key] or '—'} → {event[key] or '—'}")
        lines.append('')
        lines.append('Участники получат обновление, и встреча в их календаре изменится.')
        try:
            dt_start, _ = event_datetimes(event)
            if dt_start < datetime.datetime.now(dt_start.tzinfo):
                lines.append('⚠️ Новое время уже в прошлом.')
        except ValueError as e:
            lines.append(f'⚠️ {e}')
    else:
        lines.append(f"Во встрече «{original['name']}» пока нет изменений.")
    if new_emails:
        lines.append('')
        if invite_new:
            lines.append('Приглашение также получат новые адреса: ' + ', '.join(new_emails))
        else:
            lines.append(
                f'В чате есть новые адреса ({len(new_emails)}), которым эта встреча не отправлялась.'
            )

    buttons = []
    if changed or invite_new:
        buttons.append([InlineKeyboardButton('✅ Отправить обновление', callback_data='upd:send')])
    buttons.append([InlineKeyboardButton('✏️ Ещё правка', callback_data='upd:more')])
    if new_emails:
        label = '➖ Не приглашать новых' if invite_new else f'➕ Пригласить новых ({len(new_emails)})'
        buttons.append([InlineKeyboardButton(label, callback_data='upd:invite')])
    buttons.append([InlineKeyboardButton('❌ Отмена', callback_data='upd:cancel')])
    markup = InlineKeyboardMarkup(buttons)
    if edit:
        await _safe_edit_text(message, '\n'.join(lines), reply_markup=markup)
    else:
        await message.reply_text('\n'.join(lines), reply_markup=markup)
    return CONFIRM_UPDATE


async def update_event_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = (query.data or '').split(':', 1)[1]
    if not context.user_data.get('edit_event_id'):
        await _drop_buttons(query)
        return await _conversation_lost(query.message, context)
    if action == 'invite':
        context.user_data['invite_new'] = not context.user_data.get('invite_new')
        return await show_update_preview(query.message, context, edit=True)
    await _drop_buttons(query)
    if action == 'cancel':
        return await _cancel_editing(query.message, context)
    if action == 'more':
        return await _ask_field(query.message, context)
    if action == 'send':
        return await _send_update_and_finish(update, context)
    return CONFIRM_UPDATE


async def _send_update_and_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.callback_query.message
    ud = context.user_data
    event, event_id = ud['event'], ud['edit_event_id']
    # Права и статус проверяем ещё раз: встречу могли удалить, пока шло редактирование
    row, error = await _check_event_access(update, context, event_id)
    if error:
        context.user_data.clear()
        await message.reply_text(error)
        return ConversationHandler.END
    try:
        event_datetimes(event)
    except ValueError as e:
        await message.reply_text(f'{e}\nИсправьте дату или время.')
        return await _ask_field(message, context)

    changed = bool(_changed_fields(row, event))
    recipients = get_event_recipients(event_id)
    new_emails = []
    if ud.get('invite_new'):
        new_emails = [e for e in get_emails_in_chat(message.chat_id) if e not in recipients]
    if not changed and not new_emails:
        await message.reply_text('Изменений нет, отправлять нечего.')
        return await show_update_preview(message, context)

    sequence = row['sequence'] + 1 if changed else row['sequence']
    await message.reply_text('Отправляю обновление на email...')
    sent_upd, err_upd = [], []
    if changed:
        sent_upd, err_upd = await _deliver(recipients, event, row['uid'], sequence, 'REQUEST', 'Изменено')
        if recipients and not sent_upd:
            # Никто не получил обновление: оставляем встречу как была, чтобы можно было повторить
            context.user_data.clear()
            await message.reply_text(
                'Не удалось отправить обновление, встреча не изменена.\n\n'
                + _delivery_report([('Обновление получили', sent_upd, err_upd)]),
                reply_markup=_event_actions_keyboard(event_id),
            )
            return ConversationHandler.END
        update_event(event_id, event, sequence)
    sent_new, err_new = await _deliver(new_emails, event, row['uid'], sequence, 'REQUEST', 'Приглашение')
    add_event_recipients(event_id, sent_new)

    report = _delivery_report([
        ('Обновление получили', sent_upd, err_upd),
        ('Приглашение получили', sent_new, err_new),
    ])
    title = 'Встреча обновлена.' if changed else 'Приглашения отправлены.'
    await message.reply_text(
        f'{title}\n\n' + format_event(event) + '\n\n' + report,
        reply_markup=_event_actions_keyboard(event_id),
    )
    context.user_data.clear()
    return ConversationHandler.END


def _events_list_view(chat_id, page):
    rows, total = list_upcoming_events(chat_id, page * EVENTS_PAGE_SIZE, EVENTS_PAGE_SIZE)
    if not rows and page > 0:
        page = 0
        rows, total = list_upcoming_events(chat_id, 0, EVENTS_PAGE_SIZE)
    if not rows:
        return (
            'Предстоящих встреч нет.\n'
            'Встречи, отправленные до появления этой функции, здесь не показываются.',
            None,
        )
    buttons = []
    for row in rows:
        start = row['time'].split('-')[0].strip()
        name = row['name'] if len(row['name']) <= 40 else row['name'][:39] + '…'
        buttons.append([InlineKeyboardButton(
            f"{row['date'][:5]} {start} · {name}", callback_data=f"ev:open:{row['id']}"
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton('◀️', callback_data=f'ev:list:{page - 1}'))
    if (page + 1) * EVENTS_PAGE_SIZE < total:
        nav.append(InlineKeyboardButton('▶️', callback_data=f'ev:list:{page + 1}'))
    if nav:
        buttons.append(nav)
    return f'Ближайшие встречи ({total}):', InlineKeyboardMarkup(buttons)


async def events_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, markup = _events_list_view(update.effective_chat.id, 0)
    await update.message.reply_text(text, reply_markup=markup)


async def events_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список встреч, карточка встречи и удаление (без диалога)."""
    query = update.callback_query
    _, action, arg = (query.data or '').split(':', 2)
    arg = int(arg)
    chat_id = update.effective_chat.id

    if action == 'list':
        await query.answer()
        text, markup = _events_list_view(chat_id, arg)
        await _safe_edit_text(query.message, text, reply_markup=markup)
        return

    if action == 'open':
        await query.answer()
        row = get_event(arg)
        back = InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ К списку', callback_data='ev:list:0')]])
        if not row or row['chat_id'] != chat_id or row['status'] != 'active':
            await _safe_edit_text(query.message, 'Встреча не найдена или удалена.', reply_markup=back)
            return
        count = len(get_event_recipients(arg))
        text = (
            format_event(row)
            + f"\n👥 Отправлено: {count} {_ru_plural(count, 'адрес', 'адреса', 'адресов')}"
        )
        if row['created_by_name']:
            text += f" · создал(а) {row['created_by_name']}"
        if row['starts_at'] < _now_iso():
            await _safe_edit_text(query.message, text + '\n\nВстреча уже прошла.', reply_markup=back)
            return
        await _safe_edit_text(query.message, text, reply_markup=_event_actions_keyboard(arg, back_to_list=True))
        return

    if action == 'nodel':
        await query.answer()
        await _safe_edit_text(query.message, 'Удаление отменено.')
        return

    if action not in ('del', 'delok'):
        await query.answer()
        return

    row, error = await _check_event_access(update, context, arg)
    if error:
        await query.answer(error, show_alert=True)
        if action == 'delok':
            await _safe_edit_text(query.message, error)
        return
    await query.answer()
    recipients = get_event_recipients(arg)

    if action == 'del':
        count = len(recipients)
        await query.message.reply_text(
            f"Удалить «{row['name']}» {row['date']} {row['time']}?\n"
            f"Участникам ({count}) придёт отмена, и встреча пропадёт "
            "или будет зачёркнута в их календарях.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton('🗑 Да, удалить', callback_data=f'ev:delok:{arg}'),
                InlineKeyboardButton('Не удалять', callback_data=f'ev:nodel:{arg}'),
            ]]),
        )
        return

    # action == 'delok'
    await _safe_edit_text(query.message, f"Отправляю отмену встречи «{row['name']}»...")
    sequence = row['sequence'] + 1
    sent, errors = await _deliver(recipients, row, row['uid'], sequence, 'CANCEL', 'Отменено')
    if recipients and not sent:
        await _safe_edit_text(
            query.message,
            'Не удалось отправить отмену, встреча не удалена.\n\n'
            + _delivery_report([('Отмену получили', sent, errors)]),
            reply_markup=_event_actions_keyboard(arg),
        )
        return
    cancel_event(arg, sequence)
    await _safe_edit_text(
        query.message,
        f"Встреча «{row['name']}» удалена.\n\n"
        + _delivery_report([('Отмену получили', sent, errors)]),
    )

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
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )
    await update.message.reply_text('Получено изображение. Сохраняю и извлекаю текст с помощью OCR...')
    photo_file = await update.message.photo[-1].get_file()
    fd, photo_path = tempfile.mkstemp(suffix='.jpg')
    try:
        os.close(fd)
        await photo_file.download_to_drive(photo_path)
        reader = _get_easyocr_reader()
        ocr_result = reader.readtext(photo_path, detail=0, paragraph=True)
        text = '\n'.join(ocr_result)
    except Exception as e:
        logger.exception('OCR error: %s', e)
        await update.message.reply_text(
            'Не удалось прочитать текст с картинки. Попробуйте другое фото или пришлите текст сообщением.'
        )
        return
    finally:
        try:
            os.unlink(photo_path)
        except OSError:
            pass
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
        event = parse_event_info(result)
        event['comment'] = ''
        missing = event_missing_required_fields(event)
        if missing:
            await update.message.reply_text(
                'По фото не хватает: '
                + ', '.join(missing)
                + '.\nПришлите фото или текст ещё раз с явными данными.'
            )
            return ConversationHandler.END
        context.user_data.clear()
        context.user_data['event'] = event
        return await show_event_and_confirm(update, context)
    except Exception as e:
        logger.exception('LLM error (photo): %s', e)
        await update.message.reply_text(
            'Не получилось разобрать событие. Попробуйте прислать текстом название, дату, время и место.'
        )
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

def _event_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(config.EVENT_TIMEZONE)
    except Exception as e:
        raise ValueError(
            f"Неверный EVENT_TIMEZONE={config.EVENT_TIMEZONE!r}: {e}. "
            "Пример: Europe/Moscow"
        ) from e


def event_datetimes(event):
    """Начало и конец события (aware datetime в EVENT_TIMEZONE)."""
    tz = _event_timezone()
    date = (event.get('date') or '').strip()
    time = (event.get('time') or '').strip()
    try:
        if '-' in time:
            start_time, end_time = [t.strip() for t in time.split('-', 1)]
        else:
            start_time = end_time = time
        dt_start = datetime.datetime.strptime(
            date + ' ' + start_time, '%d.%m.%Y %H:%M'
        ).replace(tzinfo=tz)
        dt_end = datetime.datetime.strptime(
            date + ' ' + end_time, '%d.%m.%Y %H:%M'
        ).replace(tzinfo=tz)
    except Exception as e:
        raise ValueError(f"Ошибка разбора даты/времени: {e}. Проверьте формат даты (ДД.ММ.ГГГГ) и времени (ЧЧ:ММ или ЧЧ:ММ-ЧЧ:ММ).")
    if dt_end < dt_start:
        # Например, 23:00-01:00 — конец на следующий день
        dt_end += datetime.timedelta(days=1)
    return dt_start, dt_end


def _ics_escape(value) -> str:
    return (
        str(value or '')
        .replace('\\', '\\\\')
        .replace(';', '\\;')
        .replace(',', '\\,')
        .replace('\r\n', '\\n')
        .replace('\r', '\\n')
        .replace('\n', '\\n')
    )


def _ics_fold(line: str) -> str:
    """RFC 5545: строки длиннее 75 октетов переносятся, не разрывая UTF-8 символы."""
    chunks, current, size = [], '', 0
    for ch in line:
        n = len(ch.encode('utf-8'))
        if size + n > 75:
            chunks.append(current)
            current, size = ' ', 1
        current += ch
        size += n
    chunks.append(current)
    return '\r\n'.join(chunks)


def create_ics(event, organizer_email, attendee_email, uid, sequence=0, method='REQUEST'):
    """
    Создаёт .ics для календарного события.
    method=REQUEST — приглашение или обновление (тот же uid, больший sequence),
    method=CANCEL — отмена встречи.
    """
    dt_start, dt_end = event_datetimes(event)
    dtstamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    tzid = config.EVENT_TIMEZONE
    comment = (event.get('comment') or '').strip()
    if method == 'CANCEL':
        description = 'Встреча отменена организатором.'
        status = 'CANCELLED'
        attendee = f'ATTENDEE;CN=Гость:MAILTO:{attendee_email}'
    else:
        description = (
            f"Приглашение на событие. Комментарий: {comment}\n"
            "Пожалуйста, выберите ответ: Пойду/Не пойду"
        )
        status = 'CONFIRMED'
        attendee = f'ATTENDEE;CN=Гость;RSVP=TRUE;PARTSTAT=NEEDS-ACTION:MAILTO:{attendee_email}'
    lines = [
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        'PRODID:-//AddCalendarBot//EN',
        f'METHOD:{method}',
        'BEGIN:VEVENT',
        f'UID:{uid}',
        f'DTSTAMP:{dtstamp}',
        f"DTSTART;TZID={tzid}:{dt_start.strftime('%Y%m%dT%H%M%S')}",
        f"DTEND;TZID={tzid}:{dt_end.strftime('%Y%m%dT%H%M%S')}",
        f"SUMMARY:{_ics_escape(event['name'])}",
        f"LOCATION:{_ics_escape(event['location'])}",
        f'DESCRIPTION:{_ics_escape(description)}',
        f'ORGANIZER;CN=Организатор:MAILTO:{organizer_email}',
        attendee,
        f'SEQUENCE:{sequence}',
        f'STATUS:{status}',
        'TRANSP:OPAQUE',
        'END:VEVENT',
        'END:VCALENDAR',
    ]
    return '\r\n'.join(_ics_fold(line) for line in lines) + '\r\n'


def send_email(to_email, event, uid, sequence=0, method='REQUEST', subject_prefix='Приглашение'):
    logger.info(f"Connecting to SMTP server {config.SMTP_SERVER}:{config.SMTP_PORT} as {config.EMAIL_LOGIN}")
    try:
        ics_content = create_ics(event, config.EMAIL_LOGIN, to_email, uid, sequence, method)
    except ValueError as e:
        logger.error(f'Ошибка создания .ics: {e}')
        return str(e)  # Вернуть ошибку для пользователя
    if method == 'CANCEL':
        plain = 'Встреча отменена.\n\n' + format_event(event)
    else:
        plain = format_event(event)
    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"{subject_prefix}: {event['name']}"
    msg['From'] = config.EMAIL_LOGIN
    msg['To'] = to_email
    msg.add_header('Content-class', 'urn:content-classes:calendarmessage')
    msg.attach(MIMEText(plain, 'plain', 'utf-8'))
    calendar_part = MIMEText(ics_content, 'calendar', 'utf-8')
    calendar_part.set_param('method', method)
    msg.attach(calendar_part)
    try:
        with smtplib.SMTP_SSL(config.SMTP_SERVER, config.SMTP_PORT) as server:
            server.login(config.EMAIL_LOGIN, config.EMAIL_PASSWORD)
            server.sendmail(config.EMAIL_LOGIN, to_email, msg.as_string())
    except smtplib.SMTPAuthenticationError as e:
        logger.error('SMTP authentication failed: %s', e)
        return (
            "Почта отклонила вход (неверный логин или пароль). "
            "Для Gmail в EMAIL_PASSWORD нужен пароль приложения, не обычный пароль аккаунта: "
            "https://myaccount.google.com/apppasswords — после смены пароля на проде обновите "
            "GitHub Secret EMAIL_PASSWORD и задеплойте снова (или поправьте /opt/addcalendrbot/.env)."
        )
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
    if not await _user_may_manage_recipients(update, context):
        await update.message.reply_text(
            'Только администраторы чата могут очистить список email.'
        )
        return
    chat_id = update.effective_chat.id
    clear_emails_in_chat(chat_id)
    await update.message.reply_text('Список email очищен.')

async def remove_email_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _user_may_manage_recipients(update, context):
        await update.message.reply_text(
            'Только администраторы чата могут удалять email из списка.'
        )
        return
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
    if not await _user_may_manage_recipients(update, context):
        await update.message.reply_text(
            'Только администраторы чата могут добавлять email в список.'
        )
        return
    chat_id = update.effective_chat.id
    if context.args:
        email = context.args[0]
        emails = get_emails_in_chat(chat_id)
        if email in emails:
            await update.message.reply_text(f'Email {email} уже есть в списке.')
        elif add_user(chat_id, email):
            await update.message.reply_text(f'Email {email} добавлен.')
        else:
            await update.message.reply_text(f'Email {email} уже есть в списке.')
    else:
        await update.message.reply_text('Пожалуйста, укажите email для добавления. Пример: /add_email test@example.com')

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    menu_text = (
        "Доступные команды:\n"
        "/start — регистрация email или начало работы\n"
        "/add_email <email> — добавить email вручную (в группах — только админы)\n"
        "/remove_email <email> — удалить email из списка (в группах — только админы)\n"
        "/clear_emails — очистить весь список email (в группах — только админы)\n"
        "/emails — показать список email\n"
        "/events — ближайшие встречи: изменить или удалить\n"
        "/menu и /help — это меню\n"
        "/cancel — отменить подтверждение события или редактирование\n"
        "\n"
        "Вы также можете отправить текстовое сообщение или фотографию с текстом события — бот распознает параметры и отправит приглашение на email.\n"
        "\n"
        "Изменить или удалить встречу можно кнопками под отчётом о рассылке или через /events. "
        "Участники получат обновление или отмену, и встреча изменится в их календаре. "
        "В группах это могут делать автор встречи и админы чата. "
        "Встречи, отправленные до появления этой функции, изменить нельзя."
    )
    await update.message.reply_text(menu_text)


async def help_in_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await menu_command(update, context)
    return None


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        'Операция отменена.', reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

def build_application(builder=None) -> Application:
    application = (builder or Application.builder().token(config.TELEGRAM_TOKEN)).build()
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),
            MessageHandler(filters.PHOTO, handle_photo),
            CallbackQueryHandler(edit_event_entry, pattern=r'^ev:edit:\d+$'),
        ],
        states={
            EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_email)],
            CONFIRM_EVENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_event),
                CallbackQueryHandler(confirm_event_callback, pattern='^event_'),
            ],
            CHOOSE_FIELD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_field),
                CallbackQueryHandler(edit_field_callback, pattern='^field:'),
            ],
            EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_field)],
            CONFIRM_UPDATE: [CallbackQueryHandler(update_event_callback, pattern='^upd:')],
        },
        fallbacks=[
            CommandHandler('cancel', cancel),
            CommandHandler('help', help_in_conversation),
            CommandHandler('menu', help_in_conversation),
            # «Изменить» у другой встречи посреди диалога начинает её редактирование
            CallbackQueryHandler(edit_event_entry, pattern=r'^ev:edit:\d+$'),
        ],
    )
    application.add_handler(conv_handler)
    application.add_handler(
        CallbackQueryHandler(events_callback, pattern=r'^ev:(list|open|del|delok|nodel):\d+$')
    )
    application.add_handler(CommandHandler('events', events_command))
    application.add_handler(CommandHandler('help', menu_command))
    application.add_handler(CommandHandler('emails', emails_command))
    application.add_handler(CommandHandler('clear_emails', clear_emails_command))
    application.add_handler(CommandHandler('remove_email', remove_email_command))
    application.add_handler(CommandHandler('add_email', add_email_command))
    application.add_handler(CommandHandler('menu', menu_command))
    return application


def main():
    application = build_application()
    # Иначе inline-кнопки не получают callback_query (см. PTB: allowed_updates).
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main() 