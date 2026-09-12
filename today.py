"""今日事宜：课程与自定义事宜的完成清单。"""

import json
from utils import write_json
from datetime import date, timedelta
from pathlib import Path

from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget
from ui_widgets import ElidedLabel

MODULE_INFO = {"name": "今日事宜", "icon": "★"}
COURSES_FILE = Path(__file__).resolve().parents[1] / "config" / "courses.json"
TASKS_FILE = Path(__file__).resolve().parents[1] / "config" / "tasks.json"
COMPLETIONS_FILE = Path(__file__).resolve().parents[1] / "config" / "completions.json"
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
XP_BY_TYPE = {"小作业": 10, "大作业": 40, "考试": 20, "课程": 20, "其他": 10}


class TodayPage(QWidget):
    closed = Signal()
    completion_changed = Signal()

    def __init__(self, window, *, floating=False):
        super().__init__(None if floating else getattr(window, "_module_build_parent", None))
        self.window = window
        self.floating = floating
        self.floating_window = None
        if floating:
            self.setWindowTitle("今日事宜 · 悬浮窗")
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
            self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
            self.resize(460, 560)
            self.setMinimumSize(340, 260)
        elif isinstance(window, QWidget):
            window.installEventFilter(self)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(36, 28, 36, 28)
        self.layout.setSpacing(14)
        self.title = QLabel("今日事宜")
        self.title.setObjectName("pageTitle")
        self.subtitle = QLabel()
        self.subtitle.setObjectName("muted")
        self.summary = QLabel()
        self.summary.setObjectName("pageSummary")
        self.selected_date = date.today()
        self._last_today = self.selected_date
        self.navigation = QHBoxLayout()
        self.navigation.setSpacing(10)
        self.previous_button = QPushButton("昨天")
        self.today_button = QPushButton("回到今天")
        self.today_button.setObjectName("primaryButton")
        self.next_button = QPushButton("明天")
        self.previous_button.clicked.connect(lambda: self._move_date(-1))
        self.today_button.clicked.connect(self._go_today)
        self.next_button.clicked.connect(lambda: self._move_date(1))
        self.navigation.addWidget(self.previous_button)
        self.navigation.addWidget(self.today_button)
        self.navigation.addWidget(self.next_button)
        self.navigation.addStretch()
        # 清空完成状态会同时扣除经验，属于破坏性操作，使用危险按钮样式。
        self.reset_button = QPushButton("重置所有完成状态")
        self.reset_button.setObjectName("dangerButton")
        self.reset_button.clicked.connect(self.reset_all)
        self.navigation.addWidget(self.reset_button)
        self.floating_button = QPushButton("开启悬浮窗")
        self.floating_button.setCheckable(True)
        self.floating_button.toggled.connect(self._toggle_floating)
        self.navigation.addWidget(self.floating_button)
        if floating:
            for button in (self.previous_button, self.today_button, self.next_button,
                           self.reset_button, self.floating_button):
                button.hide()
            self.pin_button = QPushButton("置顶")
            self.pin_button.setCheckable(True)
            self.pin_button.setChecked(True)
            self.pin_button.toggled.connect(self._set_pinned)
            self.navigation.addWidget(self.pin_button)
            self.close_button = QPushButton("关闭悬浮窗")
            self.close_button.clicked.connect(self.close)
            self.navigation.addWidget(self.close_button)
            self.layout.setContentsMargins(16, 16, 16, 16)
        self.content = QVBoxLayout()
        self.content.setContentsMargins(0, 0, 8, 0)
        self.content.setSpacing(10)
        self.content.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.content_host = QWidget()
        self.content_host.setLayout(self.content)
        self.content_scroll = QScrollArea()
        self.content_scroll.setObjectName("matterScroll")
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content_scroll.setWidget(self.content_host)
        self.layout.addWidget(self.title)
        self.layout.addWidget(self.subtitle)
        self.layout.addLayout(self.navigation)
        self.layout.addWidget(self.summary)
        self.layout.addWidget(self.content_scroll, 1)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(30_000)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start()
        self.refresh()

    def showEvent(self, event):
        self.refresh()
        super().showEvent(event)

    def refresh(self):
        today = date.today()
        if self.floating or self.selected_date == self._last_today:
            self.selected_date = today
        self._last_today = today
        selected_date = self.selected_date
        label = "今天" if selected_date == today else (
            "昨天" if selected_date == today - timedelta(days=1) else
            "明天" if selected_date == today + timedelta(days=1) else ""
        )
        prefix = f"{label}  ·  " if label else ""
        self.subtitle.setText(f"{prefix}{selected_date:%Y年%m月%d日}  ·  {WEEKDAYS[selected_date.weekday()]}")
        self._clear_content()
        items = [(course, "course") for course in _today_courses(selected_date)]
        items.extend((task, "task") for task in _today_tasks(selected_date))
        items.sort(key=lambda item: item[0].get("time", item[0].get("start", "99:99")))
        self.summary.setText(f"{label or '该日'}共有 {len(items)} 件事宜")
        if not items:
            empty = QLabel(f"{label or '该日'}没有安排事宜。")
            empty.setObjectName("emptyState")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.content.addWidget(empty)
            return
        completions = _load_completions()
        for item, item_kind in items:
            self._add_item(item, item_kind, completions)

    def _add_item(self, item, item_kind, completions):
        if item_kind == "course":
            title = item.get("title") or item.get("arrangement") or item.get("code", "未命名课程")
            code = item.get("code", "课程")
            start = item.get("start", "--:--")
            end = item.get("end", "--:--")
            detail = f"{start} - {end}   {code} · {item.get('location') or '地点待定'}"
            xp = XP_BY_TYPE["课程"]
            key = f"course:{self.selected_date.isoformat()}:{code}:{start}"
        else:
            title = item.get("title", "未命名事宜")
            detail = f"{item.get('time') or '时间待定'}   {item.get('type', '其他')} · {item.get('frequency', '一次性')}"
            xp = XP_BY_TYPE.get(item.get("type", "其他"), 10)
            key = _task_completion_key(item, self.selected_date)
            detail += f"   ·   完成可得 {xp} XP"
            due_days = _task_due_days(item, self.selected_date)
            if due_days is not None and 0 <= due_days <= 3:
                title = f"★  {title}"
                due_label = "今天到期" if due_days == 0 else f"还剩 {due_days} 天"
                detail += f"   ·   这件事快到期了 · {due_label}"
        card = QFrame(); card.setObjectName("courseSummaryCard")
        if self.floating:
            card.setMinimumHeight(90)
        else:
            card.setFixedHeight(78)
        row = QHBoxLayout(card); row.setContentsMargins(16, 12, 12, 12); row.setSpacing(12)
        text_layout = QVBoxLayout(); text_layout.setSpacing(3)
        if self.floating:
            heading = QLabel(title); details = QLabel(detail)
            heading.setWordWrap(True); details.setWordWrap(True)
        else:
            # 卡片高度固定，长标题改为省略号，避免把页面撑出横向滚动条。
            heading = ElidedLabel(title); details = ElidedLabel(detail)
        heading.setObjectName("matterTitle")
        details.setObjectName("muted")
        text_layout.addWidget(heading); text_layout.addWidget(details)
        row.addLayout(text_layout, 1)
        completed = _task_is_completed(item, self.selected_date, completions) if item_kind == "task" else key in completions
        # 未完成时用主操作按钮（紫色实心），已完成用绿色状态按钮，避免把
        # “完成”这个正面动作画成警示红色。
        done = QPushButton("已完成" if completed else "完成")
        done.setObjectName("completedButton" if completed else "primaryButton")
        done.setCursor(Qt.CursorShape.PointingHandCursor)
        done.setEnabled(not completed)
        done.setFixedSize(108, 38)
        done.clicked.connect(lambda: self._complete(key, xp, done))
        row.addWidget(done)
        self.content.addWidget(card)

    def _complete(self, key, xp, button):
        completions = _load_completions()
        if key in completions:
            return
        completions.append(key)
        _save_completions(completions)
        # 先改可见状态，再上报经验：宿主钩子缺失或抛错时，卡片仍然会变成
        # 「已完成」，不会出现「文件写好了、按钮还是完成、再点也没反应」。
        button.setText("已完成")
        button.setObjectName("completedButton")
        button.style().unpolish(button)
        button.style().polish(button)
        button.setEnabled(False)
        _report_experience(self.window, xp)
        self.completion_changed.emit()

    def reset_all(self):
        completions = _load_completions()
        if not completions:
            QMessageBox.information(self, "重置完成状态", "当前没有已完成的课程或事宜。")
            return
        answer = QMessageBox.question(
            self, "重置完成状态",
            "确定要清除所有完成状态并扣除对应经验吗？课程和事宜本身不会被删除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        total_xp = sum(_completion_xp(item) for item in completions)
        _save_completions([])
        self.refresh()
        _report_experience(self.window, -total_xp)
        self.completion_changed.emit()

    def _toggle_floating(self, enabled):
        if enabled:
            if self.floating_window is None:
                panel = TodayPage(self.window, floating=True)
                self.floating_window = panel
                self.destroyed.connect(panel.deleteLater)
                panel.closed.connect(lambda: self.floating_button.setChecked(False))
                panel.completion_changed.connect(self.refresh)
                self.completion_changed.connect(panel.refresh)
            self.floating_window.refresh_timer.start()
            self.floating_window.refresh()
            self.floating_window.show()
            self.floating_window.raise_()
        elif self.floating_window is not None:
            self.floating_window.hide()
            self.floating_window.refresh_timer.stop()
        self.floating_button.setText("关闭悬浮窗" if enabled else "开启悬浮窗")

    def _set_pinned(self, pinned):
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, pinned)
        self.show()

    def closeEvent(self, event):
        if self.floating:
            self.refresh_timer.stop()
            self.closed.emit()
        super().closeEvent(event)

    def eventFilter(self, watched, event):
        if watched is self.window and event.type() == QEvent.Type.Close:
            self.floating_button.setChecked(False)
        return super().eventFilter(watched, event)

    def _move_date(self, days):
        self.selected_date += timedelta(days=days); self.refresh()

    def _go_today(self):
        self.selected_date = date.today(); self.refresh()

    def _clear_content(self):
        while self.content.count():
            item = self.content.takeAt(0)
            widget = item.widget()
            if widget:
                # takeAt 只解除布局关系，控件仍以 content_host 为父级并保持可见，
                # 在延迟删除真正执行前会一直绘制，刷新后出现重影卡片。
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()


