"""考试倒计时：记下每场考试的日期与时间，按日期排序并给出临近提醒。"""

import json
import uuid
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QProgressBar, QPushButton, QSpinBox,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)
from ui_widgets import (DateDropdown, StyledComboBox, TimeDropdown,
                        clip_to_rounded_frame)
from utils import write_json

MODULE_INFO = {"name": "考试倒计时", "icon": "⧗"}
EXAMS_FILE = Path(__file__).resolve().parents[1] / "config" / "exams.json"
# 考试类型只影响标签颜色以外的信息，纯粹是给清单分组用的。
KINDS = ("期中", "期末", "小测", "实验", "其他")
DEFAULT_REMIND_DAYS = 7
DATE_FORMAT = "yyyy-MM-dd"


def create_widget(window):
    steps = create_widget_steps(window)
    while True:
        try:
            next(steps)
        except StopIteration as result:
            return result.value


def create_widget_steps(window):
    page = QWidget(getattr(window, "_module_build_parent", None))
    layout = QVBoxLayout(page)
    layout.setContentsMargins(36, 28, 36, 28)
    layout.setSpacing(14)
    layout.addWidget(_heading("考试倒计时", "记下每场考试的时间地点，随时看到还剩几天"))

    yield "正在构建考试倒计时 · 录入表单…"
    form_panel = QFrame(); form_panel.setObjectName("contentPanel")
    form = QVBoxLayout(form_panel)
    form.setContentsMargins(18, 18, 18, 18); form.setSpacing(14)
    name = QLineEdit(); name.setPlaceholderText("考试名称，例如：线性代数（期中）")
    kind = StyledComboBox(); kind.addItems(KINDS)
    place = QLineEdit(); place.setPlaceholderText("考场，例如：三教 305")
    # 与课程表一致，使用公共的年/月/日与时分下拉控件，而不是系统日历弹窗。
    exam_date = DateDropdown(QDate.currentDate())
    exam_time = TimeDropdown()
    remind = _spin_int(DEFAULT_REMIND_DAYS, 1, 120)
    for field in (name, kind, place, exam_date, exam_time, remind):
        field.setMinimumHeight(38)
    first = QHBoxLayout(); first.setSpacing(14)
    first.addWidget(_field_box("考试名称", name), 3)
    first.addWidget(_field_box("类型", kind), 1)
    first.addWidget(_field_box("考场", place), 2)
    second = QHBoxLayout(); second.setSpacing(14)
    second.addWidget(_field_box("考试日期", exam_date), 2)
    second.addWidget(_field_box("开考时间", exam_time), 2)
    second.addWidget(_field_box("提前提醒（天）", remind), 1)
    form.addLayout(first); form.addLayout(second)
    hint = QLabel("清单按考试日期排序，未结束的考试按临近程度排在前面；"
                  "进入提醒天数范围后，页面会给出临近提示。所有数据保存在本地 config/exams.json。")
    hint.setObjectName("formHint"); hint.setWordWrap(True); form.addWidget(hint)
    add = QPushButton("添加考试"); add.setObjectName("primaryButton")
    add.setCursor(Qt.CursorShape.PointingHandCursor); add.setFixedWidth(120)
    actions = QHBoxLayout(); actions.addStretch(); actions.addWidget(add)
    form.addLayout(actions)
    layout.addWidget(form_panel)

    yield "正在构建考试倒计时 · 概览与提醒…"
    list_panel = QFrame(); list_panel.setObjectName("contentPanel")
    body = QVBoxLayout(list_panel)
    body.setContentsMargins(18, 16, 18, 16); body.setSpacing(12)
    title = QLabel("考试清单"); title.setObjectName("sectionTitle")
    summary = QLabel("还没有记录"); summary.setObjectName("scheduleSummary")
    # 筛选开关是可切换按钮：用默认按钮角色，选中态由主题的 :checked 统一给出。
    near = QPushButton("只看临近")
    near.setCheckable(True); near.setFixedWidth(110)
    delete = QPushButton("删除选中"); delete.setObjectName("dangerButton")
    delete.setEnabled(False); delete.setFixedWidth(110)
    toolbar = QHBoxLayout(); toolbar.setSpacing(12)
    toolbar.addWidget(title); toolbar.addWidget(summary); toolbar.addStretch()
    toolbar.addWidget(near); toolbar.addWidget(delete)
    body.addLayout(toolbar)

    stats = QHBoxLayout(); stats.setSpacing(12)
    tiles = (_stat_card("最近一场（天）"), _stat_card("临近考试"),
             _stat_card("考试总数"), _stat_card("已结束"))
    for tile, _value in tiles:
        stats.addWidget(tile, 1)
    body.addLayout(stats)

    alert = QLabel(""); alert.setObjectName("helperText"); alert.setWordWrap(True)
    calm = QLabel(""); calm.setObjectName("muted"); calm.setWordWrap(True)
    body.addWidget(alert); body.addWidget(calm)

    bar = QProgressBar(); bar.setRange(0, 1000); bar.setTextVisible(False)
    bar.setFixedHeight(8); bar.setValue(0)
    bar_caption = QLabel("添加考试后这里会显示最近一场的临近程度"); bar_caption.setObjectName("hint")
    body.addWidget(bar_caption); body.addWidget(bar)

    table = QTableWidget(0, 6)
    table.setObjectName("examTable")
    table.setHorizontalHeaderLabels(("考试", "类型", "日期", "时间", "考场", "剩余"))
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    table.setShowGrid(False); table.setAlternatingRowColors(False)
    header = table.horizontalHeader()
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    widths = (90, 110, 80, 130, 100)
    for column, width in enumerate(widths, start=1):
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(column, width)
    header.setFixedHeight(40)
    table.verticalHeader().setDefaultSectionSize(38)
    # 剩余天数是数值列：单元格与表头一起右对齐，视线不用来回跳。
    for column in range(6):
        entry = table.horizontalHeaderItem(column)
        entry.setTextAlignment((Qt.AlignmentFlag.AlignRight if column == 5
                                else Qt.AlignmentFlag.AlignLeft)
                               | Qt.AlignmentFlag.AlignVCenter)
    # 表头和单元格都会盖住外框圆角，各自按外框的圆角裁剪。
    table.setStyleSheet("""
        #examTable QHeaderView::section:first { border-top-left-radius: 12px; }
        #examTable QHeaderView::section:last { border-top-right-radius: 12px; }
    """)
    clip_to_rounded_frame(table, 11)
    clip_to_rounded_frame(header, 11, corners="tl,tr")
    empty = QLabel("还没有考试，填上面的表单就能记下第一场")
    empty.setObjectName("emptyState"); empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
    empty.setWordWrap(True)
    body.addWidget(empty, 1); body.addWidget(table, 1)
    layout.addWidget(list_panel, 1)

    records = _load_exams()
    visible_rows = []

    def refresh():
        # 先按日期排序并把剩余天数算一次，后面所有统计和提示都复用这份结果。
        rows_all = [(record, _days_left(record)) for record in _sorted_exams(records)]
        rows = rows_all
        if near.isChecked():
            rows = [row for row in rows_all
                    if row[1] is not None and 0 <= row[1] <= remind.value()]
        visible_rows.clear(); visible_rows.extend(rows)

        table.setRowCount(len(rows))
        for row_index, (record, days) in enumerate(rows):
            cells = (record["name"], record["kind"], record["date"],
                     record["time"] or "—", record["place"] or "—", _countdown(days))
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column == 5:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, record["id"])
                table.setItem(row_index, column, item)

        # 空清单用提示代替空白表格，避免整页留出大白洞。
        empty.setVisible(not rows); table.setVisible(bool(rows))
        empty.setText("还没有考试，填上面的表单就能记下第一场" if not records
                      else f"提醒范围内（{remind.value()} 天内）没有考试")

        counts = [days for _record, days in rows_all]
        upcoming = [days for days in counts if days is not None and days >= 0]
        soon = [days for days in upcoming if days <= remind.value()]
        past = [days for days in counts if days is not None and days < 0]
        nearest = min(upcoming) if upcoming else None
        _tiles(tiles, nearest, len(soon), len(records), len(past))
        if records:
            summary.setText(f"{len(records)} 场考试 · {len(soon)} 场临近"
                            + (f" · 最近一场 {nearest} 天后" if nearest is not None else ""))
        else:
            summary.setText("还没有记录")

        lines = [f'{record["name"]}（{record["date"]} {record["time"] or "时间待定"}，'
                 f'{_countdown(days)}）'
                 for record, days in rows_all
                 if days is not None and 0 <= days <= remind.value()]
        if lines:
            alert.setText(f"临近提醒（{remind.value()} 天内）：" + "；".join(lines))
        alert.setVisible(bool(lines)); calm.setVisible(not lines)
        calm.setText("提醒范围内还没有考试，先安心复习" if records
                     else "添加考试后，进入提醒天数的考试会在这里提示你")

        if nearest is None:
            bar.setValue(0)
            bar_caption.setText("添加一场还没开始的考试后，这里会显示临近程度")
        else:
            ratio = max(0.0, min(1.0, 1 - nearest / max(1, remind.value())))
            bar.setValue(round(ratio * 1000))
            bar_caption.setText(f"最近一场还有 {nearest} 天（提醒阈值 {remind.value()} 天）")
        delete.setEnabled(table.currentRow() >= 0 and bool(visible_rows))

    def add_exam():
        title_text = name.text().strip()
        if not title_text:
            name.setFocus(); return
        records.append({"id": uuid.uuid4().hex, "name": title_text,
                        "kind": kind.currentText(), "place": place.text().strip(),
                        "date": exam_date.date().toString(DATE_FORMAT),
                        "time": exam_time.time().toString("HH:mm"),
                        "added": QDate.currentDate().toString(DATE_FORMAT)})
        _save_exams(records)
        name.clear(); refresh(); name.setFocus()

    def delete_exam():
        row_index = table.currentRow()
        if not 0 <= row_index < len(visible_rows): return
        record = visible_rows[row_index][0]
        records[:] = [item for item in records if item["id"] != record["id"]]
        _save_exams(records); refresh()

    add.clicked.connect(add_exam)
    name.returnPressed.connect(add_exam)
    delete.clicked.connect(delete_exam)
    remind.valueChanged.connect(lambda _: refresh())
    near.clicked.connect(lambda _: refresh())
    table.itemSelectionChanged.connect(
        lambda: delete.setEnabled(table.currentRow() >= 0 and bool(visible_rows)))
    refresh()
    return page


