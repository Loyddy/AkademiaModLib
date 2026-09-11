"""成绩管理：记录每门课的学分与成绩，实时算出加权均分、GPA 和补分目标。"""

import json
import uuid
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (QDoubleSpinBox, QFrame, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QProgressBar, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)
from ui_widgets import StyledComboBox, clip_to_rounded_frame
from utils import write_json

MODULE_INFO = {"name": "成绩管理", "icon": "∑"}
GRADES_FILE = Path(__file__).resolve().parents[1] / "config" / "grades.json"
# 常见的 4.0 分制换算表：按“分数下限”匹配，90 分及以上记 4.0。
# 不同学校算法不同，改这张表即可换成自己学校的口径。
GPA_BANDS = ((90, 4.0), (85, 3.7), (82, 3.3), (78, 3.0), (75, 2.7),
             (72, 2.3), (68, 2.0), (64, 1.5), (60, 1.0), (0, 0.0))
KINDS = ("必修", "选修", "通识", "实践")
MAX_GPA = 4.0


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
    layout.addWidget(_heading("成绩管理", "记下每门课的学分与成绩，随时看到加权均分和 GPA"))

    yield "正在构建成绩管理 · 录入表单…"
    form_panel = QFrame(); form_panel.setObjectName("contentPanel")
    form = QVBoxLayout(form_panel)
    form.setContentsMargins(18, 18, 18, 18); form.setSpacing(14)
    name = QLineEdit(); name.setPlaceholderText("课程名称，例如：高等数学（上）")
    credits = _spin(3.0, 0.5, 30.0, 1, 0.5)
    score = _spin(88.0, 0.0, 100.0, 1, 1.0)
    kind = StyledComboBox(); kind.addItems(KINDS)
    for field in (name, credits, score, kind):
        field.setMinimumHeight(38)
    row = QHBoxLayout(); row.setSpacing(14)
    row.addWidget(_field_box("课程名称", name), 3)
    row.addWidget(_field_box("课程类型", kind), 1)
    row.addWidget(_field_box("学分", credits), 1)
    row.addWidget(_field_box("成绩（百分制）", score), 1)
    form.addLayout(row)
    hint = QLabel("按常见 4.0 分制换算表计算绩点：90 分及以上 4.0，60 分以下不计。"
                  "换算是本地计算，换学校口径改本文件顶部的 GPA_BANDS 即可。")
    hint.setObjectName("formHint"); hint.setWordWrap(True); form.addWidget(hint)
    add = QPushButton("添加成绩"); add.setObjectName("primaryButton")
    add.setCursor(Qt.CursorShape.PointingHandCursor); add.setFixedWidth(120)
    actions = QHBoxLayout(); actions.addStretch(); actions.addWidget(add)
    form.addLayout(actions)
    layout.addWidget(form_panel)

    yield "正在构建成绩管理 · 成绩单…"
    list_panel = QFrame(); list_panel.setObjectName("contentPanel")
    body = QVBoxLayout(list_panel)
    body.setContentsMargins(18, 16, 18, 16); body.setSpacing(12)
    title = QLabel("成绩单"); title.setObjectName("sectionTitle")
    summary = QLabel("还没有记录"); summary.setObjectName("scheduleSummary")
    delete = QPushButton("删除选中"); delete.setObjectName("dangerButton")
    delete.setEnabled(False); delete.setFixedWidth(110)
    toolbar = QHBoxLayout(); toolbar.setSpacing(12)
    toolbar.addWidget(title); toolbar.addWidget(summary); toolbar.addStretch()
    toolbar.addWidget(delete)
    body.addLayout(toolbar)

    stats = QHBoxLayout(); stats.setSpacing(12)
    tiles = (_stat_card("GPA（4.0 制）"), _stat_card("加权均分"),
             _stat_card("已修学分"), _stat_card("课程门数"))
    for tile, _value in tiles:
        stats.addWidget(tile, 1)
    body.addLayout(stats)

    bar = QProgressBar(); bar.setRange(0, 1000); bar.setTextVisible(False)
    bar.setFixedHeight(8); bar.setValue(0)
    bar_caption = QLabel("添加成绩后这里会显示 GPA 完成度"); bar_caption.setObjectName("hint")
    body.addWidget(bar_caption); body.addWidget(bar)

    table = QTableWidget(0, 5)
    table.setObjectName("gradeTable")
    table.setHorizontalHeaderLabels(("课程", "类型", "学分", "成绩", "绩点"))
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    table.setShowGrid(False); table.setAlternatingRowColors(False)
    header = table.horizontalHeader()
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    for column in range(1, 5):
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(column, 92)
    header.setFixedHeight(40)
    table.verticalHeader().setDefaultSectionSize(38)
    # 数值列右对齐，表头跟着走，视线不用来回跳。
    for column in range(5):
        entry = table.horizontalHeaderItem(column)
        entry.setTextAlignment((Qt.AlignmentFlag.AlignRight if column >= 2
                                else Qt.AlignmentFlag.AlignLeft)
                               | Qt.AlignmentFlag.AlignVCenter)
    # 表头和单元格都会盖住外框圆角，各自按外框的圆角裁剪。
    table.setStyleSheet("""
        #gradeTable QHeaderView::section:first { border-top-left-radius: 12px; }
        #gradeTable QHeaderView::section:last { border-top-right-radius: 12px; }
    """)
    clip_to_rounded_frame(table, 11)
    clip_to_rounded_frame(header, 11, corners="tl,tr")
    empty = QLabel("还没有成绩，填上面的表单就能记下第一门课")
    empty.setObjectName("muted"); empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
    body.addWidget(empty, 1); body.addWidget(table, 1)
    layout.addWidget(list_panel, 1)

    yield "正在构建成绩管理 · 目标规划…"
    goal_panel = QFrame(); goal_panel.setObjectName("contentPanel")
    goal_body = QVBoxLayout(goal_panel)
    goal_body.setContentsMargins(18, 16, 18, 16); goal_body.setSpacing(12)
    goal_title = QLabel("目标规划"); goal_title.setObjectName("sectionTitle")
    goal_body.addWidget(goal_title)
    target = _spin(3.6, 0.0, MAX_GPA, 2, 0.1)
    remaining = _spin(20.0, 0.0, 120.0, 1, 1.0)
    for field in (target, remaining):
        field.setMinimumHeight(38)
    goal_row = QHBoxLayout(); goal_row.setSpacing(14)
    goal_row.addWidget(_field_box("目标 GPA", target), 1)
    goal_row.addWidget(_field_box("剩余学分", remaining), 1)
    need = QLabel("设置目标后，这里会算出剩余课程需要达到的水平")
    need.setObjectName("scheduleSummary"); need.setWordWrap(True)
    goal_row.addWidget(need, 3)
    goal_body.addLayout(goal_row)
    layout.addWidget(goal_panel)

    records = _load_grades()

    def refresh():
        count = len(records)
        total = sum(record["credits"] for record in records)
        weighted = sum(record["credits"] * record["score"] for record in records)
        average = weighted / total if total else 0.0
        points = sum(record["credits"] * grade_point(record["score"]) for record in records)
        gpa = points / total if total else 0.0
        table.setRowCount(count)
        for row_index, record in enumerate(records):
            cells = (record["name"], record["kind"], f'{record["credits"]:g}',
                     f'{record["score"]:g}', f'{grade_point(record["score"]):.1f}')
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column >= 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(row_index, column, item)
        empty.setVisible(count == 0); table.setVisible(count > 0)
        if count:
            summary.setText(f"{count} 门课 · {total:g} 学分 · 加权均分 {average:.1f} · GPA {gpa:.2f}")
            bar.setValue(round(gpa / MAX_GPA * 1000))
            bar_caption.setText(f"GPA 完成度 {gpa / MAX_GPA * 100:.0f}%（满分 {MAX_GPA:g}）")
        else:
            summary.setText("还没有记录")
            bar.setValue(0)
            bar_caption.setText("添加成绩后这里会显示 GPA 完成度")
        _tiles(tiles, gpa, average, total, count)
        delete.setEnabled(table.currentRow() >= 0)
        update_goal()

    def update_goal():
        total = sum(record["credits"] for record in records)
        points = sum(record["credits"] * grade_point(record["score"]) for record in records)
        wanted = target.value(); rest = remaining.value()
        if rest <= 0:
            need.setText("填入剩余学分后即可算出还需要多高的成绩")
        elif not records:
            need.setText("先添加已修课程，再计算剩余课程需要达到的水平")
        else:
            required = (wanted * (total + rest) - points) / rest
            if required <= 0:
                need.setText(f"剩余 {rest:g} 学分只要及格即可达到 GPA {wanted:.2f}")
            elif required > MAX_GPA:
                need.setText(f"剩余 {rest:g} 学分平均要 {required:.2f} 绩点，已经超出 "
                             f"{MAX_GPA:g}，需要提高目标或增加学分")
            else:
                need.setText(f"剩余 {rest:g} 学分平均需要 {required:.2f} 绩点"
                             f"（约 {_score_for(required)} 分以上）")

    def add_record():
        title_text = name.text().strip()
        if not title_text:
            name.setFocus(); return
        records.append({"id": uuid.uuid4().hex, "name": title_text,
                        "kind": kind.currentText(), "credits": float(credits.value()),
                        "score": float(score.value()),
                        "added": QDate.currentDate().toString("yyyy-MM-dd")})
        _save_grades(records)
        name.clear(); refresh(); name.setFocus()

    def delete_record():
        row_index = table.currentRow()
        if not 0 <= row_index < len(records): return
        records.pop(row_index); _save_grades(records); refresh()

    add.clicked.connect(add_record)
    name.returnPressed.connect(add_record)
    delete.clicked.connect(delete_record)
    table.itemSelectionChanged.connect(lambda: delete.setEnabled(table.currentRow() >= 0))
    target.valueChanged.connect(lambda _: update_goal())
    remaining.valueChanged.connect(lambda _: update_goal())
    refresh()
    return page