def create_widget(window):
    return TodayPage(window)


def _today_courses(today):
    data = _load_json(COURSES_FILE, [])
    today_value = today.isoformat(); weekday = WEEKDAYS[today.weekday()]
    result = []
    for course in data if isinstance(data, list) else []:
        if not isinstance(course, dict): continue
        if course.get("weekday") and course.get("weekday") != weekday: continue
        if course.get("term") == _term_from_month(today.month):
            result.append(course); continue
        start_date = course.get("date_start") or course.get("date")
        end_date = course.get("date_end") or start_date
        if start_date and end_date and start_date <= today_value <= end_date: result.append(course)
    return sorted(result, key=lambda course: course.get("start", "99:99"))


def _today_tasks(today):
    result = []
    data = _load_json(TASKS_FILE, [])
    for task in data if isinstance(data, list) else []:
        if not isinstance(task, dict) or not _task_matches(task, today): continue
        result.append(task)
    return result


def _task_matches(task, selected):
    frequency = task.get("frequency", "一次性")
    start = task.get("date_start", "")
    if frequency == "一次性":
        if not start:
            return False
        try:
            deadline = date.fromisoformat(start)
            created = date.fromisoformat(task.get("created_date", date.today().isoformat()))
        except (TypeError, ValueError):
            return False
        return created <= selected <= deadline
    try:
        if start and selected < date.fromisoformat(start): return False
    except (TypeError, ValueError):
        return False
    term = task.get("term", "不限")
    if term not in ("不限", _term_from_month(selected.month)):
        return False
    if frequency == "每天":
        return True
    if frequency == "每周":
        # Weekly tasks recur every week after their start date.
        return True
    return False


