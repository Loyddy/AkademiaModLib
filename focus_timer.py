"""专注计时：选择一个时长，完成一段不被打扰的专注。"""
import math
import time

from PySide6.QtCore import QEasingCurve, QTimer, Qt
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QProgressBar, QPushButton, QSizePolicy, QSlider,
                               QVBoxLayout, QWidget)

from utils import PropertyAnimation as QPropertyAnimation

MODULE_INFO = {"name": "专注计时", "icon": "◉"}


def create_widget(window):
    page = QWidget(getattr(window, "_module_build_parent", None))
    layout = QVBoxLayout(page)
    layout.setContentsMargins(36, 28, 36, 28)
    layout.setSpacing(14)
    layout.addWidget(_heading("专注计时", "给重要的事情留出一段安静时间"))

    panel = QFrame()
    panel.setObjectName("contentPanel")
    panel_layout = QVBoxLayout(panel)
    panel_layout.setContentsMargins(28, 30, 28, 30)
    panel_layout.setSpacing(16)
    time_label = QLabel("25:00")
    time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    time_label.setObjectName("timerDisplay")
    time_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
    progress = QProgressBar()
    progress.setObjectName("focusProgress")
    progress.setRange(0, 1500 * 1000)
    progress.setValue(0)
    progress.setTextVisible(False)
    progress.setFixedHeight(8)
    duration_title = QLabel("专注时长")
    duration_title.setObjectName("sectionTitle")
    duration_title.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
    minutes_input = QLineEdit("25")
    minutes_input.setValidator(QIntValidator(1, 180, minutes_input))
    minutes_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
    minutes_input.setFixedWidth(72)
    minutes_input.setToolTip("1–180 分钟")
    minutes_label = QLabel("分钟")
    minutes_label.setObjectName("muted")
    duration_slider = QSlider(Qt.Orientation.Horizontal)
    duration_slider.setRange(1, 180)
    duration_slider.setValue(25)
    duration_slider.setMinimumWidth(220)
    duration_slider.setToolTip("拖动选择专注时长")
    # 开始/重置各自固定宽度并居中成一行，避免按钮被拉满整块面板。
    start = QPushButton("开始专注")
    start.setObjectName("primaryButton")
    start.setCursor(Qt.CursorShape.PointingHandCursor)
    start.setFixedWidth(150)
    reset = QPushButton("重置")
    reset.setCursor(Qt.CursorShape.PointingHandCursor)
    reset.setFixedWidth(110)
    duration_row = QHBoxLayout()
    duration_row.setSpacing(12)
    duration_row.addWidget(minutes_input)
    duration_row.addWidget(minutes_label)
    duration_row.addWidget(duration_slider, 1)
    action_row = QHBoxLayout()
    action_row.setSpacing(12)
    action_row.addStretch()
    action_row.addWidget(start)
    action_row.addWidget(reset)
    action_row.addStretch()
    panel_layout.addStretch(2)
    panel_layout.addWidget(time_label)
    panel_layout.addWidget(progress)
    panel_layout.addSpacing(26)
    panel_layout.addWidget(duration_title)
    panel_layout.addLayout(duration_row)
    panel_layout.addSpacing(22)
    panel_layout.addLayout(action_row)
    panel_layout.addStretch(3)
    panel.setFixedHeight(392)
    panel.setMaximumWidth(640)
    # 卡片按内容定尺寸并居中，空白留给工作区背景而不是一块大白板。
    card_row = QHBoxLayout()
    card_row.addStretch(1)
    card_row.addWidget(panel, 8)
    card_row.addStretch(1)
    layout.addSpacing(24)
    layout.addLayout(card_row)
    layout.addStretch(1)

    timer = QTimer(page)
    seconds = [1500]
    total_seconds = [1500]
    started = [False]
    remaining = [1500.0]
    deadline = [0.0]
    progress_animation = QPropertyAnimation(progress, b"value", page)
    progress_animation.setDuration(700)
    progress_animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def selected_minutes():
        try:
            value = max(1, min(180, int(minutes_input.text())))
        except ValueError:
            value = duration_slider.value()
        minutes_input.setText(str(value))
        duration_slider.setValue(value)
        return value

    def sync_input(value):
        minutes_input.blockSignals(True)
        minutes_input.setText(str(value))
        minutes_input.blockSignals(False)

    # 专注进行中改时长不会影响当前这一段，因此运行期间锁定时长控件。
    def lock_duration(locked):
        duration_slider.setEnabled(not locked)
        minutes_input.setEnabled(not locked)

    duration_slider.valueChanged.connect(sync_input)
    minutes_input.editingFinished.connect(selected_minutes)

    def update_display(animated=True):
        time_label.setText(f"{seconds[0] // 60:02d}:{seconds[0] % 60:02d}")
        elapsed = (total_seconds[0] - seconds[0]) * 1000
        if animated:
            progress_animation.stop()
            progress_animation.setStartValue(progress.value())
            progress_animation.setEndValue(elapsed)
            progress_animation.start()
        else:
            progress_animation.stop()
            progress.setValue(elapsed)

    def tick():
        remaining[0] = max(0.0, deadline[0] - time.monotonic())
        seconds[0] = math.ceil(remaining[0])
        update_display()
        if seconds[0] == 0:
            timer.stop()
            start.setText("开始专注")
            lock_duration(False)

    def begin():
        if not started[0] or seconds[0] <= 0:
            seconds[0] = selected_minutes() * 60
            total_seconds[0] = seconds[0]
            started[0] = True
            remaining[0] = float(seconds[0])
        lock_duration(True)
        deadline[0] = time.monotonic() + remaining[0]
        progress.setRange(0, total_seconds[0] * 1000)
        update_display(animated=False)
        timer.start(1000)
        start.setText("暂停专注")

    def toggle():
        if timer.isActive():
            tick()
            timer.stop()
            progress_animation.stop()
            start.setText("继续专注" if seconds[0] else "开始专注")
        else:
            begin()

    def clear():
        timer.stop()
        progress_animation.stop()
        seconds[0] = selected_minutes() * 60
        total_seconds[0] = seconds[0]
        remaining[0] = float(seconds[0])
        started[0] = False
        lock_duration(False)
        progress.setRange(0, total_seconds[0] * 1000)
        update_display(animated=False)
        start.setText("开始专注")

    timer.timeout.connect(tick)
    start.clicked.connect(toggle)
    reset.clicked.connect(clear)
    return page


def _heading(title, subtitle):
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 4)
    layout.setSpacing(4)
    title_label = QLabel(title)
    title_label.setObjectName("pageTitle")
    subtitle_label = QLabel(subtitle)
    subtitle_label.setObjectName("muted")
    layout.addWidget(title_label)
    layout.addWidget(subtitle_label)
    return widget
