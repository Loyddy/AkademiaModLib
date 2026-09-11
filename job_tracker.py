"""实习投递：记录每一次投递的公司、岗位与进度，按状态筛选跟进。"""

import json
import uuid
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QProgressBar, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)
from ui_widgets import DateDropdown, StyledComboBox, clip_to_rounded_frame
from utils import write_json

MODULE_INFO = {"name": "实习投递", "icon": "✈"}
JOB_FILE = Path(__file__).resolve().parents[1] / "config" / "job_applications.json"
# 投递流程从“已投递”一路走到结果，顺序也是界面下拉里的顺序。
STATUSES = ("已投递", "笔试", "面试", "已录用", "已拒绝")
CHANNELS = ("官网", "Boss直聘", "LinkedIn", "内推", "校园招聘", "其他")
# 还在流程里、需要继续盯着的状态。
PENDING_STATUSES = ("已投递", "笔试", "面试")


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
    layout.addWidget(_heading("实习投递", "把投出去的简历记下来，跟进每一步进度"))

    yield "正在构建实习投递 · 录入表单…"
    form_panel = QFrame(); form_panel.setObjectName("contentPanel")
    form = QVBoxLayout(form_panel)
    form.setContentsMargins(18, 18, 18, 18); form.setSpacing(14)
    company = QLineEdit(); company.setPlaceholderText("公司名称，例如：字节跳动")
    role = QLineEdit(); role.setPlaceholderText("岗位名称，例如：后端开发实习生")
    channel = StyledComboBox(); channel.addItems(CHANNELS)
    status = StyledComboBox(); status.addItems(STATUSES)
    applied = DateDropdown(QDate.currentDate())
    deadline = DateDropdown(QDate.currentDate().addDays(14))
    note = QLineEdit(); note.setPlaceholderText("备注：内推人、笔试时间、面试轮次等")
    for field in (company, role, channel, status, applied, deadline, note):
        field.setMinimumHeight(38)

    first = QHBoxLayout(); first.setSpacing(14)
    first.addWidget(_field_box("公司名称", company), 3)
    first.addWidget(_field_box("岗位名称", role), 3)
    first.addWidget(_field_box("投递渠道", channel), 1)
    second = QHBoxLayout(); second.setSpacing(14)
    second.addWidget(_field_box("投递日期", applied), 2)
    second.addWidget(_field_box("截止 / 跟进日期", deadline), 2)
    second.addWidget(_field_box("当前状态", status), 1)
    form.addLayout(first); form.addLayout(second)
    form.addWidget(_field_box("备注", note))
    hint = QLabel("公司和岗位不能为空；状态可以随时在下面表格里选中后更新，"
                  "投递记录保存在 config/job_applications.json。")
    hint.setObjectName("formHint"); hint.setWordWrap(True); form.addWidget(hint)
    add = QPushButton("记录投递"); add.setObjectName("primaryButton")
    add.setCursor(Qt.CursorShape.PointingHandCursor); add.setFixedWidth(120)
    actions = QHBoxLayout(); actions.addStretch(); actions.addWidget(add)
    form.addLayout(actions)
    layout.addWidget(form_panel)

    yield "正在构建实习投递 · 进度概览…"
    list_panel = QFrame(); list_panel.setObjectName("contentPanel")
    body = QVBoxLayout(list_panel)
    body.setContentsMargins(18, 16, 18, 16); body.setSpacing(12)
    title = QLabel("投递记录"); title.setObjectName("sectionTitle")
    summary = QLabel("还没有记录"); summary.setObjectName("scheduleSummary")
    filter_box = StyledComboBox(); filter_box.addItems(("全部状态",) + STATUSES)
    filter_box.setAccessibleName("状态筛选")
    delete = QPushButton("删除选中"); delete.setObjectName("dangerButton")
    delete.setEnabled(False); delete.setFixedWidth(110)
    toolbar = QHBoxLayout(); toolbar.setSpacing(12)
    toolbar.addWidget(title); toolbar.addWidget(summary); toolbar.addStretch()
    toolbar.addWidget(filter_box); toolbar.addWidget(delete)
    body.addLayout(toolbar)

    stats = QHBoxLayout(); stats.setSpacing(12)
    tiles = (_stat_card("投递总数"), _stat_card("进行中"),
             _stat_card("面试中"), _stat_card("已录用"))
    for tile, _value in tiles:
        stats.addWidget(tile, 1)
    body.addLayout(stats)

    bar = QProgressBar(); bar.setRange(0, 1000); bar.setTextVisible(False)
    bar.setFixedHeight(8); bar.setValue(0)
    bar_caption = QLabel("记录第一条投递后，这里会显示录用进度"); bar_caption.setObjectName("hint")
    body.addWidget(bar_caption); body.addWidget(bar)

    yield "正在构建实习投递 · 记录表格…"
    table = QTableWidget(0, 7)
    table.setObjectName("jobTable")
    table.setHorizontalHeaderLabels(("公司", "岗位", "渠道", "投递日期", "截止 / 跟进", "状态", "备注"))
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    table.setShowGrid(False); table.setAlternatingRowColors(False)
    header = table.horizontalHeader()
    # 公司、岗位容易被截断，留成可伸缩列；其余按内容定宽。
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
    for column, width in ((2, 92), (3, 104), (4, 104), (5, 84), (6, 150)):
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(column, width)
    header.setFixedHeight(40)
    # 单元格是左对齐的，表头跟着走，视线不用来回跳。
    for column in range(7):
        table.horizontalHeaderItem(column).setTextAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    table.verticalHeader().setDefaultSectionSize(38)
    # 表头和单元格都会盖住外框圆角，各自按外框的圆角裁剪。
    table.setStyleSheet("""
        #jobTable QHeaderView::section:first { border-top-left-radius: 12px; }
        #jobTable QHeaderView::section:last { border-top-right-radius: 12px; }
    """)
    clip_to_rounded_frame(table, 11)
    clip_to_rounded_frame(header, 11, corners="tl,tr")
    empty = QLabel("还没有投递记录，填上面的表单就能记下第一条")
    empty.setObjectName("muted"); empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
    body.addWidget(empty, 1); body.addWidget(table, 1)

    yield "正在构建实习投递 · 状态跟进…"
    follow = QHBoxLayout(); follow.setSpacing(12)
    follow_label = QLabel("更新选中记录的状态"); follow_label.setObjectName("formLabel")
    status_editor = StyledComboBox(); status_editor.addItems(STATUSES)
    status_editor.setMinimumHeight(38)
    apply_status = QPushButton("更新状态"); apply_status.setObjectName("primaryButton")
    apply_status.setCursor(Qt.CursorShape.PointingHandCursor)
    apply_status.setEnabled(False); apply_status.setFixedWidth(120)
    follow.addWidget(follow_label)
    follow.addWidget(status_editor, 1)
    follow.addStretch()
    follow.addWidget(apply_status)
    body.addLayout(follow)
    layout.addWidget(list_panel, 1)

    records = _load_jobs()
    shown = []

    def refresh():
        keyword = filter_box.currentText()
        shown[:] = [record for record in records
                    if keyword == "全部状态" or record["status"] == keyword]
        table.setRowCount(len(shown))
        for row_index, record in enumerate(shown):
            cells = (record["company"], record["role"], record["channel"],
                     record["applied"], record["deadline"], record["status"],
                     record["note"])
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                # 状态只用文字表达，颜色一律来自主题角色。
                item.setTextAlignment(Qt.AlignmentFlag.AlignLeft
                                      | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(row_index, column, item)
        table.setVisible(bool(shown)); empty.setVisible(not shown)
        if records and not shown:
            empty.setText("当前筛选下没有记录，换个状态看看")
        else:
            empty.setText("还没有投递记录，填上面的表单就能记下第一条")
        total = len(records)
        pending = sum(1 for record in records if record["status"] in PENDING_STATUSES)
        interviews = sum(1 for record in records if record["status"] == "面试")
        offers = sum(1 for record in records if record["status"] == "已录用")
        _tiles(tiles, total, pending, interviews, offers)
        if records:
            summary.setText(f"{total} 条记录 · 当前显示 {len(shown)} 条 · "
                            f"进行中 {pending} 条")
            bar.setValue(round(offers / total * 1000))
            bar_caption.setText(f"录用进度 {offers / total * 100:.0f}%"
                                f"（已录用 {offers} / 共 {total}）")
        else:
            summary.setText("还没有记录")
            bar.setValue(0)
            bar_caption.setText("记录第一条投递后，这里会显示录用进度")
        _sync_buttons()

    def _sync_buttons():
        selected = 0 <= table.currentRow() < len(shown)
        delete.setEnabled(selected); apply_status.setEnabled(selected)

    def selected_record():
        row_index = table.currentRow()
        return shown[row_index] if 0 <= row_index < len(shown) else None

    def add_record():
        company_text = company.text().strip()
        role_text = role.text().strip()
        if not company_text:
            company.setFocus(); return
        if not role_text:
            role.setFocus(); return
        records.append({"id": uuid.uuid4().hex,
                        "company": company_text, "role": role_text,
                        "channel": channel.currentText(),
                        "applied": applied.date().toString("yyyy-MM-dd"),
                        "deadline": deadline.date().toString("yyyy-MM-dd"),
                        "status": status.currentText(),
                        "note": note.text().strip()})
        _save_jobs(records)
        company.clear(); role.clear(); note.clear()
        refresh(); company.setFocus()

    def update_status():
        record = selected_record()
        if record is None: return
        record["status"] = status_editor.currentText()
        _save_jobs(records)
        row_index = table.currentRow()
        refresh()
        # 刷新后把选中行留在原地，方便连续改状态。
        if 0 <= row_index < len(shown):
            table.setCurrentCell(row_index, 0)

    def delete_record():
        record = selected_record()
        if record is None: return
        records.remove(record)
        # 删除后 refresh 会按当前选中行重新决定按钮状态（与成绩管理一致）。
        _save_jobs(records); refresh()

    add.clicked.connect(add_record)
    company.returnPressed.connect(add_record)
    role.returnPressed.connect(add_record)
    note.returnPressed.connect(add_record)
    delete.clicked.connect(delete_record)
    apply_status.clicked.connect(update_status)
    filter_box.currentTextChanged.connect(lambda _: refresh())
    table.itemSelectionChanged.connect(_sync_buttons)
    refresh()
    return page


def _stat_card(caption):
    card = QFrame(); card.setObjectName("statCard")
    box = QVBoxLayout(card); box.setContentsMargins(16, 12, 16, 12); box.setSpacing(4)
    label = QLabel(caption); label.setObjectName("statCaption")
    value = QLabel("—"); value.setObjectName("statValue")
    box.addWidget(label); box.addWidget(value)
    return card, value


def _tiles(tiles, total, pending, interviews, offers):
    values = (str(total), str(pending), str(interviews), str(offers))
    for (_card, value_label), text in zip(tiles, values):
        value_label.setText(text if total else "—")


def _field_box(label, field):
    """标签在上、控件在下的字段盒子，和其他模块的表单保持一致。"""
    box = QWidget(); box.setObjectName("fieldBox")
    box_layout = QVBoxLayout(box); box_layout.setContentsMargins(0, 0, 0, 0)
    box_layout.setSpacing(6)
    label_widget = QLabel(label); label_widget.setObjectName("formLabel")
    box_layout.addWidget(label_widget); box_layout.addWidget(field)
    return box


def _heading(title, subtitle):
    widget = QWidget(); layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 4); layout.setSpacing(4)
    a = QLabel(title); a.setObjectName("pageTitle")
    b = QLabel(subtitle); b.setObjectName("muted")
    layout.addWidget(a); layout.addWidget(b); return widget


def _load_jobs():
    try:
        data = json.loads(JOB_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    records = []
    for item in data if isinstance(data, list) else []:
        # 坏条目直接跳过，不让一条脏数据拖垮整张表。
        if not isinstance(item, dict): continue
        try:
            status = str(item.get("status", STATUSES[0]))
            records.append({
                "id": str(item.get("id") or uuid.uuid4().hex),
                "company": str(item.get("company", "未填写公司")),
                "role": str(item.get("role", "未填写岗位")),
                "channel": str(item.get("channel", CHANNELS[-1])),
                "applied": str(item.get("applied", "")),
                "deadline": str(item.get("deadline", "")),
                "status": status if status in STATUSES else STATUSES[0],
                "note": str(item.get("note", "")),
            })
        except (TypeError, ValueError):
            continue
    return records


def _save_jobs(records):
    JOB_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_json(JOB_FILE, records)