def _days_left(record):
    """考试的日期距离今天还有几天；日期非法时返回 None。"""
    value = QDate.fromString(str(record.get("date", "")), DATE_FORMAT)
    return QDate.currentDate().daysTo(value) if value.isValid() else None


def _countdown(days):
    """剩余天数的中文说法，今天 / N 天后 / 已结束都覆盖到。"""
    if days is None:
        return "日期待定"
    if days == 0:
        return "今天"
    if days > 0:
        return f"{days} 天后"
    return f"已结束 {-days} 天"


def _sorted_exams(records):
    """按日期排序：未结束的按临近程度升序在前，已结束的按时间倒序排在后面。"""
    def key(record):
        value = QDate.fromString(str(record.get("date", "")), DATE_FORMAT)
        if not value.isValid():
            return (2, 0, "")
        days = QDate.currentDate().daysTo(value)
        stamp = f'{record.get("date", "")} {record.get("time", "")}'
        return (1, -days, stamp) if days < 0 else (0, days, stamp)
    return sorted(records, key=key)


def _spin_int(value, low, high):
    box = QSpinBox()
    box.setRange(low, high); box.setValue(value)
    # 自带的上下箭头在深色输入框上很突兀，数值直接输入即可。
    box.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
    box.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return box


def _stat_card(caption):
    card = QFrame(); card.setObjectName("statCard")
    box = QVBoxLayout(card); box.setContentsMargins(16, 12, 16, 12); box.setSpacing(4)
    label = QLabel(caption); label.setObjectName("statCaption")
    value = QLabel("—"); value.setObjectName("statValue")
    box.addWidget(label); box.addWidget(value)
    return card, value