def _task_completion_key(task, selected):
    task_id = task.get("id", task.get("title", "未命名事宜"))
    frequency = task.get("frequency", "一次性")
    if frequency == "一次性":
        return f"task:{task_id}:once:{selected.isoformat()}"
    if frequency == "每周":
        due_date = _task_due_date(task, selected)
        return f"task:{task_id}:week:{due_date.isoformat()}"
    return f"task:{task_id}:day:{selected.isoformat()}"


def _task_is_completed(task, selected, completions):
    """Weekly completion applies to every date in the same week."""
    exact_key = _task_completion_key(task, selected)
    if exact_key in completions:
        return True
    if task.get("frequency") == "每周":
        prefix = f"task:{task.get('id', task.get('title', '未命名事宜'))}:week:{_task_due_date(task, selected).isoformat()}"
        return any(str(item).startswith(prefix) for item in completions)
    if task.get("frequency") == "一次性":
        prefix = f"task:{task.get('id', task.get('title', '未命名事宜'))}:once:"
        return any(str(item).startswith(prefix) for item in completions)
    return False


def _task_due_days(task, selected):
    """Return days until a task is due, when its deadline is known."""
    frequency = task.get("frequency", "一次性")
    if frequency == "一次性":
        try:
            due = date.fromisoformat(task.get("date_start", ""))
        except (TypeError, ValueError):
            return None
    elif frequency == "每周":
        due = _task_due_date(task, selected)
    elif frequency == "每天":
        due = selected
    else:
        return None
    return (due - selected).days


