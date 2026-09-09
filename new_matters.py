"""新增事宜：创建一次性或周期性的学习任务。"""

import json
import uuid
from datetime import date
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (QDateEdit, QFrame, QHBoxLayout, QLabel,
                             QLineEdit, QListWidget, QPushButton,
                             QVBoxLayout, QWidget)
from ui_widgets import StyledComboBox

MODULE_INFO = {"name": "新增事宜", "icon": "+"}
TASKS_FILE = Path(__file__).resolve().parents[1] / "config" / "tasks.json"
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def create_widget(window):
    page = QWidget(); layout = QVBoxLayout(page)
    layout.setContentsMargins(36, 28, 36, 28); layout.setSpacing(14)
    layout.addWidget(_heading("新增事宜", "创建课程之外的学习任务，并在完成后领取经验"))
    panel = QFrame(); panel.setObjectName("contentPanel"); form = QVBoxLayout(panel)
    form.setContentsMargins(18, 18, 18, 18); form.setSpacing(12)
    title = QLineEdit(); title.setPlaceholderText("事宜名称，例如：完成线性代数小作业")
    type_box = StyledComboBox(); type_box.addItems(("小作业", "大作业", "考试", "课程", "其他"))
    frequency = StyledComboBox(); frequency.addItems(("一次性", "每天", "每周"))
    term = StyledComboBox(); term.addItems(("不限", "Fall", "Winter", "Summer"))
    start_date = QDateEdit(QDate.currentDate()); start_date.setCalendarPopup(True); start_date.setDisplayFormat("yyyy-MM-dd")
    weekday = StyledComboBox(); weekday.addItems(WEEKDAYS); weekday.setCurrentIndex(date.today().weekday())
    for field in (title, type_box, frequency, term, start_date, weekday): field.setFixedHeight(36)

    first = QHBoxLayout(); _field(first, "名称", title, 3); _field(first, "类型", type_box, 1)
    second = QHBoxLayout(); _field(second, "频率", frequency, 1); date_label = _field(second, "开始日期", start_date, 1); term_label = _field(second, "学期", term, 1); weekday_label = _field(second, "星期", weekday, 1)
    add = QPushButton("添加事宜"); add.setFixedWidth(120)
    second.addWidget(add)
    form.addLayout(first); form.addLayout(second)
    hint = QLabel("一次性事宜会从创建当天每天显示到截止日期；每天/每周事宜会持续到学期结束，并按类型发放经验。")
    hint.setObjectName("muted"); hint.setWordWrap(True); form.addWidget(hint)
    layout.addWidget(panel)

    list_panel = QFrame(); list_panel.setObjectName("contentPanel"); list_layout = QVBoxLayout(list_panel)
    list_layout.setContentsMargins(18, 14, 18, 14); list_layout.setSpacing(8)
    list_title = QLabel("已添加事宜"); list_title.setObjectName("sectionTitle")
    tasks_list = QListWidget()
    delete = QPushButton("删除选中事宜"); delete.setEnabled(False); delete.setFixedWidth(140)
    list_toolbar = QHBoxLayout(); list_toolbar.addWidget(list_title); list_toolbar.addStretch(); list_toolbar.addWidget(delete)
    list_layout.addLayout(list_toolbar); list_layout.addWidget(tasks_list, 1)
    layout.addWidget(list_panel, 1)
    tasks = _load_tasks()

    def render():
        tasks_list.clear()
        for task in tasks:
            text = f"{task.get('title', '未命名')}  ·  {task.get('type', '其他')}  ·  {task.get('frequency', '一次性')}  ·  {task.get('date_start', '')}"
            if task.get("frequency") == "每周": text += f" · {WEEKDAYS[int(task.get('weekday', 0))]}"
            tasks_list.addItem(text)

    def add_task():
        name = title.text().strip()
        if not name:
            title.setFocus(); return
        tasks.append({
            "id": uuid.uuid4().hex,
            "title": name,
            "type": type_box.currentText(),
            "time": "",
            "frequency": frequency.currentText(),
            "term": term.currentText(),
            "date_start": start_date.date().toString("yyyy-MM-dd"),
            "created_date": date.today().isoformat(),
            "weekday": weekday.currentIndex(),
        })
        _save_tasks(tasks); title.clear(); render()

    def update_delete_state():
        delete.setEnabled(tasks_list.currentRow() >= 0)

    def delete_task():
        row = tasks_list.currentRow()
        if row < 0 or row >= len(tasks): return
        tasks.pop(row); _save_tasks(tasks); render(); delete.setEnabled(False)

    def update_frequency(value):
        one_time = value == "一次性"
        weekly = value == "每周"
        date_label.setText("截止日期" if one_time else "开始日期")
        term_label.setVisible(not one_time)
        term.setVisible(not one_time)
        weekday_label.setVisible(weekly)
        weekday.setVisible(weekly)
    frequency.currentTextChanged.connect(update_frequency)
    tasks_list.currentRowChanged.connect(lambda _: update_delete_state())
    delete.clicked.connect(delete_task)
    update_frequency(frequency.currentText())
    add.clicked.connect(add_task); render(); return page


def _field(row, label, field, stretch=1):
    label_widget = QLabel(label); label_widget.setObjectName("formLabel")
    row.addWidget(label_widget); row.addWidget(field, stretch); row.setSpacing(8); return label_widget


def _heading(title, subtitle):
    widget = QWidget(); layout = QVBoxLayout(widget); layout.setContentsMargins(0, 0, 0, 4); layout.setSpacing(4)
    a = QLabel(title); a.setObjectName("pageTitle"); b = QLabel(subtitle); b.setObjectName("muted")
    layout.addWidget(a); layout.addWidget(b); return widget


def _load_tasks():
    try:
        data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError): return []


def _save_tasks(tasks):
    TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TASKS_FILE.write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")