def _tiles(tiles, nearest, soon, total, past):
    values = (str(nearest) if nearest is not None else "—", str(soon), str(total), str(past))
    for (_card, value_label), text in zip(tiles, values):
        value_label.setText(text if total else "—")


def _field_box(label, field):
    """标签在上、控件在下的字段盒子，和其他模块的表单保持一致。"""
    box = QWidget(); box.setObjectName("fieldBox")
    box_layout = QVBoxLayout(box); box_layout.setContentsMargins(0, 0, 0, 0); box_layout.setSpacing(6)
    label_widget = QLabel(label); label_widget.setObjectName("formLabel")
    box_layout.addWidget(label_widget); box_layout.addWidget(field)
    return box


def _heading(title, subtitle):
    widget = QWidget(); layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 4); layout.setSpacing(4)
    a = QLabel(title); a.setObjectName("pageTitle")
    b = QLabel(subtitle); b.setObjectName("muted")
    layout.addWidget(a); layout.addWidget(b); return widget


def _load_exams():
    """读取考试记录；文件缺失或损坏时返回空清单，坏条目直接跳过。"""
    try:
        data = json.loads(EXAMS_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    records = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict): continue
        try:
            records.append({"id": str(item.get("id") or uuid.uuid4().hex),
                            "name": str(item.get("name") or "未命名考试"),
                            "kind": str(item.get("kind") or KINDS[-1]),
                            "place": str(item.get("place") or ""),
                            "date": str(item.get("date") or ""),
                            "time": str(item.get("time") or ""),
                            "added": str(item.get("added") or "")})
        except (TypeError, ValueError):
            continue
    return records


def _save_exams(records):
    EXAMS_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_json(EXAMS_FILE, records)
