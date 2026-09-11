"""生活费记账：设定本月预算，记下每一笔花销，随时看到预算还剩多少。"""

import json
import uuid
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDoubleSpinBox, QFrame, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QProgressBar, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)
from ui_widgets import DateDropdown, StyledComboBox, clip_to_rounded_frame
from utils import write_json

MODULE_INFO = {"name": "生活费记账", "icon": "¥"}
BUDGET_FILE = Path(__file__).resolve().parents[1] / "config" / "budget.json"
# 记账分类：改这一行就能换成自己的口径，历史数据里的旧分类照常显示。
CATEGORIES = ("餐饮", "交通", "购物", "学习", "娱乐", "住宿", "医疗", "其他")
# 还没设置预算时先按这个数字估算，页面上会明确提示是默认值。
DEFAULT_BUDGET = 1500.0
FORM_HINT = "金额要大于 0 才会记账；记错了就到下面的明细里选中再删除。"


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
    layout.addWidget(_heading("生活费记账", "记录每天的花销，随时看到本月还剩多少"))

    budget_value, items, saved = _load_budget()
    # 用列表包一层，闭包里才能把“预算是否已设置”改回来。
    configured = [saved]

    yield "正在构建生活费记账 · 预算设置…"
    budget_panel = QFrame(); budget_panel.setObjectName("contentPanel")
    budget_body = QVBoxLayout(budget_panel)
    budget_body.setContentsMargins(18, 16, 18, 16); budget_body.setSpacing(12)
    budget_title = QLabel("本月预算"); budget_title.setObjectName("sectionTitle")
    budget_body.addWidget(budget_title)
    budget_spin = _spin(budget_value, 0.0, 999999.0, 2, 50.0)
    budget_spin.setMinimumHeight(38)
    budget_note = QLabel(_budget_hint(saved, budget_value))
    budget_note.setObjectName("formHint"); budget_note.setWordWrap(True)
    save_budget = QPushButton("保存预算"); save_budget.setObjectName("completedButton")
    save_budget.setCursor(Qt.CursorShape.PointingHandCursor); save_budget.setFixedWidth(110)
    budget_row = QHBoxLayout(); budget_row.setSpacing(14)
    budget_row.addWidget(_field_box("每月预算（元）", budget_spin), 2)
    budget_row.addWidget(budget_note, 5)
    budget_row.addWidget(save_budget)
    budget_body.addLayout(budget_row)
    layout.addWidget(budget_panel)

    yield "正在构建生活费记账 · 记一笔…"
    form_panel = QFrame(); form_panel.setObjectName("contentPanel")
    form = QVBoxLayout(form_panel)
    form.setContentsMargins(18, 18, 18, 18); form.setSpacing(14)
    name = QLineEdit(); name.setPlaceholderText("花在哪里，例如：食堂午餐")
    category = StyledComboBox(); category.addItems(CATEGORIES)
    amount = _spin(20.0, 0.0, 999999.0, 2, 1.0)
    when = DateDropdown()
    # 年/月/日 三个下拉各自有最小内容宽度，日期盒子太窄就会把文字裁掉。
    when.setMinimumWidth(320)
    for field in (name, category, amount, when):
        field.setMinimumHeight(38)
    row = QHBoxLayout(); row.setSpacing(14)
    row.addWidget(_field_box("名称", name), 3)
    row.addWidget(_field_box("分类", category), 1)
    row.addWidget(_field_box("金额（元）", amount), 1)
    row.addWidget(_field_box("日期", when), 3)
    form.addLayout(row)
    hint = QLabel(FORM_HINT); hint.setObjectName("formHint"); hint.setWordWrap(True)
    form.addWidget(hint)
    add = QPushButton("记一笔"); add.setObjectName("primaryButton")
    add.setCursor(Qt.CursorShape.PointingHandCursor); add.setFixedWidth(120)
    actions = QHBoxLayout(); actions.addStretch(); actions.addWidget(add)
    form.addLayout(actions)
    layout.addWidget(form_panel)

    yield "正在构建生活费记账 · 明细…"
    list_panel = QFrame(); list_panel.setObjectName("contentPanel")
    body = QVBoxLayout(list_panel)
    body.setContentsMargins(18, 16, 18, 16); body.setSpacing(12)
    title = QLabel("本月明细"); title.setObjectName("sectionTitle")
    summary = QLabel("还没有记录"); summary.setObjectName("scheduleSummary")
    # 超支提醒做成一个不可点的状态按钮，配色由待办提醒角色统一给出。
    notice = QPushButton("预算充足"); notice.setObjectName("pendingButton")
    notice.setEnabled(False); notice.setMinimumWidth(140)
    delete = QPushButton("删除选中"); delete.setObjectName("dangerButton")
    delete.setEnabled(False); delete.setFixedWidth(110)
    toolbar = QHBoxLayout(); toolbar.setSpacing(12)
    toolbar.addWidget(title); toolbar.addWidget(summary); toolbar.addStretch()
    toolbar.addWidget(notice); toolbar.addWidget(delete)
    body.addLayout(toolbar)

    stats = QHBoxLayout(); stats.setSpacing(12)
    tiles = (_stat_card("本月预算"), _stat_card("已花"), _stat_card("剩余"),
             _stat_card("明细笔数"))
    for tile, _value in tiles:
        stats.addWidget(tile, 1)
    body.addLayout(stats)

    bar = QProgressBar(); bar.setRange(0, 1000); bar.setTextVisible(False)
    bar.setFixedHeight(8); bar.setValue(0)
    bar_caption = QLabel("设置预算并记一笔后，这里显示预算使用率"); bar_caption.setObjectName("hint")
    body.addWidget(bar_caption); body.addWidget(bar)

    category_title = QLabel("分类统计"); category_title.setObjectName("sectionTitle")
    body.addWidget(category_title)
    # 分组容器用透明的 fieldBox 角色，免得纯 QWidget 默认底色在白面板上压出条带。
    category_box = QWidget(); category_box.setObjectName("fieldBox")
    category_body = QVBoxLayout(category_box)
    category_body.setContentsMargins(0, 0, 0, 0); category_body.setSpacing(6)
    body.addWidget(category_box)

    table = QTableWidget(0, 4)
    table.setObjectName("budgetTable")
    table.setHorizontalHeaderLabels(("名称", "分类", "金额（元）", "日期"))
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    table.setShowGrid(False); table.setAlternatingRowColors(False)
    header = table.horizontalHeader()
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    for column in range(1, 4):
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(column, 120)
    header.setFixedHeight(40)
    table.verticalHeader().setDefaultSectionSize(38)
    # 金额和日期右对齐，表头跟着走，视线不用来回跳。
    for column in range(4):
        entry = table.horizontalHeaderItem(column)
        entry.setTextAlignment((Qt.AlignmentFlag.AlignRight if column >= 2
                                else Qt.AlignmentFlag.AlignLeft)
                               | Qt.AlignmentFlag.AlignVCenter)
    # 表头和单元格都会盖住外框圆角，各自按外框的圆角裁剪。
    table.setStyleSheet("""
        #budgetTable QHeaderView::section:first { border-top-left-radius: 12px; }
        #budgetTable QHeaderView::section:last { border-top-right-radius: 12px; }
    """)
    clip_to_rounded_frame(table, 11)
    clip_to_rounded_frame(header, 11, corners="tl,tr")
    empty = QLabel("还没有花销，填上面的表单就能记下第一笔")
    empty.setObjectName("emptyState"); empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
    body.addWidget(empty, 1); body.addWidget(table, 1)
    layout.addWidget(list_panel, 1)

    def refresh():
        budget = float(budget_spin.value())
        count = len(items)
        total = sum(item["amount"] for item in items)
        remaining = budget - total
        ratio = total / budget if budget > 0 else 0.0
        table.setRowCount(count)
        for row_index, item in enumerate(items):
            cells = (item["name"], item["category"], f'{item["amount"]:.2f}', item["date"])
            for column, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                if column >= 2:
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(row_index, column, cell)
        empty.setVisible(count == 0); table.setVisible(count > 0)
        summary.setText(f"{count} 笔 · 已花 ¥{total:.2f} · 预算 ¥{budget:.2f}"
                        if count else "还没有记录")
        bar.setValue(max(0, min(1000, round(ratio * 1000))))
        # 状态药丸按语义换主题角色：充足用已完成绿，接近或超出预算才用预警玫红。
        if total > budget:
            bar_caption.setText(f"预算使用率 100%，已花 ¥{total:.2f}，"
                                f"超出预算 ¥{total - budget:.2f}")
            notice.setText(f"已超支 ¥{total - budget:.2f}")
            _set_notice_role(notice, "pendingButton")
        else:
            bar_caption.setText(f"预算使用率 {ratio * 100:.0f}%"
                                f"（已花 ¥{total:.2f} / 预算 ¥{budget:.2f}）")
            watch = bool(count) and ratio >= 0.8
            notice.setText(f"已用 {ratio * 100:.0f}%，注意控制" if watch else "预算充足")
            _set_notice_role(notice, "pendingButton" if watch else "completedButton")
        _tiles(tiles, budget, total, remaining, count)
        budget_note.setText(_budget_hint(configured[0], budget))
        _render_categories(category_body, items, total)
        delete.setEnabled(table.currentRow() >= 0)

    def commit_budget():
        configured[0] = True
        _save_budget(float(budget_spin.value()), items)
        refresh()

    def add_record():
        text = name.text().strip()
        if not text:
            name.setFocus(); return
        value = float(amount.value())
        if value <= 0:
            hint.setText("金额要大于 0 才会记账，请重新填写。")
            amount.setFocus(); return
        items.append({"id": uuid.uuid4().hex, "name": text,
                      "category": category.currentText(), "amount": value,
                      "date": when.date().toString("yyyy-MM-dd")})
        _save_budget(float(budget_spin.value()), items)
        name.clear(); hint.setText(FORM_HINT); refresh(); name.setFocus()

    def delete_record():
        row_index = table.currentRow()
        if not 0 <= row_index < len(items):
            return
        items.pop(row_index)
        _save_budget(float(budget_spin.value()), items)
        refresh()

    add.clicked.connect(add_record)
    name.returnPressed.connect(add_record)
    delete.clicked.connect(delete_record)
    save_budget.clicked.connect(commit_budget)
    table.itemSelectionChanged.connect(lambda: delete.setEnabled(table.currentRow() >= 0))
    budget_spin.valueChanged.connect(lambda _: refresh())
    refresh()
    return page


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


