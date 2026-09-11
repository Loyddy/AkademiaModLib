"""Standalone UTM study-room feature. Persistent data lives in ../config."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import reduce
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from math import gcd
from pathlib import Path
from urllib.parse import urljoin, urlencode, urlsplit, urlunsplit
from urllib.request import build_opener, HTTPCookieProcessor, HTTPRedirectHandler, Request
import json
import re
import threading

from PySide6.QtCore import Qt, Signal, QDate, QEasingCurve, QAbstractAnimation, QRectF
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView, QStyledItemDelegate,
    QCalendarWidget, QFrame, QCheckBox, QScrollArea, QLineEdit,
)
from ui_widgets import StyledComboBox
from utils import PropertyAnimation, VariantAnimation
from utils import write_json

# Read HTML table structure as data, preserving nested tables and spans.


class TableReader(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.stack = []
        self.skip = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'): self.skip += 1
        if self.skip: return
        attrs = dict(attrs)
        if tag == 'table':
            table = {'rows': [], 'row': None, 'cell': None, 'attrs': attrs}
            self.tables.append(table); self.stack.append(table)
        if not self.stack: return
        table = self.stack[-1]
        if tag == 'tr': table['row'] = []
        elif tag in ('th', 'td') and table['row'] is not None:
            table['cell'] = {'text': '', 'attrs': attrs, 'header': tag == 'th'}
        elif tag == 'br' and table['cell'] is not None: table['cell']['text'] += ' '

    def handle_data(self, data):
        if not self.skip and self.stack and self.stack[-1]['cell'] is not None:
            self.stack[-1]['cell']['text'] += data

    def handle_endtag(self, tag):
        if tag in ('script', 'style'): self.skip = max(0, self.skip - 1)
        if self.skip or not self.stack: return
        table = self.stack[-1]
        if tag in ('th', 'td') and table['cell'] is not None:
            table['cell']['text'] = ' '.join(table['cell']['text'].split())
            table['row'].append(table['cell']); table['cell'] = None
        elif tag == 'tr' and table['row'] is not None:
            if table['row']: table['rows'].append(table['row'])
            table['row'] = None
        elif tag == 'table': self.stack.pop()


def expanded_rows(table):
    """Expand row/column spans; absent cells stay absent, never become free."""
    occupied = {}
    result = []
    for r, row in enumerate(table['rows'][:500]):
        c = 0
        for cell in row:
            while (r, c) in occupied: c += 1
            try:
                height = max(1, min(100, int(cell['attrs'].get('rowspan', '1'))))
                width = max(1, min(100, int(cell['attrs'].get('colspan', '1'))))
            except ValueError: height = width = 1
            for rr in range(r, r + height):
                for cc in range(c, c + width): occupied[(rr, cc)] = cell
            c += width
        columns = [cc for rr, cc in occupied if rr == r]
        result.append([occupied.get((r, cc)) for cc in range(max(columns, default=-1) + 1)])
    return result


# Reservation records from the school's room-by-time availability matrix.


@dataclass(frozen=True)
class Reservation:
    day: date
    room: str
    start: int
    end: int
    status: str
    detail: str


@dataclass(frozen=True)
class SlotSelection:
    day: date
    room: str
    start: int
    end: int


def minute(value):
    match = re.fullmatch(r'(\d{1,2}):(\d{2})\s*(AM|PM)?', value.strip().upper())
    if not match: raise ValueError('Invalid time')
    h, m = int(match[1]), int(match[2])
    if m > 59 or (match[3] and not 1 <= h <= 12) or (not match[3] and h > 23):
        raise ValueError('Invalid time')
    if match[3]: h = h % 12 + (12 if match[3] == 'PM' else 0)
    return h * 60 + m


def clock_text(value):
    return '次日 00:00' if value == 1440 else f'{value // 60:02d}:{value % 60:02d}'


def availability_date(document):
    text = ' '.join(document.text)
    match = re.search(r'Availability\s+for\s*:\s*(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})', text, re.I)
    if not match: return None
    try:
        return datetime.strptime(match[1], '%Y-%m-%d' if '-' in match[1] else '%m/%d/%Y').date()
    except ValueError: return None


def matrix_records(document):
    day = availability_date(document)
    if day is None: return []
    result = []
    for table in getattr(document, 'tables', []):
        columns = None
        for row in expanded_rows(table):
            if not row or row[0] is None: continue
            texts = [cell['text'] if cell else '' for cell in row]
            if texts[0].strip().casefold() == 'time' and len(texts) > 1 and all(texts[1:]):
                columns = texts[1:]; continue
            if columns is None: continue
            match = re.fullmatch(r'(\d{1,2}:\d{2}\s*(?:AM|PM)?)\s*[-–—]\s*(\d{1,2}:\d{2}\s*(?:AM|PM)?)', texts[0], re.I)
            if not match: continue
            try: start, end = minute(match[1]), minute(match[2])
            except ValueError: continue
            if end == 0 and start > 0: end = 1440
            if end <= start: continue
            for column, room in enumerate(columns, 1):
                if column >= len(row) or row[column] is None: continue
                cell = row[column]; value = cell['text']
                attrs = cell['attrs']
                flags = (attrs.get('class', '') + ' ' + attrs.get('aria-disabled', '')).lower()
                # The verified availability matrix uses empty cells for free slots.
                # Missing cells, other tables, and dates without a heading do not qualify.
                status = '已预约' if value else '可预约'
                if any(flag in flags.split() for flag in ('disabled', 'unavailable', 'closed', 'true')):
                    status = '不可预约'
                result.append(Reservation(day, room, start, end, status, value or status))
    return result


def reservations_from_document(document):
    matrix = matrix_records(document)
    if matrix: return matrix
    aliases = {'date': ('date', '日期'), 'room': ('room', 'resource', '房间', '学习室'),
               'start': ('start', 'start time', '开始时间'), 'end': ('end', 'end time', '结束时间'),
               'status': ('status', '状态')}
    records = []
    columns = None
    for row in document.rows:
        normalized = [cell.strip().casefold() for cell in row]
        found = {key: next((i for i, cell in enumerate(normalized) if cell in names), -1)
                 for key, names in aliases.items()}
        if all(i >= 0 for i in found.values()): columns = found; continue
        if columns is None or max(columns.values()) >= len(row): continue
        try:
            day = date.fromisoformat(row[columns['date']].strip())
            start, end = minute(row[columns['start']]), minute(row[columns['end']])
            room = row[columns['room']].strip()
            if not room or end <= start: continue
            raw = row[columns['status']].strip()
            status = {'available': '可预约', 'free': '可预约', '可预约': '可预约',
                      'booked': '已预约', 'reserved': '已预约', '已预约': '已预约',
                      'unavailable': '不可预约', 'closed': '不可预约', '不可预约': '不可预约'}.get(raw.casefold(), '待确认')
            records.append(Reservation(day, room, start, end, status, ' · '.join(row)))
        except ValueError: continue
    return records


# Memory-only HTTP session; credential persistence is handled separately.


BOOKING_URL = 'https://app.utm.utoronto.ca/rbs/library/BookAResource.action'
SCHOOL_NAME = 'University of Toronto Mississauga（多伦多大学密西沙加校区）'
LOCATION_NAMES = ('Library Study Rooms', 'Instructional Building Group Study Rooms',
                  'Deerfield Hall', 'Maanjiwe nendamowinan')


def allowed_url(url):
    try:
        p = urlsplit(url)
        return p.scheme == 'https' and p.hostname in {'app.utm.utoronto.ca', 'appauth.utm.utoronto.ca'} and p.port in (None, 443) and not p.username and not p.password
    except (ValueError, TypeError):
        return False


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_url(newurl):
            raise ValueError('学校要求跳转至尚未适配的认证服务。')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.forms, self.rows, self.links = [], [], []
        self.text = []
        self.form = self.field = self.option = self.row = self.cell = self.link = None
        self.skip = 0
        self.feed(html)
        self.tables = TableReader(html).tables
        self.rows = [[cell['text'] if cell else '' for cell in row]
                     for table in self.tables for row in expanded_rows(table)]

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ('script', 'style'):
            self.skip += 1
        if self.skip:
            return
        if tag == 'form':
            self.form = dict(action=a.get('action', ''), method=a.get('method', 'get').lower(), fields=[])
            self.forms.append(self.form)
        if tag in ('input', 'select', 'textarea', 'button') and self.form is not None and a.get('name') and 'disabled' not in a:
            self.field = dict(name=a['name'], type=a.get('type', 'submit' if tag == 'button' else tag), value=a.get('value', ''), label=a.get('aria-label') or a.get('placeholder') or a['name'], options=[], checked='checked' in a)
            self.form['fields'].append(self.field)
        if tag == 'option' and self.field is not None:
            self.option = [a.get('value'), '', 'selected' in a]
        if tag == 'tr': self.row = []
        if tag in ('th', 'td') and self.row is not None: self.cell = ''
        if tag == 'a' and a.get('href'): self.link = [a['href'], '']

    def handle_data(self, data):
        if self.skip: return
        if data.strip(): self.text.append(data.strip())
        if self.cell is not None: self.cell += data
        if self.option is not None: self.option[1] += data
        if self.link is not None: self.link[1] += data

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.skip = max(0, self.skip - 1)
        if self.skip: return
        if tag == 'form': self.form = None
        if tag == 'option' and self.option is not None:
            value, label, selected = self.option
            value = label.strip() if value is None else value
            self.field['options'].append((label.strip(), value))
            if selected or len(self.field['options']) == 1: self.field['value'] = value
            self.option = None
        if tag in ('th', 'td') and self.cell is not None:
            self.row.append(' '.join(self.cell.split())); self.cell = None
        if tag == 'tr' and self.row is not None:
            if self.row: self.rows.append(self.row)
            self.row = None
        if tag == 'a' and self.link is not None:
            self.links.append((self.link[0], ' '.join(self.link[1].split())))
            self.link = None


class LibrarySession:
    def __init__(self):
        self.cookies = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies), SafeRedirect())
        self.url = BOOKING_URL
        self.resource_type = None
        self.locations = [(LOCATION_NAMES[0], BOOKING_URL)]
        self.entry_url = BOOKING_URL

    def fetch(self, url, values=None, method='get'):
        if not allowed_url(url): raise ValueError('此服务尚未适配。')
        data = None
        if values is not None:
            encoded = urlencode(values)
            if method == 'post': data = encoded.encode()
            else: url += ('&' if '?' in url else '?') + encoded
        with self.opener.open(Request(url, data=data), timeout=25) as response:
            self.url = response.url
            document = Document(response.read(2_000_000).decode('utf-8', errors='replace'))
            document.source_url = self.url
            for form in document.forms:
                form['source_url'] = self.url
            self.document = document
            return document

    def submit(self, form, values):
        target = urljoin(form.get('source_url', self.url), form['action'])
        # CAS's relative action omits the service query; keep the entry's service.
        if urlsplit(target).path == urlsplit(self.url).path and not urlsplit(target).query:
            target = self.url
        return self.fetch(target, values, form['method'])

    def load_day(self, day):

        form = getattr(self, 'calendar_form', None)
        if form is None:
            entry = self.fetch(self.entry_url)
            self.capture_entry(entry)
            form = self.calendar_form
        values = self.entry_values(form)
        values = [(name, day.strftime('%m/%d/%Y') if name == 'searchDate' else value) for name, value in values]
        result = self.submit(form, values)
        if availability_date(result) != day:
            raise ValueError('学校未返回所选日期的预约表，会话可能已过期，请重新登录。')
        return result

    def capture_entry(self, document):
        self.discover_locations(document)
        def find(action):
            return next((f for f in document.forms if urlsplit(f['action']).path.rsplit('/', 1)[-1] == action), None)
        self.calendar_form = find('ShowResourceTableViewByDay.action')
        self.booking_form = find('MakeABooking.action')
        if self.calendar_form is None or self.booking_form is None:
            raise ValueError('无法识别学校预约入口，请重新登录。')
        for form in (self.calendar_form, self.booking_form):
            form['source_url'] = getattr(document, 'source_url', self.url)
        field = next((f for f in self.calendar_form['fields'] if f['name'] == 'resourceTypeId'), None)
        self.resource_type = field['value'] if field else None

    def discover_locations(self, document):
        locations = dict(self.locations)
        names = {name.casefold(): name for name in LOCATION_NAMES}
        for href, label in document.links:
            name = names.get(' '.join(label.split()).casefold())
            if name is None: continue
            target = urljoin(getattr(document, 'source_url', self.url), href)
            target = urlunsplit(urlsplit(target)._replace(fragment=''))
            if (allowed_url(target) and urlsplit(target).path.startswith('/rbs/')
                    and urlsplit(target).path.endswith('/BookAResource.action')):
                locations[name] = target
        self.locations = [(name, locations[name]) for name in LOCATION_NAMES if name in locations]

    def select_location(self, url, day):
        if url not in [target for _, target in self.locations]:
            raise ValueError('学校未提供该预约入口。')
        previous = (self.entry_url, self.calendar_form, self.booking_form, self.resource_type)
        try:
            self.capture_entry(self.fetch(url))
            self.entry_url = url
            return self.load_day(day)
        except Exception:
            self.entry_url, self.calendar_form, self.booking_form, self.resource_type = previous
            raise

    def entry_values(self, form):
        return [(f['name'], self.resource_type if f['name'] == 'resourceTypeId' and self.resource_type is not None else f['value'])
                for f in form['fields'] if f.get('type') not in ('submit', 'button', 'reset')]

    def prepare_booking(self, slot, title, description=''):

        if not title.strip(): raise ValueError('请输入预约标题。')
        if not 0 <= slot.start < slot.end <= 1440: raise ValueError('预约时间范围无效。')
        current = self.load_day(slot.day)
        relevant = [r for r in reservations_from_document(current) if r.room == slot.room and r.start < slot.end and slot.start < r.end]
        if any(r.status != '可预约' for r in relevant): raise ValueError('该时段已被占用，请刷新后重新选择。')
        covered = slot.start
        for record in sorted(relevant, key=lambda r:r.start):
            if record.start > covered: break
            covered = max(covered, record.end)
        if covered < slot.end: raise ValueError('无法确认所选时段全部可用，未提交预约。')
        entry = self.booking_form
        form_doc = self.submit(entry, self.entry_values(entry))
        form = next((f for f in form_doc.forms if urlsplit(f['action']).path.rsplit('/', 1)[-1] == 'SaveBooking.action'), None)
        if form is None: raise ValueError('学校未返回预约提交表单，请重新登录。')
        form['source_url'] = getattr(form_doc, 'source_url', self.url)
        fields = {f['name']:f for f in form['fields']}
        matches = [value for label,value in fields.get('resourceId',{}).get('options',[]) if ' '.join(label.split()).casefold() == ' '.join(slot.room.split()).casefold()]
        resource = matches[0] if len(matches) == 1 else None
        if resource is None or resource == '0': raise ValueError('学校未返回所选房间编号，未提交预约。')
        h,m = divmod(slot.start,60)
        values = {f['name']:f['value'] for f in form['fields'] if f['type'] == 'hidden'}
        values.update({'resourceId':resource, 'resourceNm':slot.room,
            'booking.bookingTitle':title.strip(), 'booking.bookingDesc':description,
            'bookingStartDt':slot.day.strftime('%m/%d/%Y'), 'startTime':f'{h%12 or 12}:{m:02d}',
            'startAmPm':'1' if h >= 12 else '0', 'booking.duration':str(slot.end-slot.start)})
        for name in ('resourceId','startTime','startAmPm','booking.duration'):
            if values[name] not in [v for label,v in fields.get(name,{}).get('options',[])]:
                raise ValueError('学校不支持所选起始时间或时长，未提交预约。')
        return form, list(values.items())

    def book_slot(self, slot, title, description=''):
        form, values = self.prepare_booking(slot, title, description)
        # Never retry a reservation POST: a timeout may occur after it was saved.
        try:
            receipt = self.submit(form, values)
        except Exception:
            raise ValueError('提交结果不确定，请先在学校“View My Bookings”核实，勿立即重复提交。') from None
        try:
            calendar = self.load_day(slot.day)
        except Exception:
            calendar = None
        return {'receipt': receipt, 'calendar': calendar}

    def login(self, username, password):
        doc = self.fetch(BOOKING_URL)
        form = next((f for f in doc.forms if any(x['type'] == 'password' for x in f['fields'])), None)
        if form is None: raise ValueError('无法识别学校登录表单，请稍后重试。')
        values = [(f['name'], f['value']) for f in form['fields'] if f['type'] == 'hidden']
        values += [('username', username), ('password', password)]
        doc = self.submit(form, values)
        if any(f['type'] == 'password' for form in doc.forms for f in form['fields']):
            self.cookies.clear()
            raise ValueError('登录未完成，请检查账号密码；额外身份验证尚未适配。')
        if urlsplit(self.url).hostname != 'app.utm.utoronto.ca':
            raise ValueError('学校要求额外身份验证，目前尚未适配。')
        # CAS may land on the application home instead of the booking calendar.
        self.discover_locations(doc)
        entry = self.fetch(BOOKING_URL)
        self.capture_entry(entry)
        value = next(f['value'] for f in self.calendar_form['fields'] if f['name'] == 'searchDate')
        return self.load_day(datetime.strptime(value, '%m/%d/%Y').date())


# User-requested plaintext library credentials, stored atomically in config.

CREDENTIALS_FILE = Path(__file__).resolve().parent.parent / 'config' / 'library_credentials.json'


def load_credentials(path=None):
    try:
        data = json.loads(Path(path or CREDENTIALS_FILE).read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('school') != 'utm': return '', ''
        username, password = data.get('username'), data.get('password')
        return (username, password) if isinstance(username, str) and isinstance(password, str) else ('', '')
    except (OSError, ValueError):
        return '', ''


def save_credentials(username, password, path=None):
    write_json(path or CREDENTIALS_FILE, dict(school='utm', username=username, password=password))


# Native room/time booking matrix with calendar and animated interaction.


COLORS = {'可预约': ('#f5fbf8', '#388466'), '已预约': ('#eee8f7', '#7962aa'),
          '不可预约': ('#f6ecef', '#a76579'), '待确认': ('#f4f3f6', '#918998')}


class MatrixDelegate(QStyledItemDelegate):
    @staticmethod
    def _wrap(text, metrics, width, height):
        """按词换行，放不下时省略结尾，避免文字被硬裁切。"""
        if width <= 0 or height <= 0:
            return str(text)
        lines, current = [], ""
        for word in str(text).replace("\r", " ").replace("\n", " ").split():
            pieces = []
            # 单个词过长时按字符拆成放得下的片段。
            while metrics.horizontalAdvance(word) > width:
                piece, index = "", 1
                while index <= len(word) and metrics.horizontalAdvance(word[:index]) <= width:
                    piece, index = word[:index], index + 1
                if not piece:
                    break
                pieces.append(piece); word = word[len(piece):]
            pieces.append(word)
            for piece in pieces:
                candidate = f"{current} {piece}".strip()
                if current and metrics.horizontalAdvance(candidate) > width:
                    lines.append(current); current = piece
                else:
                    current = candidate
        if current:
            lines.append(current)
        limit = max(1, int(height // max(1, metrics.lineSpacing())))
        if len(lines) > limit:
            lines = lines[:limit]
            last = lines[-1]
            while last and metrics.horizontalAdvance(last + "…") > width:
                last = last[:-1]
            lines[-1] = last + "…"
        return "\n".join(lines)

    def paint(self, painter, option, index):
        table = self.parent()
        data = index.data(Qt.ItemDataRole.UserRole) or {}
        status = data.get('status', '待确认')
        bg, fg = COLORS[status]
        rect = QRectF(option.rect).adjusted(3, 3, -3, -3)
        selected = index.row() in table.selected_rows and index.column() == table.selected_column
        hovered = (index.row(), index.column()) == table.hovered
        painter.save(); painter.setRenderHint(painter.RenderHint.Antialiasing)
        painter.setOpacity(table.reveal)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#e2d6f5' if selected else bg))
        painter.drawRoundedRect(rect, 7, 7)
        if selected or hovered:
            color = QColor('#9271bc' if selected else fg)
            color.setAlphaF(1.0 if selected else table.hover_progress * .65)
            painter.setPen(QPen(color, 1.5)); painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(.7, .7, -.7, -.7), 7, 7)
        painter.setPen(QColor(fg))
        text = data.get('text', '未加载')
        if status == '可预约': text = '✓ 已选' if selected else '+ 预约' if hovered else '空闲'
        painter.setClipRect(rect.adjusted(7, 5, -7, -5))
        text_rect = rect.adjusted(8, 7, -8, -5)
        wrapped = self._wrap(text, painter.fontMetrics(), text_rect.width(), text_rect.height())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, wrapped)
        painter.restore()


class ReservationMatrix(QTableWidget):
    slotRequested = Signal(object)
    recordSelected = Signal(object)

    def __init__(self):
        super().__init__()
        self.setObjectName('reservationMatrix')
        self.setMouseTracking(True)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setShowGrid(False)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.horizontalHeader().setMinimumSectionSize(1)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.horizontalHeader().setFixedHeight(76)
        self.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        self.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.verticalHeader().setDefaultSectionSize(68)
        self.verticalHeader().setFixedWidth(88)
        self.setCornerButtonEnabled(False)
        self.setItemDelegate(MatrixDelegate(self))
        self.setMinimumHeight(330)
        self.setAccessibleName('图书馆房间与时间预约矩阵')
        self.setStyleSheet('''
            QTableWidget {background:white; border:1px solid #e0d7f7; border-radius:12px;}
            QHeaderView::section {background:#f3eff8; color:#7962aa; border:0; padding:6px; font-weight:600;}
            QTableCornerButton::section {background:#f3eff8; border:0;}
        ''')
        self.hovered = (-1, -1)
        self.hover_progress = 1.0
        self.reveal = 1.0
        self.selected_rows = []
        self.selected_column = -1
        self.anchor = None
        self.duration = 60
        self.step = 60
        self.start = 480
        self.columns = []
        self.day = date.today()
        self.week = False
        self.room = None
        self.records = []
        self.hover_animation = VariantAnimation(self)
        self.hover_animation.setDuration(110)
        self.hover_animation.setStartValue(0.0); self.hover_animation.setEndValue(1.0)
        self.hover_animation.valueChanged.connect(self._hover_frame)
        self.reveal_animation = VariantAnimation(self)
        self.reveal_animation.setDuration(160)
        self.reveal_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.reveal_animation.setStartValue(.35); self.reveal_animation.setEndValue(1.0)
        self.reveal_animation.valueChanged.connect(self._reveal_frame)
        self.scroll_animations = {}
        for name, bar in [('x', self.horizontalScrollBar()), ('y', self.verticalScrollBar())]:
            animation = PropertyAnimation(bar, b'value', self)
            animation.setDuration(180); animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            bar.sliderPressed.connect(animation.stop)
            self.scroll_animations[name] = animation

    def _hover_frame(self, value):
        self.hover_progress = value
        self.viewport().update()

    def _reveal_frame(self, value):
        self.reveal = value
        self.viewport().update()

    def clear_selection(self):
        self.anchor = None; self.selected_rows = []; self.selected_column = -1
        self.viewport().update()

    def set_records(self, records, day, week=False, room=None, known_rooms=()):
        self.clear_selection()
        self.hover_animation.stop(); self.hovered = (-1, -1)
        for animation in self.scroll_animations.values(): animation.stop()
        self.records, self.day, self.week, self.room = records, day, week, room
        self.step = max(15, reduce(gcd, [r.end - r.start for r in records] + [60]))
        self.start = min((r.start for r in records), default=480) // self.step * self.step
        end = max((r.end for r in records), default=1440)
        monday = day - timedelta(days=day.weekday())
        self.columns = [monday + timedelta(days=i) for i in range(7)] if week else list(known_rooms) or sorted({r.room for r in records})
        headers = [f'周{"一二三四五六日"[d.weekday()]}\n{d:%m/%d}' for d in self.columns] if week else [r.replace('Library Study Room ', 'Room\n').replace('Instructional Building Group Study Room ', 'IB\n') for r in self.columns]
        self.setRowCount(max(0, (end - self.start + self.step - 1) // self.step))
        self.setColumnCount(len(self.columns) or 1)
        self.setHorizontalHeaderLabels(headers or ['等待学校数据'])
        for column, key in enumerate(self.columns):
            self.horizontalHeaderItem(column).setToolTip(str(key))
        self.setVerticalHeaderLabels([clock_text(self.start + i * self.step) + '\n— ' + clock_text(min(end, self.start + (i + 1) * self.step)) for i in range(self.rowCount())])
        for row in range(self.rowCount()):
            start = self.start + row * self.step
            for column in range(self.columnCount()):
                key = self.columns[column] if self.columns else None
                relevant = [r for r in records if (r.day == key and r.room == room if week else r.room == key)
                            and r.start < start + self.step and start < r.end]
                status = '待确认'
                if relevant:
                    status = next((r.status for r in relevant if r.status != '可预约'), '可预约')
                    if status == '可预约' and not any(r.start <= start and r.end >= start + self.step for r in relevant): status = '待确认'
                text = '\n'.join(dict.fromkeys(r.detail for r in relevant)) if relevant else '未加载'
                item = QTableWidgetItem(status + (' · ' + text if text else ''))
                item.setData(Qt.ItemDataRole.UserRole, dict(status=status, text=text, records=relevant))
                item.setToolTip(f'{key or "等待学校数据"}\n{clock_text(start)}–{clock_text(start + self.step)}\n{text}')
                self.setItem(row, column, item)
        self.reveal_animation.stop()
        if self.isVisible(): self.reveal_animation.start()
        else: self.reveal = 1.0

    def selection_slot(self):
        if not self.selected_rows or not self.columns: return None
        key = self.columns[self.selected_column]
        room = self.room if self.week else key
        if not room: return None
        return SlotSelection(key if self.week else self.day, room,
                             self.start + min(self.selected_rows) * self.step,
                             self.start + (max(self.selected_rows) + 1) * self.step)

    def mousePressEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        if event.button() != Qt.MouseButton.LeftButton or not index.isValid():
            return super().mousePressEvent(event)
        self.clear_selection()
        data = index.data(Qt.ItemDataRole.UserRole) or {}
        if data.get('status') != '可预约':
            for record in data.get('records', [])[:1]: self.recordSelected.emit(record)
            return
        self.setCurrentIndex(index)
        self.anchor = (index.row(), index.column())
        self.selected_column = index.column()
        count = max(1, (self.duration + self.step - 1) // self.step)
        self.selected_rows = list(range(index.row(), index.row() + count))
        self.viewport().update()
        event.accept()

    def mouseMoveEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        hovered = (index.row(), index.column()) if index.isValid() else (-1, -1)
        if hovered != self.hovered:
            self.hovered = hovered; self.hover_animation.stop(); self.hover_animation.start()
        if self.anchor is not None and index.isValid() and event.buttons() & Qt.MouseButton.LeftButton:
            row, column = self.anchor
            self.selected_column = column
            self.selected_rows = list(range(min(row, index.row()), max(row, index.row()) + 1))
            self.viewport().update()
        data = index.data(Qt.ItemDataRole.UserRole) or {}
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if data.get('status') == '可预约' else Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.anchor is not None:
            self.anchor = None
            slot = self.selection_slot()
            if slot is not None: self.slotRequested.emit(slot)
            event.accept(); return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):
        self.hover_animation.stop(); self.hovered = (-1, -1); self.viewport().update()
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape: self.clear_selection(); return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            index = self.currentIndex()
            data = index.data(Qt.ItemDataRole.UserRole) or {}
            if index.isValid() and data.get('status') == '可预约':
                self.selected_column = index.column()
                count = max(1, (self.duration + self.step - 1) // self.step)
                self.selected_rows = list(range(index.row(), index.row() + count))
                slot = self.selection_slot()
                if slot is not None: self.slotRequested.emit(slot)
                self.viewport().update()
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event):
        horizontal = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier) or abs(event.angleDelta().x()) > abs(event.angleDelta().y())
        bar = self.horizontalScrollBar() if horizontal else self.verticalScrollBar()
        animation = self.scroll_animations['x' if horizontal else 'y']
        pixel = event.pixelDelta().x() if horizontal and event.pixelDelta().x() else event.pixelDelta().y()
        delta = event.angleDelta().x() if horizontal and event.angleDelta().x() else event.angleDelta().y()
        if pixel:
            animation.stop(); bar.setValue(bar.value() - pixel)
        else:
            base = bar.value()
            if animation.state() != QAbstractAnimation.State.Stopped and (int(animation.endValue()) - base) * delta < 0: base = int(animation.endValue())
            animation.stop(); animation.setStartValue(bar.value())
            animation.setEndValue(max(bar.minimum(), min(bar.maximum(), round(base - delta / 120 * 90))))
            animation.start()
        event.accept()

    def hideEvent(self, event):
        self.hover_animation.stop(); self.reveal_animation.stop(); self.reveal = 1.0
        for animation in self.scroll_animations.values(): animation.stop()
        self.clear_selection()
        super().hideEvent(event)


class ReservationSchedule(QWidget):
    bookingRequested = Signal(object)
    dateRequested = Signal(object)

    def __init__(self, defer_build=False):
        super().__init__()
        if not defer_build:
            for _ in self.build_steps():
                pass

    def build_steps(self):
        self.records = []
        self.pending_slot = None
        self.setObjectName('librarySchedule')
        self.setStyleSheet('''
            /* 日历侧栏与 #contentPanel 使用同一套面板样式，标签沿用全局角色。 */
            #librarySchedule QFrame#bookingSidebar {background:rgba(255,255,255,0.82);border:1px solid #e0d7f7;border-radius:24px;}
            #librarySchedule QLabel#matrixTitle {font-size:18px;font-weight:600;color:#594178;}
            #librarySchedule QCalendarWidget {background:white;}
            #librarySchedule QCalendarWidget QToolButton {color:#7962aa;background:transparent;border:0;padding:3px;}
            #librarySchedule QCalendarWidget QWidget#qt_calendar_navigationbar {background:#e8e0f8;}
            #librarySchedule QCalendarWidget QAbstractItemView {background:white;selection-background-color:#9b87ca;selection-color:white;outline:0;font-size:11px;}
        ''')
        layout = QHBoxLayout(self); layout.setContentsMargins(0,0,0,0); layout.setSpacing(16)
        sidebar = QFrame(); sidebar.setObjectName('bookingSidebar')
        left = QVBoxLayout(sidebar); left.setContentsMargins(12,16,12,16); left.setSpacing(12)
        title = QLabel('选择日期'); title.setObjectName('sectionTitle'); left.addWidget(title)
        yield "正在构建图书馆预约 · 日历…"
        self.calendar = QCalendarWidget(); self.calendar.setGridVisible(False)
        self.calendar.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.calendar.setHorizontalHeaderFormat(QCalendarWidget.HorizontalHeaderFormat.SingleLetterDayNames)
        # 侧栏宽度有限，日历保持可收缩，避免右列被裁掉。
        self.calendar.setMinimumSize(180, 210); self.calendar.setMaximumHeight(230)
        left.addWidget(self.calendar)
        today = QPushButton('回到今天'); today.setObjectName('primaryButton'); today.clicked.connect(lambda: self.calendar.setSelectedDate(QDate.currentDate())); left.addWidget(today)
        room_label = QLabel('房间'); room_label.setObjectName('sectionTitle'); left.addWidget(room_label)
        self.room = StyledComboBox(); self.room.addItem('全部房间', None); left.addWidget(self.room)
        duration_hint = QLabel('单击预约时长 · 拖动可选择连续时段'); duration_hint.setObjectName('muted'); duration_hint.setWordWrap(True)
        left.addWidget(duration_hint)
        self.duration = StyledComboBox()
        for minutes in (60,120): self.duration.addItem(f'{minutes // 60} 小时', minutes)
        left.addWidget(self.duration)
        title_label = QLabel('预约标题（学校公开显示）'); title_label.setObjectName('sectionTitle'); left.addWidget(title_label)
        self.booking_title = QLineEdit('Study'); left.addWidget(self.booking_title)
        self.auto_submit = QCheckBox('选中后直接提交'); self.auto_submit.setChecked(True); left.addWidget(self.auto_submit)
        self.detail = QLabel('单击空闲格预约；沿同一列拖动选择连续时段。'); self.detail.setObjectName('muted'); self.detail.setWordWrap(True); self.detail.setTextFormat(Qt.TextFormat.PlainText); left.addWidget(self.detail)
        self.submit = QPushButton('预约所选时段'); self.submit.setObjectName('primaryButton'); self.submit.setEnabled(False); self.submit.clicked.connect(self.submit_pending); left.addWidget(self.submit)
        left.addStretch()
        hint = QLabel('绿色：空闲  ·  紫色：已预约\n灰色：尚未加载\n房间列自动适应窗口宽度'); hint.setObjectName('hint'); hint.setWordWrap(True); left.addWidget(hint)
        sidebar_scroll = QScrollArea(); sidebar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        sidebar_scroll.setWidgetResizable(True); sidebar_scroll.setFixedWidth(230)
        sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sidebar_scroll.setWidget(sidebar); layout.addWidget(sidebar_scroll)
        right = QVBoxLayout(); right.setSpacing(10)
        row = QHBoxLayout()
        title = QLabel('校园学习室'); title.setObjectName('matrixTitle'); row.addWidget(title); row.addStretch()
        self.mode = StyledComboBox(); self.mode.addItems(['房间日视图','单个房间周视图']); row.addWidget(self.mode)
        right.addLayout(row)
        self.summary = QLabel(); self.summary.setObjectName('scheduleSummary'); self.summary.setWordWrap(True); right.addWidget(self.summary)
        yield "正在构建图书馆预约 · 预约表格…"
        self.canvas = ReservationMatrix(); right.addWidget(self.canvas, 1)
        self.scroll = self.canvas
        layout.addLayout(right, 1)
        self.canvas.slotRequested.connect(self.request_slot)
        self.canvas.recordSelected.connect(self.select)
        self.calendar.selectionChanged.connect(self.change_date)
        self.room.currentIndexChanged.connect(self.refresh)
        self.mode.currentIndexChanged.connect(self.refresh)
        self.duration.currentIndexChanged.connect(self.refresh)
        yield "正在准备图书馆预约 · 空闲时段…"
        self.refresh()

    def change_date(self):
        self.refresh()
        self.dateRequested.emit(self.calendar.selectedDate().toPython())

    def set_records(self, records):
        self.records = records
        selected = self.room.currentData()
        self.room.blockSignals(True); self.room.clear(); self.room.addItem('全部房间', None)
        for room in sorted({r.room for r in records}): self.room.addItem(room, room)
        index = self.room.findData(selected)
        if index >= 0: self.room.setCurrentIndex(index)
        self.room.blockSignals(False)
        if records and not any(r.day == self.calendar.selectedDate().toPython() for r in records):
            self.calendar.blockSignals(True); self.calendar.setSelectedDate(QDate(min(r.day for r in records))); self.calendar.blockSignals(False)
        self.refresh()

    def refresh(self, *_):
        self.pending_slot = None; self.submit.setEnabled(False)
        day = self.calendar.selectedDate().toPython(); week = self.mode.currentIndex() == 1
        start = day - timedelta(days=day.weekday()) if week else day
        end = start + timedelta(days=7 if week else 1)
        room = self.room.currentData()
        if week and room is None and self.room.count() > 1:
            self.room.blockSignals(True); self.room.setCurrentIndex(1); self.room.blockSignals(False); room = self.room.currentData()
        records = [r for r in self.records if start <= r.day < end and (room is None or r.room == room)]
        rooms = [room] if room else sorted({r.room for r in self.records})
        self.canvas.duration = self.duration.currentData()
        self.canvas.set_records(records, day, week, room, rooms)
        self.summary.setText(f'{start:%Y-%m-%d}' + (f' — {end-timedelta(days=1):%Y-%m-%d}' if week else '') + f'  ·  {len(rooms)} 个房间  ·  {sum(r.status == "可预约" for r in records)} 个空闲时段')
        self.detail.setText('单击空闲格预约；沿同一列拖动选择连续时段。' if records else '当前日期尚未加载数据。')

    def select(self, record):
        self.pending_slot = None; self.submit.setEnabled(False)
        self.detail.setText(f'{record.room}\n{record.day:%Y-%m-%d}\n{clock_text(record.start)}–{clock_text(record.end)} · {record.status}\n{record.detail}')

    def request_slot(self, slot):
        self.pending_slot = None; self.submit.setEnabled(False)
        matching = [r for r in self.records if r.day == slot.day and r.room == slot.room and r.start < slot.end and slot.start < r.end]
        if any(r.status != '可预约' for r in matching):
            self.canvas.clear_selection(); self.detail.setText('所选范围包含不可用时段，请重新选择。'); return
        covered = slot.start
        for record in sorted(matching, key=lambda r:r.start):
            if record.start > covered: break
            covered = max(covered, record.end)
        if covered < slot.end:
            self.canvas.clear_selection(); self.detail.setText('所选范围尚未获取完整可用情况，请先加载该日期。'); return
        self.pending_slot = slot; self.submit.setEnabled(True)
        self.detail.setText(f'{slot.room}\n{slot.day}\n{clock_text(slot.start)}–{clock_text(slot.end)}\n共 {slot.end-slot.start} 分钟')
        if self.auto_submit.isChecked(): self.submit_pending()

    def submit_pending(self):
        if self.pending_slot is not None:
            slot, self.pending_slot = self.pending_slot, None
            self.submit.setEnabled(False)
            self.bookingRequested.emit(slot)


# Native library reservations, with school-specific HTTP adapters.


MODULE_INFO = {'name': '图书馆预约', 'icon': '▤', 'description': '选择学校，登录账号并预约图书馆学习室。目前支持多伦多大学密西沙加校区。'}


class LibraryPage(QWidget):
    completed = Signal(int, object, str)

    def __init__(self, window=None, defer_build=False):
        super().__init__(getattr(window, "_module_build_parent", None))
        self.window = window
        if not defer_build:
            for _ in self.build_steps():
                pass

    def build_steps(self):
        self.session = None
        self.generation = 0
        self.busy = False
        self.authenticated = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 28, 36, 28)
        layout.setSpacing(14)
        header = QHBoxLayout()
        title = QLabel('图书馆预约'); title.setObjectName('pageTitle'); header.addWidget(title)
        header.addStretch()
        self.settings_toggle = QPushButton('收起学校与登录设置 ▴')
        self.settings_toggle.setCheckable(True)
        self.settings_toggle.setChecked(True)
        header.addWidget(self.settings_toggle)
        layout.addLayout(header)
        card = self.settings_panel = QFrame(); card.setObjectName('contentPanel')
        self.settings_toggle.toggled.connect(self.set_settings_expanded)
        body = QVBoxLayout(card); body.setContentsMargins(18, 16, 18, 16); body.setSpacing(10)
        school_row = QHBoxLayout()
        self.search = QLineEdit(); self.search.setPlaceholderText('搜索学校名称'); self.search.setClearButtonEnabled(True)
        self.school = StyledComboBox(); self.school.addItem(SCHOOL_NAME, 'utm')
        school_row.addWidget(self.search, 1); school_row.addWidget(self.school, 3)
        body.addLayout(school_row)
        self.location = StyledComboBox()
        self.location.addItem('Library Study Rooms', BOOKING_URL)
        self.location.setEnabled(False)
        body.addWidget(self.location)
        self.location.currentIndexChanged.connect(self.change_location)
        self.username = QLineEdit(); self.username.setPlaceholderText('学校账号（UTORid）')
        self.password = QLineEdit(); self.password.setPlaceholderText('学校密码'); self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.connect_button = QPushButton('登录'); self.connect_button.setObjectName('primaryButton'); self.disconnect_button = QPushButton('退出会话'); self.disconnect_button.setEnabled(False)
        row = QHBoxLayout()
        for control in (self.username, self.password, self.connect_button, self.disconnect_button): row.addWidget(control)
        body.addLayout(row)
        layout.addWidget(card)
        self.status = QLabel('尚未登录 · 登录时将账号密码保存至本地 config，下次自动填入'); self.status.setWordWrap(True); self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        yield "正在构建图书馆预约 · 日期与房间…"
        self.schedule = ReservationSchedule(defer_build=True)
        yield from self.schedule.build_steps()
        layout.addWidget(self.schedule, 1)
        self.schedule.bookingRequested.connect(self.book_selection)
        self.schedule.dateRequested.connect(self.load_date)
        self.search.textChanged.connect(self.filter_school)
        self.connect_button.clicked.connect(self.connect_session)
        self.password.returnPressed.connect(self.connect_session)
        self.disconnect_button.clicked.connect(self.disconnect_session)
        self.completed.connect(self.finish)
        username, password = load_credentials()
        self.username.setText(username); self.password.setText(password)

    def set_settings_expanded(self, expanded):
        self.settings_panel.setVisible(expanded)
        self.settings_toggle.setText('收起学校与登录设置 ▴' if expanded else '展开学校与登录设置 ▾')

    def filter_school(self, text):
        self.school.clear()
        if text.strip().casefold() in (SCHOOL_NAME + ' utm 多伦多大学 密西沙加').casefold():
            self.school.addItem(SCHOOL_NAME, 'utm')
        self.connect_button.setEnabled(not self.busy and self.session is None and self.school.count() > 0)

    def run(self, action):
        if self.busy: return
        self.busy = True
        self.location.setEnabled(False)
        self.schedule.setEnabled(False)
        generation = self.generation
        self.connect_button.setEnabled(False)
        self.status.setText('正在连接学校服务…')
        def work():
            try: result, error = action(), ''
            except ValueError as exc: result, error = None, str(exc)
            except Exception: result, error = None, '连接失败，请检查网络后重试。'
            try: self.completed.emit(generation, result, error)
            except RuntimeError: pass
        threading.Thread(target=work, daemon=True, name='library-reservations').start()

    def connect_session(self):
        if self.busy or self.session is not None or self.school.currentData() != 'utm': return
        username, password = self.username.text().strip(), self.password.text()
        if not username or not password:
            self.status.setText('请输入学校账号和密码。'); return
        try:
            save_credentials(username, password)
        except OSError:
            self.status.setText('账号密码保存失败，请检查 config 目录写入权限。'); return
        session = LibrarySession(); self.session = session
        self.password.clear()
        self.search.setEnabled(False); self.school.setEnabled(False); self.username.setEnabled(False); self.password.setEnabled(False)
        self.disconnect_button.setEnabled(True)
        self.run(lambda: session.login(username, password))

    def finish(self, generation, document, error):
        if generation != self.generation: return
        self.busy = False
        self.sync_locations()
        self.schedule.setEnabled(True)
        if error:
            if not self.authenticated:
                self.disconnect_session()
            self.status.setText(error)
            return
        if isinstance(document, dict):
            receipt, calendar = document['receipt'], document['calendar']
            if any(f['type'] == 'password' for form in receipt.forms for f in form['fields']):
                self.disconnect_session(); self.status.setText('学校要求重新登录，预约未确认成功。'); return
            self.render_document(calendar or receipt)
            self.status.setText('预约请求已发送 · ' + ('时间表已更新。' if calendar else '请重新选择日期加载时间表。') + ' 学校返回：' + ' '.join(receipt.text)[:600])
            return
        if any(field['type'] == 'password' for form in document.forms for field in form['fields']):
            self.disconnect_session(); self.status.setText('会话已失效，请重新登录。'); return
        first_connection = not self.authenticated
        self.authenticated = True
        if first_connection:
            self.settings_toggle.setChecked(False)
        self.status.setText('已连接 · 预约是否成功以学校返回的结果为准')
        self.render_document(document)

    def render_document(self, document):
        records = reservations_from_document(document)
        self.schedule.set_records(records)
        if not records:
            self.status.setText('未识别到预约时段，请重新选择日期或重新登录。')

    def sync_locations(self):
        self.location.blockSignals(True)
        self.location.clear()
        for label, url in getattr(self.session, 'locations', []):
            self.location.addItem(label, url)
        index = self.location.findData(getattr(self.session, 'entry_url', None))
        if index >= 0: self.location.setCurrentIndex(index)
        self.location.setEnabled(self.location.count() > 1)
        self.location.blockSignals(False)

    def change_location(self, *_):
        if self.busy or not self.authenticated or self.session is None: return
        url = self.location.currentData()
        if url is None: return
        session = self.session
        day = self.schedule.calendar.selectedDate().toPython()
        self.schedule.set_records([])
        self.run(lambda: session.select_location(url, day))

    def load_date(self, day):
        if self.busy or not self.authenticated or self.session is None:
            return
        session = self.session
        self.run(lambda: session.load_day(day))

    def book_selection(self, slot):
        if self.busy:
            return
        if not self.authenticated:
            self.schedule.detail.setText('请先登录学校账号并加载预约情况。')
            return
        title = self.schedule.booking_title.text().strip()
        if not title:
            self.schedule.detail.setText('请输入预约标题。'); return
        session = self.session
        self.run(lambda: session.book_slot(slot, title))

    def disconnect_session(self):
        self.settings_toggle.setChecked(True)
        self.generation += 1; self.busy = False; self.session = None
        self.authenticated = False
        self.sync_locations()
        self.schedule.setEnabled(True)
        self.password.clear(); self.username.clear()
        self.schedule.set_records([])
        for control in (self.search, self.school, self.username, self.password): control.setEnabled(True)
        self.connect_button.setEnabled(self.school.count() > 0); self.disconnect_button.setEnabled(False)
        username, password = load_credentials()
        self.username.setText(username); self.password.setText(password)
        self.status.setText('已退出会话 · 已保留 config 中的账号密码')


def create_widget(window):
    return LibraryPage(window)


def create_widget_steps(window):
    page = LibraryPage(window, defer_build=True)
    yield from page.build_steps()
    return page