def _task_due_date(task, selected):
    try:
        due_day = int(task.get("weekday", selected.weekday()))
    except (TypeError, ValueError, OverflowError):
        due_day = selected.weekday()
    if not 0 <= due_day <= 6:
        due_day = selected.weekday()
    return selected + timedelta(days=(due_day - selected.weekday()) % 7)


def _report_experience(window, amount):
    """Report finished work to the host, if this shell grew the hook.

    The 今日事宜 page must keep working on a shell that has no experience
    ledger: resolving the hook through getattr keeps a missing one inert
    instead of raising inside the click handler.
    """
    action = getattr(window, "add_experience" if amount >= 0 else "remove_experience", None)
    if callable(action):
        action(abs(int(amount)))


def _load_completions():
    data = _load_json(COMPLETIONS_FILE, [])
    return data if isinstance(data, list) else []


def _completion_xp(completion):
    value = str(completion)
    if value.startswith("course:"):
        return XP_BY_TYPE["课程"]
    if value.startswith("task:"):
        parts = value.split(":")
        task_id = parts[1] if len(parts) > 1 else ""
        for task in _load_json(TASKS_FILE, []):
            if isinstance(task, dict) and str(task.get("id", "")) == task_id:
                return XP_BY_TYPE.get(task.get("type", "其他"), 10)
        return XP_BY_TYPE["其他"]
    return 0


def _save_completions(data):
    write_json(COMPLETIONS_FILE, data)


def _load_json(path, fallback):
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError): return fallback


def _term_from_month(month):
    if month in (1, 2, 3, 4): return "Winter"
    if month in (5, 6, 7, 8): return "Summer"
    return "Fall"