def _set_notice_role(notice, role):
    """按语义换用主题角色：同一个状态药丸，绿=正常，玫红=需要提醒。"""
    if notice.objectName() == role:
        return
    notice.setObjectName(role)
    notice.style().unpolish(notice)
    notice.style().polish(notice)


def _tiles(tiles, budget, total, remaining, count):
    remaining_text = f"¥{remaining:.2f}" if remaining >= 0 else f"-¥{-remaining:.2f}"
    values = (f"¥{budget:.2f}", f"¥{total:.2f}", remaining_text, str(count))
    for (_card, value_label), text in zip(tiles, values):
        value_label.setText(text)


def _render_categories(container, items, total):
    """按分类汇总后从多到少排列，清掉旧行再重建。"""
    while container.count():
        child = container.takeAt(0)
        widget = child.widget()
        if widget is not None:
            # 先断开父子关系：deleteLater 要等事件循环空闲才生效，
            # 只靠它会让旧行在下一帧还压在原位，和新行叠成一片。
            widget.setParent(None)
            widget.deleteLater()
    if not items:
        label = QLabel("记下第一笔后，这里会按分类汇总")
        label.setObjectName("hint")
        container.addWidget(label)
        return
    totals = {}
    for item in items:
        totals[item["category"]] = totals.get(item["category"], 0.0) + item["amount"]
    for category, amount in sorted(totals.items(), key=lambda pair: pair[1], reverse=True):
        line = QWidget(); line.setObjectName("fieldBox")
        row = QHBoxLayout(line); row.setContentsMargins(0, 0, 0, 0); row.setSpacing(10)
        caption = QLabel(category); caption.setObjectName("matterTitle")
        share = QLabel(f"¥{amount:.2f}   {amount / total * 100 if total else 0:.0f}%")
        share.setObjectName("scheduleSummary")
        share.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(caption); row.addStretch(); row.addWidget(share)
        container.addWidget(line)


