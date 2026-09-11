"""新增事宜：创建一次性或周期性的学习任务。"""

import json
from utils import write_json
import uuid
from datetime import date
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QPushButton, QVBoxLayout, QWidget)
from ui_widgets import DateDropdown, StyledComboBox, clip_to_rounded_frame

MODULE_INFO = {"name": "新增事宜", "icon": "+"}
TASKS_FILE = Path(__file__).resolve().parents[1] / "config" / "tasks.json"
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def create_widget(window):
    steps = create_widget_steps(window)
    while True:
        try:
            next(steps)
        except StopIteration as result:
            return result.value


def create_widget_steps(window):
    page = QWidget(getattr(window, "_module_build_parent", None)); layout = QVBoxLayout(page)
    layout.setContentsMargins(36, 28, 36, 28); layout.setSpacing(14)
    layout.addWidget(_heading("新增事宜", "创建课程之外的学习任务，并在完成后领取经验"))
    panel = QFrame(); panel.setObjectName("contentPanel"); form = QVBoxLayout(panel)
    form.setContentsMargins(18, 18, 18, 18); form.setSpacing(14)
    title = QLineEdit(); title.setPlaceholderText("事宜名称，例如：完成线性代数小作业")
    type_box = StyledComboBox(); type_box.addItems(("小作业", "大作业", "考试", "课程", "其他"))
    frequency = StyledComboBox(); frequency.addItems(("一次性", "每天", "每周"))
    term = StyledComboBox(); term.addItems(("不限", "Fall", "Winter", "Summer"))
    yield "正在构建新增事宜 · 日期选项…"
    # 与课程表一致，使用公共的年/月/日下拉控件，而不是系统日历弹窗。
    start_date = DateDropdown(QDate.currentDate())
    weekday = StyledComboBox(); weekday.addItems(WEEKDAYS); weekday.setCurrentIndex(date.today().weekday())
    for field in (title, type_box, frequency, term, start_date, weekday): field.setMinimumHeight(38)

    yield "正在构建新增事宜 · 表单…"
    first = QHBoxLayout(); first.setSpacing(14)
    first.addWidget(_field_box("名称", title), 3)
    first.addWidget(_field_box("类型", type_box), 1)
    second = QHBoxLayout(); second.setSpacing(14)
    second.addWidget(_field_box("频率", frequency), 1)
    date_box = _field_box("截止日期", start_date); second.addWidget(date_box, 2)
    term_box = _field_box("学期", term); second.addWidget(term_box, 1)
    weekday_box = _field_box("星期", weekday); second.addWidget(weekday_box, 1)
    form.addLayout(first); form.addLayout(second)
    hint = QLabel("一次性事宜会从创建当天每天显示到截止日期；每天/每周事宜会持续到学期结束，并按类型发放经验。")
    hint.setObjectName("formHint"); hint.setWordWrap(True); form.addWidget(hint)
    add = QPushButton("添加事宜")
    add.setObjectName("primaryButton")
    add.setCursor(Qt.CursorShape.PointingHandCursor)
    add.setFixedWidth(120)
    actions = QHBoxLayout(); actions.setSpacing(8); actions.addStretch(); actions.addWidget(add)
    form.addLayout(actions)
    layout.addWidget(panel)

    yield "正在构建新增事宜 · 任务列表…"
    list_panel = QFrame(); list_panel.setObjectName("contentPanel"); list_layout = QVBoxLayout(list_panel)
    list_layout.setContentsMargins(18, 14, 18, 14); list_layout.setSpacing(10)
    list_title = QLabel("已添加事宜"); list_title.setObjectName("sectionTitle")
    tasks_list = QListWidget()
    tasks_list.setAlternatingRowColors(False)
    # 列表行会盖住外框圆角，把视口裁成同样的圆角。
    clip_to_rounded_frame(tasks_list, 11)
    empty_hint = QLabel("还没有事宜，填写上面的表单即可添加")
    empty_hint.setObjectName("muted")
    empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
    empty_hint.setWordWrap(True)
    delete = QPushButton("删除选中事宜"); delete.setObjectName("dangerButton")
    delete.setEnabled(False); delete.setFixedWidth(140)
    list_toolbar = QHBoxLayout(); list_toolbar.addWidget(list_title); list_toolbar.addStretch(); list_toolbar.addWidget(delete)
    list_layout.addLayout(list_toolbar)
    list_layout.addWidget(empty_hint, 1)
    list_layout.addWidget(tasks_list, 1)
    layout.addWidget(list_panel, 1)
    tasks = _load_tasks()

    def render():
        tasks_list.clear()
        # 空列表用一个提示代替空白列表框，避免整页留出大白洞。
        empty_hint.setVisible(not tasks)
        tasks_list.setVisible(bool(tasks))
        for task in tasks:
            text = f"{task.get('title', '未命名')}  ·  {task.get('type', '其他')}  ·  {task.get('frequency', '一次性')}  ·  {task.get('date_start', '')}"
            if task.get("frequency") == "每周":
                try:
                    day = int(task.get('weekday', 0))
                except (TypeError, ValueError, OverflowError):
                    day = -1
                text += f" · {WEEKDAYS[day] if 0 <= day <= 6 else '星期待定'}"
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
        date_box.caption.setText("截止日期" if one_time else "开始日期")
        term_box.setVisible(not one_time)
        weekday_box.setVisible(weekly)
    frequency.currentTextChanged.connect(update_frequency)
    tasks_list.currentRowChanged.connect(lambda _: update_delete_state())
    delete.clicked.connect(delete_task)
    update_frequency(frequency.currentText())
    add.clicked.connect(add_task); render(); return page


def _field_box(label, field):
    """标签在上、控件在下的字段盒子，和其他模块的表单保持一致。"""
    box = QWidget(); box.setObjectName("fieldBox")
    box_layout = QVBoxLayout(box); box_layout.setContentsMargins(0, 0, 0, 0); box_layout.setSpacing(6)
    label_widget = QLabel(label); label_widget.setObjectName("formLabel")
    box_layout.addWidget(label_widget); box_layout.addWidget(field)
    box.caption = label_widget
    return box


def _heading(title, subtitle):
    widget = QWidget(); layout = QVBoxLayout(widget); layout.setContentsMargins(0, 0, 0, 4); layout.setSpacing(4)
    a = QLabel(title); a.setObjectName("pageTitle"); b = QLabel(subtitle); b.setObjectName("muted")
    layout.addWidget(a); layout.addWidget(b); return widget


def _load_tasks():
    try:
        data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
        return [task for task in data if isinstance(task, dict)] if isinstance(data, list) else []
    except (OSError, UnicodeError, json.JSONDecodeError): return []


def _save_tasks(tasks):
    TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_json(TASKS_FILE, tasks)