def grade_point(score):
    """百分制成绩 -> 4.0 分制绩点，按 GPA_BANDS 从高到低匹配。"""
    for floor, point in GPA_BANDS:
        if score >= floor:
            return point
    return 0.0


def _score_for(point):
    """反查至少拿到该绩点所需的分数下限。"""
    best = 0
    for floor, value in GPA_BANDS:
        if value >= point and floor > best:
            best = floor
    return best


def _spin(value, low, high, decimals, step):
    box = QDoubleSpinBox()
    box.setDecimals(decimals); box.setRange(low, high); box.setSingleStep(step)
    box.setValue(value)
    # 自带的上下箭头在深色输入框上很突兀，数值直接输入即可。
    box.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
    box.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return box


def _stat_card(caption):
    card = QFrame(); card.setObjectName("statCard")
    box = QVBoxLayout(card); box.setContentsMargins(16, 12, 16, 12); box.setSpacing(4)
    label = QLabel(caption); label.setObjectName("statCaption")
    value = QLabel("—"); value.setObjectName("statValue")
    box.addWidget(label); box.addWidget(value)
    return card, value


def _tiles(tiles, gpa, average, total, count):
    values = (f"{gpa:.2f}", f"{average:.1f}", f"{total:g}", str(count))
    for (_card, value_label), text in zip(tiles, values):
        value_label.setText(text if count else "—")


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


def _load_grades():
    try:
        data = json.loads(GRADES_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    records = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict): continue
        try:
            records.append({"id": str(item.get("id") or uuid.uuid4().hex),
                            "name": str(item.get("name", "未命名课程")),
                            "kind": str(item.get("kind", KINDS[0])),
                            "credits": max(0.0, float(item.get("credits", 0))),
                            "score": min(100.0, max(0.0, float(item.get("score", 0)))),
                            "added": str(item.get("added", ""))})
        except (TypeError, ValueError):
            continue
    return records


def _save_grades(records):
    GRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_json(GRADES_FILE, records)