def _budget_hint(configured, value):
    if configured:
        return (f"当前本月预算 ¥{value:.2f}，改动金额后点“保存预算”写入文件；"
                "超支时会提示剩余为负。")
    return (f"还没有设置预算，暂按默认 {DEFAULT_BUDGET:g} 元估算。"
            "填入金额后点“保存预算”。")


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


def _load_budget():
    """读取预算文件，返回 (预算, 明细, 是否已设置)；坏条目直接跳过。"""
    try:
        data = json.loads(BUDGET_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    try:
        raw = float(data.get("budget", 0) or 0)
    except (TypeError, ValueError):
        raw = 0.0
    configured = raw > 0
    budget = raw if configured else DEFAULT_BUDGET
    items = []
    stored = data.get("items")
    for item in stored if isinstance(stored, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            amount = float(item.get("amount", 0))
        except (TypeError, ValueError):
            continue
        if amount <= 0:
            continue
        items.append({"id": str(item.get("id") or uuid.uuid4().hex),
                      "name": str(item.get("name", "")).strip() or "未命名花销",
                      "category": str(item.get("category", CATEGORIES[-1])),
                      "amount": amount,
                      "date": str(item.get("date", ""))})
    return budget, items, configured


def _save_budget(budget, items):
    BUDGET_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_json(BUDGET_FILE, {"budget": round(float(budget), 2), "items": items})
