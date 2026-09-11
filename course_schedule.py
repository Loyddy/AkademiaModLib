"""课程表：支持 ICS 导入、手动添加和周视图持久化。"""

import json
from utils import write_json
import math
import re
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (QAbstractAnimation, QDate, QEasingCurve, QEvent, QObject,
    QPoint, QPointF, QPropertyAnimation, QRect, QRectF, QTime, QTimer, Qt, Signal, QVariantAnimation, Property)
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPen, QPolygon
from PySide6.QtWidgets import ( QFileDialog, QGridLayout, QHBoxLayout,
    QApplication, QFrame, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QStyledItemDelegate, QPushButton, QTableWidget, QTableWidgetItem, QGraphicsEffect,
    QVBoxLayout, QWidget, QScrollArea)
from ui_widgets import StyledComboBox, DateDropdown, TimeDropdown, clip_to_rounded_frame

from utils import PropertyAnimation as QPropertyAnimation, VariantAnimation as QVariantAnimation

MODULE_INFO = {"name": "课程表", "icon": "◷"}
DATA_FILE = Path(__file__).resolve().parents[1] / "config" / "courses.json"
SETTINGS_FILE = Path(__file__).resolve().parents[1] / "config" / "course_settings.json"
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


class SmoothTableWidget(QTableWidget):
    """用像素和缓动动画处理滚轮，避免按整行跳动。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._scroll_animation = QPropertyAnimation(self.verticalScrollBar(), b"value", self)

    def wheelEvent(self, event):
        delta = event.pixelDelta().y()
        if not delta:
            delta = event.angleDelta().y() / 120 * 72
        if not delta:
            event.ignore()
            return
        scrollbar = self.verticalScrollBar()
        current = scrollbar.value()
        if event.pixelDelta().y():
            self._scroll_animation.stop()
            scrollbar.setValue(round(current - delta))
            event.accept()
            return
        base = current
        if self._scroll_animation.state() != QAbstractAnimation.State.Stopped:
            pending = int(self._scroll_animation.endValue()) - current
            if pending * delta < 0:
                base = int(self._scroll_animation.endValue())
        target = max(scrollbar.minimum(), min(scrollbar.maximum(), round(base - delta)))
        self._scroll_animation.stop()
        self._scroll_animation.setStartValue(current)
        self._scroll_animation.setEndValue(target)
        self._scroll_animation.setDuration(180)
        self._scroll_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._scroll_animation.start()
        event.accept()

    def mousePressEvent(self, event):
        if self._scroll_animation and self._scroll_animation.state() != QAbstractAnimation.State.Stopped:
            self._scroll_animation.stop()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        """课程表不进入单元格编辑，也不因双击自动定位。"""
        event.accept()

    def scrollTo(self, index, hint=None):
        """禁止 Qt 因子控件点击而把视图自动定位到某个单元格。"""
        return


class RoundedTableWidget(SmoothTableWidget):
    """课程表容器，保留独立类型以兼容现有创建逻辑。"""

    def resizeEvent(self, event):
        super().resizeEvent(event)


class CourseCellDelegate(QStyledItemDelegate):
    """给有课的格子绘制清晰的高亮边框。"""

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        if index.column() == 0 or not index.data():
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#9b87ca"), 2))
        painter.drawRoundedRect(option.rect.adjusted(1, 1, -1, -1), 5, 5)
        painter.restore()


class CurrentDayHeader(QHeaderView):
    """Keep today's original colors and dim the other weekday columns."""

    def _band_path(self):
        # 表头横条的上沿要跟表格圆角对齐：上圆下方。
        rect = QRectF(self.rect())
        radius = min(14.0, rect.width() / 2, rect.height() / 2)
        path = QPainterPath()
        path.moveTo(rect.left(), rect.bottom())
        path.lineTo(rect.left(), rect.top() + radius)
        path.quadTo(rect.left(), rect.top(), rect.left() + radius, rect.top())
        path.lineTo(rect.right() - radius, rect.top())
        path.quadTo(rect.right(), rect.top(), rect.right(), rect.top() + radius)
        path.lineTo(rect.right(), rect.bottom())
        path.closeSubpath()
        return path

    def paintSection(self, painter, rect, logical_index):
        painter.save()
        painter.setClipPath(self._band_path(), Qt.ClipOperation.IntersectClip)
        today = logical_index == datetime.now().isoweekday()
        painter.fillRect(rect, QColor("#f1e9fb" if today else "#faf8fe"))
        painter.setPen(QColor("#6a4f9e" if today else "#8b7bb5"))
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        title = "时间" if logical_index == 0 else WEEKDAYS[logical_index - 1]
        if today:
            title += " · 今天"
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, title)
        painter.setPen(QColor("#e9e1f7"))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        painter.restore()


def _resize_band_height(rect):
    # Leave at least the middle half available for moving short courses.
    return max(1, min(16, rect.height() // 4))


def _resize_strip(rect, bottom=False):
    band = _resize_band_height(rect)
    inset = min(5, max(0, (rect.width() - 1) // 4))
    height = max(1, min(8, band - 2))
    margin = min(3, max(0, band - height))
    y = rect.bottom() - margin - height + 1 if bottom else rect.top() + margin
    return QRect(rect.left() + inset, y, max(1, rect.width() - 2 * inset), height)


class CourseDetailsBubble(QFrame):
    """A dismissible popup anchored to the visible part of a course card."""

    def __init__(self, parent):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._arrow_left = True
        self._arrow_y = 40
        self._reveal_origin = QPoint()
        self._reveal_target = QPoint()
        self._reveal_animation = QVariantAnimation(self)
        self._reveal_animation.setStartValue(0.0)
        self._reveal_animation.setEndValue(1.0)
        self._reveal_animation.setDuration(180)
        self._reveal_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._reveal_animation.valueChanged.connect(self._advance_reveal)
        parent.window().installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Hide):
            self.hide()
        return super().eventFilter(watched, event)

    def _advance_reveal(self, value):
        progress = float(value)
        offset = self._reveal_target - self._reveal_origin
        self.move(self._reveal_origin + QPoint(round(offset.x() * progress),
                                               round(offset.y() * progress)))
        self.setWindowOpacity(progress)

    def hideEvent(self, event):
        self._reveal_animation.stop()
        self.setWindowOpacity(1.0)
        super().hideEvent(event)

    def show_at(self, anchor):
        self._reveal_animation.stop()
        screen = QApplication.screenAt(anchor.center()) or self.parentWidget().screen()
        bounds = screen.availableGeometry().adjusted(8, 8, -8, -8)
        self.setFixedWidth(min(360, bounds.width()))
        self.adjustSize()
        self._arrow_left = anchor.right() + self.width() + 8 <= bounds.right()
        x = anchor.right() + 4 if self._arrow_left else anchor.left() - self.width() - 4
        x = max(bounds.left(), min(x, bounds.right() - self.width() + 1))
        y = max(bounds.top(), min(anchor.center().y() - self.height() // 2,
                                  bounds.bottom() - self.height() + 1))
        self._arrow_y = max(24, min(self.height() - 24, anchor.center().y() - y))
        self._reveal_target = QPoint(x, y)
        start_x = max(bounds.left(), min(x + (-8 if self._arrow_left else 8),
                                        bounds.right() - self.width() + 1))
        self._reveal_origin = QPoint(start_x, y)
        self.move(self._reveal_origin)
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self.setFocus()
        self._reveal_animation.start()
        self.update()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            event.accept()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(9, 1, self.width() - 18, self.height() - 2), 12, 12)
        arrow = QPainterPath()
        x = 9 if self._arrow_left else self.width() - 9
        tip = 1 if self._arrow_left else self.width() - 1
        arrow.moveTo(x, self._arrow_y - 9)
        arrow.lineTo(tip, self._arrow_y)
        arrow.lineTo(x, self._arrow_y + 9)
        arrow.closeSubpath()
        painter.setPen(QPen(QColor("#c9b8e3"), 1))
        painter.setBrush(QColor("#ffffff"))
        painter.drawPath(path.united(arrow))


class ScheduleTimeAxis(QWidget):
    """时间标注以网格边界为中心，而非放在半小时时段中央。"""

    def __init__(self, start_minute, end_minute, row_height=36, parent=None):
        super().__init__(parent)
        self.setAutoFillBackground(True)
        self.start_minute = start_minute
        self.end_minute = end_minute
        self.row_height = row_height
        self.setFixedHeight((end_minute - start_minute) * row_height // 30 + row_height)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#faf8fe"))
        painter.setPen(QColor("#9a90ae"))
        font = painter.font()
        font.setPointSize(9)
        painter.setFont(font)
        label_height = painter.fontMetrics().height()
        for minute in range(self.start_minute, self.end_minute + 1, 30):
            y = self.row_height / 2 + (minute - self.start_minute) * self.row_height / 30
            rect = QRectF(0, y - label_height / 2, self.width(), label_height)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, _format_clock(minute))


class DayScheduleWidget(QWidget):
    """绘制一天的连续时间轴，支持长课与重叠课程。"""
    courseSelected = Signal(object)
    courseClicked = Signal(object)

    def __init__(self, courses, start_minute, end_minute, row_height=36, endpoint_height=36, parent=None, weekday_index=None):
        super().__init__(parent)
        self.courses = courses
        self.weekday_index = weekday_index
        self.start_minute = start_minute
        self.end_minute = end_minute
        self.row_height = row_height
        self.endpoint_height = endpoint_height
        self.top_padding = endpoint_height / 2
        self.selected_course = None
        self._overlay_course = None
        self.setFixedHeight((end_minute - start_minute) * row_height // 30 + endpoint_height)
        self._segments = []
        self.setMouseTracking(True)
        self._landing_origins = {}
        self._landing_progress = 1.0
        self._landing_animation = QVariantAnimation(self)
        self._landing_animation.setDuration(380)
        self._landing_animation.setStartValue(0.0)
        self._landing_animation.setEndValue(1.0)
        self._landing_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._landing_animation.valueChanged.connect(self._update_landing)
        self._landing_animation.finished.connect(self._finish_landing)
        self._bounce_course = None
        self._bounce_scale = 1.0
        self._bounce_animation = QVariantAnimation(self)
        self._bounce_animation.setDuration(420)
        self._bounce_animation.setStartValue(1.0)
        self._bounce_animation.setKeyValueAt(0.20, 0.88)
        self._bounce_animation.setKeyValueAt(0.52, 1.045)
        self._bounce_animation.setKeyValueAt(0.76, 0.985)
        self._bounce_animation.setEndValue(1.0)
        self._bounce_animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._bounce_animation.valueChanged.connect(self._update_bounce)

    def _update_landing(self, value):
        self._landing_progress = float(value)
        self.update()

    def _finish_landing(self):
        self._landing_origins.clear()
        self._landing_progress = 1.0
        self.update()

    def stop_landing(self):
        self._landing_animation.stop()
        self._finish_landing()

    def animate_landing(self, origins):
        self.stop_bounce()
        self._landing_animation.stop()
        self._landing_origins = origins
        self._landing_progress = 0.0
        self._landing_animation.start()

    def _paint_rect(self, course, target):
        origin = self._landing_origins.get(id(course))
        if origin is None:
            return QRect(target)
        progress = self._landing_progress
        return QRect(*(round(a + (b - a) * progress) for a, b in zip(
            (origin.x(), origin.y(), origin.width(), origin.height()),
            (target.x(), target.y(), target.width(), target.height()))))

    def visible_boxes(self):
        self._layout_boxes()
        return {id(course): self._paint_rect(course, rect) for course, rect in self._segments}

    def _update_bounce(self, value):
        self._bounce_scale = float(value)
        self.update()

    def bounce_course(self, course):
        self._bounce_animation.stop()
        self._bounce_course = course
        self._bounce_scale = 1.0
        self._bounce_animation.start()

    def stop_bounce(self):
        self._bounce_animation.stop()
        self._bounce_course = None
        self._bounce_scale = 1.0
        self.update()

    def set_selected(self, course):
        self.selected_course = course
        self.update()

    def _layout_boxes(self):
        intervals = []
        for index, course in enumerate(self.courses):
            start, end, _ = _course_start_end(course)
            if start is None:
                continue
            start = max(start, self.start_minute)
            end = min(end, self.end_minute)
            if start < end:
                intervals.append((start, end, index, course))
        # Stable input order breaks ties, rather than process-specific object IDs.
        intervals.sort(key=lambda item: item[:3])
        groups = []
        group_end = self.start_minute
        for item in intervals:
            start, end, _, _ = item
            # Touching endpoints are not conflicts.
            if not groups or start >= group_end:
                groups.append([])
                group_end = end
            groups[-1].append(item)
            group_end = max(group_end, end)

        segments = []
        for group in groups:
            lane_ends = []
            assigned = []
            for start, end, _, course in group:
                lane = next((i for i, last_end in enumerate(lane_ends)
                             if last_end <= start), len(lane_ends))
                if lane == len(lane_ends):
                    lane_ends.append(end)
                else:
                    lane_ends[lane] = end
                assigned.append((course, start, end, lane))
            # A course keeps the same lane and width for its entire duration.
            count = len(lane_ends)
            inset = min(4, max(0, (self.width() - count) / 2))
            available = max(0, self.width() - inset * 2)
            gap = min(4, max(0, math.floor((available - count) / max(1, count - 1))))
            lane_width = max(0, (available - gap * (count - 1)) / count)
            for course, start, end, lane in assigned:
                x = inset + lane * (lane_width + gap)
                y = self.top_padding + (start - self.start_minute) * self.row_height / 30
                height = (end - start) * self.row_height / 30
                vertical_inset = min(4, max(0, (height - 1) / 2))
                rect = QRect(round(x), round(y + vertical_inset),
                             max(0, round(x + lane_width) - round(x)),
                             max(1, round(y + height - vertical_inset) - round(y + vertical_inset)))
                segments.append((course, rect))
        self._segments = segments

    def paintEvent(self, event):
        try:
            self._layout_boxes()
        except (AttributeError, TypeError, ValueError, RuntimeError):
            self._segments = []
        painter = QPainter(self); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        now = datetime.now()
        is_today = self.weekday_index == now.weekday()
        painter.fillRect(self.rect(), QColor("#faf8ff" if is_today else "#ffffff"))
        for row in range((self.end_minute - self.start_minute) // 30 + 1):
            y = round(self.top_padding + row * self.row_height)
            painter.setPen(QPen(QColor("#e9e3f1" if row % 2 == 0 else "#f3eff8"), 1))
            painter.drawLine(0, y, self.width(), y)
        text_rects = {}
        grouped = {}
        for course, rect in self._segments:
            if course is self._overlay_course:
                continue
            grouped.setdefault(id(course), (course, []))[1].append(self._paint_rect(course, rect))
        # Animate only the painted card; retain stable hit boxes for dragging.
        for course, rects in grouped.values():
            if course is self._bounce_course and self._bounce_scale != 1.0:
                bounds = QRect(rects[0])
                for rect in rects[1:]:
                    bounds = bounds.united(rect)
                center = bounds.center()
                scale = self._bounce_scale
                rects[:] = [QRect(
                    round(center.x() + (rect.x() - center.x()) * scale),
                    round(center.y() + (rect.y() - center.y()) * scale),
                    max(2, round(rect.width() * scale)),
                    max(2, round(rect.height() * scale)),
                ) for rect in rects]
        for course, rects in grouped.values():
            selected = course is self.selected_course
            fill, accent = "#ede7fa", "#7960ac"
            painter.setBrush(QColor(accent if selected else fill))
            painter.setPen(QPen(QColor(accent if selected else fill).darker(108), 1))
            rects.sort(key=lambda rect: rect.top())
            if len(rects) == 1:
                rect = rects[0]
                radius = min(9, max(2, rect.width() // 5), max(2, rect.height() // 5))
                painter.drawRoundedRect(rect, radius, radius)
            else:
                polygon_points = [QPoint(rects[0].left(), rects[0].top())]
                for index, rect in enumerate(rects):
                    polygon_points.append(QPoint(rect.left(), rect.bottom()))
                    if index + 1 < len(rects):
                        polygon_points.append(QPoint(rects[index + 1].left(), rects[index + 1].top()))
                polygon_points.append(QPoint(rects[-1].right(), rects[-1].bottom()))
                for index in range(len(rects) - 1, -1, -1):
                    rect = rects[index]
                    polygon_points.append(QPoint(rect.right(), rect.top()))
                    if index:
                        polygon_points.append(QPoint(rects[index - 1].right(), rects[index - 1].bottom()))
                try:
                    painter.drawPath(_rounded_polygon(polygon_points))
                except (TypeError, RuntimeError, ValueError):
                    painter.drawPolygon(QPolygon(polygon_points))
            key = id(course)
            largest = max(rects, key=lambda rect: rect.width() * rect.height())
            if key not in text_rects or largest.width() * largest.height() > text_rects[key][0].width() * text_rects[key][0].height():
                text_rects[key] = (largest, course)
        base_font = painter.font()
        for rect, course in text_rects.values():
            painter.setPen(QColor("#ffffff" if course is self.selected_course else "#2d2738"))
            text_inset = _resize_band_height(rect)
            text_rect = rect.adjusted(6, text_inset, -6, -text_inset)
            if text_rect.isEmpty():
                continue
            font = type(base_font)(base_font)
            font.setBold(True)
            text = _fit_course_text(_course_text(course), font, text_rect.width(), text_rect.height())
            painter.save()
            painter.setClipRect(text_rect)
            painter.setFont(font)
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, text)
            painter.restore()
        for course, rects in grouped.values():
            if course is not self.selected_course:
                continue
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#d9ccef"))
            first = min(rects, key=lambda r: r.top())
            last = max(rects, key=lambda r: r.bottom())
            for rect, bottom in ((first, False), (last, True)):
                strip = _resize_strip(rect, bottom)
                radius = min(4, strip.height() / 2)
                painter.drawRoundedRect(strip, radius, radius)
        minutes = now.hour * 60 + now.minute + (now.second + now.microsecond / 1_000_000) / 60
        if is_today and self.start_minute <= minutes <= self.end_minute:
            y = self.top_padding + (minutes - self.start_minute) * self.row_height / 30
            # Draw last so the current-time marker remains visible over courses.
            pulse = (math.sin(time.monotonic() * math.tau / 3) + 1) / 2
            painter.setPen(QPen(QColor(199, 86, 126, round(28 + 30 * pulse)),
                                6 + 3 * pulse, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(QPointF(5, y), QPointF(self.width() - 5, y))
            painter.setPen(QPen(QColor("#c7567e"), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(QPointF(5, y), QPointF(self.width() - 5, y))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#c7567e"))
            painter.setBrush(QColor(199, 86, 126, round(25 + 25 * pulse)))
            painter.drawEllipse(QPointF(5, y), 5 + 2 * pulse, 5 + 2 * pulse)
            painter.setBrush(QColor("#c7567e"))
            painter.drawEllipse(QPointF(5, y), 3.5, 3.5)
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            for course, rect in reversed(self._segments):
                if rect.contains(event.position().toPoint()):
                    self.bounce_course(course)
                    self.courseSelected.emit(course)
                    event.accept()
                    return
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        event.accept()


class DragPreviewEffect(QGraphicsEffect):
    """Rotate the preview visually without changing its snapped geometry."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._angle = 0.0

    def get_angle(self):
        return self._angle

    def set_angle(self, value):
        self._angle = float(value)
        self.update()

    angle = Property(float, get_angle, set_angle)

    def boundingRectFor(self, rect):
        # Reserve room for rotated corners, including tall course cards.
        margin = max(rect.width(), rect.height()) * 0.13 + 3
        return rect.adjusted(-margin, -margin, margin, margin)

    def draw(self, painter):
        center = self.sourceBoundingRect().center()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setOpacity(0.68)
        painter.translate(center)
        painter.rotate(self._angle)
        painter.translate(-center)
        self.drawSource(painter)
        painter.restore()


class ScheduleDragController(QObject):
    """Keep edits provisional until release; animate the snapped landing card."""

    # 日期列之间只有 1px 分隔线：逐像素拖动跨列时光标会正好落进这条缝。
    # 判定按精确边界算的话，这一步会被当成“不在网格内”，预览消失一帧再飞回来，
    # 看起来就是跨日拖动时闪一下，所以命中判定留一点容差。
    COLUMN_GAP_TOLERANCE = 4
    # 贴着表格边缘拖动时允许光标越界这么多像素，避免预览突然消失。
    EDGE_MARGIN = 14

    def __init__(self, table, commit, parent=None):
        super().__init__(parent)
        self.table = table
        self.commit = commit
        self.days = []
        self.drag = None
        self.preview = QLabel(table.viewport())
        self.preview.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setWordWrap(True)
        self.preview.setStyleSheet(
            "background: rgba(121,98,170,225); color: white; border: 2px solid #cbb7f3;"
            "border-radius: 9px; padding: 2px; font-weight: 600;")
        self.preview.hide()
        self.preview_effect = DragPreviewEffect(self.preview)
        self.preview.setGraphicsEffect(self.preview_effect)
        self.tilt_animation = QPropertyAnimation(self.preview_effect, b"angle", self)
        self.tilt_animation.setDuration(100)
        self.tilt_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.tilt_idle = QTimer(self)
        self.tilt_idle.setSingleShot(True)
        self.tilt_idle.setInterval(80)
        self.tilt_idle.timeout.connect(lambda: self.set_tilt(0.0))
        self.hint = QLabel(table.viewport())
        self.hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hint.setStyleSheet("background: #493663; color: white; border-radius: 7px; padding: 6px;")
        self.hint.hide()
        self.animation = QPropertyAnimation(self.preview, b"geometry", self)
        self.animation.setDuration(280)
        self.animation.setEasingCurve(QEasingCurve.Type.OutQuart)
        self.animation.valueChanged.connect(lambda _: self.position_hint())
        self.landing_day = None
        self.animation.finished.connect(self.finish_landing)
        self.timer = QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.auto_scroll)
        QApplication.instance().installEventFilter(self)

    def attach(self, days):
        self.cancel()
        self.days = days

    def finish_landing(self):
        if self.landing_day is not None:
            self.landing_day._overlay_course = None
            self.landing_day.update()
            self.landing_day = None
            self.preview.hide()

    def set_tilt(self, angle):
        self.tilt_animation.stop()
        self.tilt_animation.setStartValue(self.preview_effect.angle)
        self.tilt_animation.setEndValue(angle)
        self.tilt_animation.start()

    def hit(self, widget, point):
        widget._layout_boxes()
        for course, rect in reversed(widget._segments):
            if rect.contains(point):
                band = _resize_band_height(rect)
                mode = ("start" if point.y() < rect.top() + band else
                        "end" if point.y() > rect.bottom() - band else "move")
                return course, mode
        return None

    def _day_at(self, local):
        """返回光标所在的日期列；落在列间分隔线里时取最近的一列。

        精确边界判断会让光标正好压在 1px 分隔线上时判定为“没有目标列”，
        跨日拖动就会闪一下，所以这里留 COLUMN_GAP_TOLERANCE 的容差。
        """
        nearest = None
        for day in self.days:
            pos = day.mapTo(self.table.viewport(), QPoint())
            if pos.x() <= local.x() < pos.x() + day.width():
                return day
            distance = min(abs(local.x() - pos.x()), abs(local.x() - (pos.x() + day.width())))
            if nearest is None or distance < nearest[0]:
                nearest = (distance, day)
        return nearest[1] if nearest is not None and nearest[0] <= self.COLUMN_GAP_TOLERANCE else None

    def eventFilter(self, watched, event):
        kind = event.type()
        if self.drag and kind == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            self.cancel()
            return True
        if self.drag and kind == QEvent.Type.ApplicationDeactivate:
            self.cancel()
        if self.drag and kind == QEvent.Type.MouseMove:
            if not event.buttons() & Qt.MouseButton.LeftButton:
                self.cancel()
                return False
            self.drag['global'] = event.globalPosition().toPoint()
            self.update_drag()
            return True
        if self.drag and kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            drag = self.drag
            origins = {day: day.visible_boxes() for day in self.days}
            preview_rect = QRect(self.preview.geometry())
            self.cancel()
            if drag['active'] and drag.get('valid'):
                self.commit(drag['course'], *drag['target'])
                # Animate from the last visible preview to the actual saved layout,
                # including any columns that must make room for this course.
                for day in self.days:
                    day._layout_boxes()
                    old_boxes = origins[day]
                    if any(course is drag['course'] for course, _ in day._segments):
                        position = day.mapTo(self.table.viewport(), QPoint())
                        old_boxes[id(drag['course'])] = preview_rect.translated(-position)
                        self.landing_day = day
                        day._overlay_course = drag['course']
                        target_rect = day.visible_boxes()[id(drag['course'])].translated(position)
                        self.preview.setGeometry(preview_rect)
                        self.preview.show()
                        self.preview.raise_()
                        self.animation.setStartValue(preview_rect)
                        self.animation.setEndValue(target_rect)
                        self.animation.start()
                    day.animate_landing(old_boxes)
            elif not drag['active'] and drag['mode'] == 'move':
                released = drag['source'].mapFromGlobal(event.globalPosition().toPoint())
                hit = self.hit(drag['source'], released)
                if hit and hit[0] is drag['course']:
                    drag['source'].courseClicked.emit(drag['course'])
            return False
        if watched not in self.days:
            return False
        if kind == QEvent.Type.MouseMove:
            hit = self.hit(watched, event.position().toPoint())
            watched.setCursor(Qt.CursorShape.SizeVerCursor if hit and hit[1] != 'move'
                              else Qt.CursorShape.OpenHandCursor if hit else Qt.CursorShape.ArrowCursor)
        elif kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            hit = self.hit(watched, event.position().toPoint())
            if hit:
                self.cancel()
                for day in self.days:
                    day.stop_landing()
                course, mode = hit
                start, end, _ = _course_start_end(course)
                point = event.globalPosition().toPoint()
                self.drag = dict(course=course, mode=mode, start=start, end=end,
                                 origin=point, **{'global': point}, source=watched, active=False)
                watched.bounce_course(course)
                watched.courseSelected.emit(course)
                return True
        return False

    def update_drag(self):
        drag = self.drag
        if not drag:
            return
        point = drag['global']
        movement = point - drag.get('last_tilt_point', drag['origin'])
        drag['last_tilt_point'] = QPoint(point)
        if not drag['active']:
            if (point - drag['origin']).manhattanLength() < QApplication.startDragDistance():
                return
            drag['active'] = True
            # The bounce changes painting only, so it can finish while the
            # drag continues to use the unchanged course geometry.
            self.timer.start()
            drag['source'].setCursor(Qt.CursorShape.ClosedHandCursor)
        if not movement.isNull():
            direction = movement.x() if abs(movement.x()) >= abs(movement.y()) else movement.y()
            self.set_tilt(math.copysign(min(6.0, 2.0 + abs(direction) * 0.12), direction))
            self.tilt_idle.start()
        viewport = self.table.viewport()
        local = viewport.mapFromGlobal(point)
        target = self._day_at(local)
        # 光标短暂越过表格边缘时不要立刻判定为无效：预览会消失一帧再出现。
        reach = QRect(viewport.rect()).adjusted(-self.EDGE_MARGIN, -self.EDGE_MARGIN,
                                                self.EDGE_MARGIN, self.EDGE_MARGIN)
        drag['valid'] = target is not None and reach.contains(local)
        if not drag['valid']:
            self.preview.hide()
            self.hint.hide()
            return
        if drag['mode'] != 'move':
            target = drag['source']
        source = drag['source']
        origin_y = source.mapFromGlobal(drag['origin']).y()
        # Account for scrolling since press by storing the initial content coordinate.
        origin_y = drag.setdefault('origin_y', origin_y)
        delta = (target.mapFromGlobal(point).y() - origin_y) * 30 / target.row_height
        start, end = _drag_times(drag['start'], drag['end'], delta, drag['mode'],
                                 target.start_minute, target.end_minute)
        drag['target'] = (WEEKDAYS[self.days.index(target)], start, end)
        pos = target.mapTo(viewport, QPoint())
        rect = QRect(pos.x() + 4, round(pos.y() + target.top_padding + (start - target.start_minute) * target.row_height / 30 + 4),
                     max(10, target.width() - 8), max(8, round((end - start) * target.row_height / 30 - 8)))
        # 只在拖动开始时把预览对齐到原卡片；之后一律沿当前位置继续动画，
        # 否则任何一次短暂隐藏都会让预览“闪回”到出发的那一列。
        if not drag.get('positioned'):
            source_rect = source.visible_boxes()[id(drag['course'])]
            self.preview.setGeometry(source_rect.translated(source.mapTo(viewport, QPoint())))
            drag['positioned'] = True
        if self.animation.endValue() != rect or not self.preview.isVisible():
            self.animation.stop()
            self.animation.setStartValue(self.preview.geometry())
            self.animation.setEndValue(rect)
            self.animation.start()
        self.preview.setText(_course_text(drag['course']))
        self.preview.show()
        self.preview.raise_()
        self.hint.setText(f"{drag['target'][0]}  {_format_clock(start)}–{_format_clock(end)}  ·  Esc 取消")
        self.hint.adjustSize()
        self.position_hint()
        self.hint.show()
        self.hint.raise_()

    def position_hint(self):
        rect = self.preview.geometry()
        self.hint.move(max(0, min(rect.x(), self.table.viewport().width() - self.hint.width())),
                       max(0, min(rect.y() - self.hint.height() - 5, self.table.viewport().height() - self.hint.height())))

    def auto_scroll(self):
        if not self.drag:
            return
        viewport = self.table.viewport()
        point = viewport.mapFromGlobal(self.drag['global'])
        # 贴着边缘拖动时允许光标略微越界，滚动不会因为一像素的抖动中断。
        reach = QRect(viewport.rect()).adjusted(-self.EDGE_MARGIN, -self.EDGE_MARGIN,
                                                self.EDGE_MARGIN, self.EDGE_MARGIN)
        if not reach.contains(point):
            return
        delta = -6 if point.y() < 32 else 6 if point.y() > viewport.height() - 32 else 0
        if delta:
            bar = self.table.verticalScrollBar()
            bar.setValue(bar.value() + delta)
            self.update_drag()

    def cancel(self):
        self.finish_landing()
        for day in self.days:
            day.stop_landing()
        if self.drag:
            self.drag['source'].unsetCursor()
        self.drag = None
        self.timer.stop()
        self.animation.stop()
        self.tilt_idle.stop()
        self.tilt_animation.stop()
        self.preview_effect.angle = 0.0
        self.preview.hide()
        self.hint.hide()


def _drag_times(start, end, delta, mode, lower=480, upper=1320):
    """Half-hour grid, fixed duration when moving, minimum half-hour resize."""
    snap = lambda value: int(math.floor(value / 30 + 0.5)) * 30
    if mode == 'move':
        duration = end - start
        new_start = max(lower, min(upper - duration, snap(start + delta)))
        return new_start, new_start + duration
    if mode == 'start':
        return max(lower, min(end - 30, snap(start + delta))), end
    return start, min(upper, max(start + 30, snap(end + delta)))


def create_widget(window):
    steps = create_widget_steps(window)
    while True:
        try:
            next(steps)
        except StopIteration as result:
            return result.value


def create_widget_steps(window):
    """Build GUI controls in short batches without blocking loading animation."""
    page = QWidget(getattr(window, "_module_build_parent", None)); page.setObjectName("courseSchedulePage")
    page.setStyleSheet("""
        #courseSchedulePage { background: transparent; }
        #courseSchedulePage QLabel { background: transparent; }
        /* 学期栏、表单和底部操作条与 #contentPanel 共用同一套面板样式；
           按钮、输入框、标题等通用外观由全局 theme.py 统一提供。 */
        #termPanel, #courseFormPanel, #scheduleFooter { background: #ffffff; border: 1px solid #ece4f8; border-radius: 18px; }
        #courseFormPanel #fieldBox, #courseFormPanel QLabel { background: transparent; }
        #courseTable { background: #ffffff; border: 1px solid #ece4f8; border-radius: 14px; gridline-color: #f4f0fb; }
                /* 表头横条由 CurrentDayHeader 自绘并按圆角裁剪，样式表只留文字样式。 */
                #courseTable QHeaderView::section { background: transparent; color: #8b7bb5; padding: 8px; border: none; font-weight: 700; }
        #courseTable QTableCornerButton::section { background: #faf8fe; border: none; }
    """)
    layout = QVBoxLayout(page); layout.setContentsMargins(36, 28, 36, 28); layout.setSpacing(14)

    code = QLineEdit(); code.setPlaceholderText("例如：MAT102H5")
    course_type = QLineEdit(); course_type.setPlaceholderText("例如：LEC、TUT、PRA")
    course_name = QLineEdit(); course_name.setPlaceholderText("课程全名（可选）")
    date_start = DateDropdown(QDate.currentDate())
    yield "正在构建课程表 · 日期选项…"
    date_end = DateDropdown(QDate.currentDate())
    yield "正在构建课程表 · 结束日期…"
    weekday = StyledComboBox(); weekday.addItems(WEEKDAYS)
    start_time = TimeDropdown(QTime(9, 0))
    yield "正在构建课程表 · 开始时间…"
    end_time = TimeDropdown(QTime(10, 0))
    yield "正在构建课程表 · 时间选项…"
    location = QLineEdit(); location.setPlaceholderText("教室（可选）")
    add = QPushButton("添加课程"); import_button = QPushButton("导入 .ics")
    delete = QPushButton("删除选中课程"); delete.setEnabled(False)
    delete.setObjectName("dangerButton")
    toggle_form = QPushButton("＋ 新增课程")
    toggle_form.setObjectName("toggleCourseForm")
    toggle_form.setCheckable(True)
    add.setObjectName("primaryButton")
    summary = QLabel(); summary.setObjectName("scheduleSummary")
    summary.setWordWrap(True)
    selection = QLabel("单击课程展开详情与操作按钮；拖动课程移动，拖动上下条调整时长（30 分钟吸附）")
    selection.setObjectName("selectionSummary")
    selection.setWordWrap(True)
    details = CourseDetailsBubble(page)
    details.setObjectName("courseDetails")
    details.setStyleSheet("#courseDetails { background: transparent; }")
    details_layout = QVBoxLayout(details)
    details_layout.setContentsMargins(25, 18, 25, 18)
    details_layout.setSpacing(14)
    details_header = QHBoxLayout()
    details_title = QLabel()
    details_title.setObjectName("courseDetailsTitle")
    details_title.setTextFormat(Qt.TextFormat.PlainText)
    details_title.setWordWrap(True)
    details_title.setStyleSheet("font-size: 16px; font-weight: 600; color: #7962aa; background: transparent;")
    close_details = QPushButton("收起详情")
    close_details.setCursor(Qt.CursorShape.PointingHandCursor)
    close_details.setStyleSheet("QPushButton { color: #87739f; background: transparent; border: none; padding: 4px 0 4px 10px; font-size: 11px; } QPushButton:hover { color: #594178; }")
    close_details.clicked.connect(details.hide)
    details_header.addWidget(details_title, 1)
    details_header.addWidget(close_details)
    details_layout.addLayout(details_header)
    details_body = QLabel()
    details_body.setObjectName("courseDetailsBody")
    details_body.setTextFormat(Qt.TextFormat.PlainText)
    details_body.setWordWrap(True)
    details_body.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
    details_body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    details_body.setStyleSheet("color: #514361; background: transparent;")
    details_scroll = QScrollArea()
    details_scroll.setFrameShape(QFrame.Shape.NoFrame)
    details_scroll.setWidgetResizable(True)
    details_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    details_scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
    details_scroll.setWidget(details_body)
    details_layout.addWidget(details_scroll)
    details_actions = QWidget()
    details_actions.setObjectName("courseDetailsActions")
    details_actions_layout = QHBoxLayout(details_actions)
    details_actions_layout.setContentsMargins(0, 0, 0, 0)
    details_actions_layout.setSpacing(8)
    details_copy = QPushButton("复制课程代码")
    details_copy.setCursor(Qt.CursorShape.PointingHandCursor)
    details_delete = QPushButton("删除课程")
    details_delete.setObjectName("dangerButton")
    details_delete.setCursor(Qt.CursorShape.PointingHandCursor)
    details_actions_layout.addWidget(details_copy)
    details_actions_layout.addStretch(1)
    details_actions_layout.addWidget(details_delete)
    details_layout.addWidget(details_actions)
    details_course = None
    details.hide()
    term = StyledComboBox(); term.addItems(("Fall", "Winter", "Summer"))
    for field in (code, course_type, course_name, date_start, date_end, weekday, start_time, end_time, location, term):
        field.setMinimumHeight(38)
    yield "正在构建课程表 · 课程详情…"

    def field_box(label, field):
        box = QWidget(); box.setObjectName("fieldBox")
        box_layout = QVBoxLayout(box); box_layout.setContentsMargins(0, 0, 0, 0); box_layout.setSpacing(5)
        label_widget = QLabel(label); label_widget.setObjectName("formLabel")
        box_layout.addWidget(label_widget); box_layout.addWidget(field)
        return box

    form = QGridLayout(); form.setContentsMargins(18, 18, 18, 18); form.setHorizontalSpacing(14); form.setVerticalSpacing(12)
    form.setColumnStretch(0, 1); form.setColumnStretch(1, 1)
    form.addWidget(field_box("课程代码 *", code), 0, 0)
    form.addWidget(field_box("类型 *", course_type), 0, 1)
    form.addWidget(field_box("名称", course_name), 1, 0, 1, 2)
    form.addWidget(field_box("起始日期 *", date_start), 2, 0)
    form.addWidget(field_box("结束日期 *", date_end), 2, 1)
    yield "正在构建课程表 · 课程表单…"
    form.addWidget(field_box("星期 *", weekday), 3, 0)
    form.addWidget(field_box("教室", location), 3, 1)
    form.addWidget(field_box("开始 *", start_time), 4, 0)
    form.addWidget(field_box("结束 *", end_time), 4, 1)
    form_hint = QLabel("带 * 的字段为必填项；课程将添加到当前学期。")
    form_hint.setWordWrap(True)
    form_hint.setObjectName("formHint")
    form.addWidget(form_hint, 5, 0, 1, 2)
    form_actions = QHBoxLayout(); form_actions.setSpacing(8)
    form_actions.addStretch(); form_actions.addWidget(add)
    form.addLayout(form_actions, 6, 0, 1, 2)
    yield "正在构建课程表 · 课程表单…"
    table = RoundedTableWidget(0, 8)
    table.setHorizontalHeader(CurrentDayHeader(Qt.Orientation.Horizontal, table))
    table.setHorizontalHeaderLabels(["时间"] + list(WEEKDAYS))
    table.horizontalHeader().setStretchLastSection(False)
    table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
    table.setColumnWidth(0, 78)
    for column in range(1, 8):
        table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
    table.setWordWrap(True)
    table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
    table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    table.setVerticalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)
    table.setHorizontalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)
    table.verticalScrollBar().setSingleStep(12)
    table.horizontalScrollBar().setSingleStep(12)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers); table.verticalHeader().setVisible(False)
    table.setItemDelegate(CourseCellDelegate(table))
    table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
    table.horizontalHeader().setFixedHeight(52)
    table.setObjectName("courseTable")
    # 单元格内容会盖住表格外框的圆角，把视口裁成同样的圆角。
    clip_to_rounded_frame(table, 13)
    table.setMinimumHeight(360); table.setAlternatingRowColors(False); courses = _load_courses(); grid_start = 8 * 60
    saved_term = _load_selected_term()
    term.setCurrentText(saved_term if saved_term in ("Fall", "Winter", "Summer")
                        else _term_from_month(QDate.currentDate().month()))
    selected_course = None; cell_widgets = []
    yield "正在构建课程表 · 时间网格…"

    def refresh():
        nonlocal grid_start, selected_course, cell_widgets, details_course
        drag_controller.cancel()
        scroll_value = table.verticalScrollBar().value()
        if table._scroll_animation and table._scroll_animation.state() != QAbstractAnimation.State.Stopped:
            table._scroll_animation.stop()
        selected_course = None; cell_widgets = []; details_course = None
        details.hide()
        visible_courses = [course for course in courses if _course_term(course) == term.currentText()]
        summary.setText(f"{term.currentText()}  ·  {len(visible_courses)} 门课程  ·  08:00–22:00")
        selection.setText("单击课程展开详情与操作按钮；拖动课程移动，拖动上下条调整时长（30 分钟吸附）")
        timed = [_course_start_end(course) for course in visible_courses]
        timed = [(start, end, course) for start, end, course in timed if start is not None]
        grid_start = 8 * 60
        grid_end = 22 * 60
        timed = [(start, end, course) for start, end, course in timed
                 if end > grid_start and start < grid_end]
        slots = list(range(grid_start, grid_end + 1, 30))
        for row in range(table.rowCount()):
            for column in range(table.columnCount()):
                widget = table.cellWidget(row, column)
                if widget:
                    widget.deleteLater()
        table.clearContents(); table.clearSpans(); table.setRowCount(len(slots))
        for row, minutes in enumerate(slots):
            table.setItem(row, 0, QTableWidgetItem(_format_clock(minutes)))
            table.setRowHeight(row, 36)
        table.setSpan(0, 0, len(slots), 1)
        table.setCellWidget(0, 0, ScheduleTimeAxis(grid_start, grid_end))
        for day_index, day in enumerate(WEEKDAYS, start=1):
            day_courses = [item[2] for item in timed if _course_day(item[2]) == day]
            cell_widget = DayScheduleWidget(day_courses, grid_start, grid_end, endpoint_height=36, weekday_index=day_index - 1)
            cell_widget.courseSelected.connect(select_course)
            cell_widget.courseClicked.connect(show_details)
            cell_widgets.append(cell_widget)
            table.setSpan(0, day_index, len(slots), 1)
            table.setCellWidget(0, day_index, cell_widget)
        drag_controller.attach(cell_widgets)
        QTimer.singleShot(0, lambda: table.verticalScrollBar().setValue(
            min(scroll_value, table.verticalScrollBar().maximum())))
        delete.setEnabled(selected_course is not None)

    def update_details(course):
        details_title.setText("课程详情")
        details_copy.setEnabled(bool((course.get("code") or course.get("title") or "").strip()))
        rows = [
            f"教室：{course.get('location') or '待定'}",
            f"类型：{course.get('course_type') or '未填写'}",
            f"时间：{_time_slot(course) or '未填写'}",
            f"全称：{course.get('arrangement') or course.get('title') or '未填写'}",
        ]
        details_body.setText("\n\n".join(rows))
        details_scroll.setFixedHeight(min(380, max(90, details_body.heightForWidth(280) + 12)))

    def show_details(course):
        nonlocal details_course
        for day in cell_widgets:
            day._layout_boxes()
            for item, rect in day._segments:
                if item is course:
                    visible = QRect(day.mapToGlobal(rect.topLeft()), rect.size()).intersected(
                        QRect(table.viewport().mapToGlobal(QPoint()), table.viewport().size()))
                    if not visible.isEmpty():
                        details_course = course
                        update_details(course)
                        details.show_at(visible)
                    return

    def select_course(course):
        nonlocal selected_course
        if table._scroll_animation and table._scroll_animation.state() != QAbstractAnimation.State.Stopped:
            table._scroll_animation.stop()
        selected_course = course
        details.hide()
        selection.setText(f"已选择：{course.get('code') or course.get('title', '课程')}  ·  {_time_slot(course)}")
        for cell_widget in cell_widgets:
            cell_widget.set_selected(course)
        delete.setEnabled(True)

    def commit_drag(course, day, start, end):
        if (_course_day(course), *_course_start_end(course)[:2]) == (day, start, end):
            return
        previous = dict(course)
        course.update(weekday=day, start=_format_clock(start), end=_format_clock(end))
        try:
            _save_courses(courses)
        except OSError as exc:
            course.clear()
            course.update(previous)
            QMessageBox.warning(page, "保存失败", f"课程时间未更改：{exc}")
            return
        # Reuse the day widgets so the viewport and selection remain stable.
        for index, widget in enumerate(cell_widgets):
            widget.courses = [item for item in courses if _course_term(item) == term.currentText()
                              and _course_day(item) == WEEKDAYS[index]]
            widget.update()
        select_course(course)
        selection.setText(f"已保存：{course.get('code') or course.get('title', '课程')}  ·  {day} {_time_slot(course)} · 后续沿用此时间")

    drag_controller = ScheduleDragController(table, commit_drag, page)
    page.drag_controller = drag_controller
    table.verticalScrollBar().valueChanged.connect(details.hide)
    table.horizontalScrollBar().valueChanged.connect(details.hide)

    last_clock_day = datetime.now().date()

    def update_current_time():
        nonlocal last_clock_day
        if not page.isVisible():
            return
        now = datetime.now()
        day_changed = now.date() != last_clock_day
        last_clock_day = now.date()
        for widget in cell_widgets:
            if day_changed:
                widget.update()
            elif widget.weekday_index == now.weekday():
                # Repaint just the moving glow, leaving the rest of the timetable idle.
                minute = now.hour * 60 + now.minute + (now.second + now.microsecond / 1_000_000) / 60
                y = widget.top_padding + (minute - widget.start_minute) * widget.row_height / 30
                previous_y = getattr(widget, '_clock_y', y)
                if abs(previous_y - y) > 20:
                    widget.update(QRect(0, int(previous_y) - 10, widget.width(), 22))
                widget.update(QRect(0, int(y) - 10, widget.width(), 22))
                widget._clock_y = y
        if day_changed:
            table.horizontalHeader().viewport().update()

    clock_timer = QTimer(page)
    clock_timer.setTimerType(Qt.TimerType.PreciseTimer)
    clock_timer.setInterval(33)
    clock_timer.timeout.connect(update_current_time)
    clock_timer.start()

    def add_course():
        course_code = code.text().strip(); type_name = course_type.text().strip()
        if not course_code:
            QMessageBox.warning(page, "无法添加", "请填写课程代码。"); code.setFocus(); return
        if not type_name:
            QMessageBox.warning(page, "无法添加", "请填写课程类型。"); course_type.setFocus(); return
        start_date_value = date_start.date(); end_date_value = date_end.date()
        if start_date_value > end_date_value:
            QMessageBox.warning(page, "日期有误", "结束日期必须晚于或等于开始日期。"); return
        start = start_time.time().toString("HH:mm"); end = end_time.time().toString("HH:mm")
        if start >= end:
            QMessageBox.warning(page, "时间有误", "结束时间必须晚于开始时间。"); return
        courses.append({
            "title": course_name.text().strip() or course_code, "code": course_code, "course_type": type_name,
            "date_start": start_date_value.toString("yyyy-MM-dd"), "date_end": end_date_value.toString("yyyy-MM-dd"),
            "weekday": weekday.currentText(), "start": start, "end": end, "location": location.text().strip(),
            "term": term.currentText(), "arrangement": "", "source": "manual",
        })
        _save_courses(courses); refresh()
        for field in (code, course_type, course_name, location): field.clear()

    def import_ics():
        nonlocal courses
        filename, _ = QFileDialog.getOpenFileName(page, "选择课程表 ICS 文件", "", "iCalendar 文件 (*.ics);;所有文件 (*)")
        if not filename: return
        try:
            imported = _parse_ics(Path(filename))
        except (OSError, UnicodeError, ValueError) as exc:
            QMessageBox.critical(page, "读取失败", f"无法读取该 ICS 文件：{exc}"); return
        if not imported:
            QMessageBox.warning(page, "没有课程", "该 ICS 文件中没有找到有效的日历事件。"); return
        courses = imported; _save_courses(courses); refresh()
        QMessageBox.information(page, "读取完成", f"已读取 {len(imported)} 门课程，之前的课程表内容已覆盖。")

    def remove_course(course):
        nonlocal selected_course
        label = (course.get("code") or course.get("title") or "课程").strip()
        if course in courses:
            courses.remove(course)
        selected_course = None
        _save_courses(courses); refresh()
        selection.setText(f"已删除：{label}")

    def delete_course():
        if selected_course is None:
            QMessageBox.information(page, "删除课程", "请先点击要删除的课程。"); return
        remove_course(selected_course)

    def _copy_course_code(course):
        text = (course.get("code") or course.get("title") or "").strip()
        if not text:
            return
        QApplication.clipboard().setText(text)
        selection.setText(f"已复制课程代码：{text}")

    def copy_details_code():
        if details_course is not None:
            _copy_course_code(details_course)

    def delete_details_course():
        if details_course is not None:
            details.hide()
            remove_course(details_course)

    details_copy.clicked.connect(copy_details_code)
    details_delete.clicked.connect(delete_details_course)

    add.clicked.connect(add_course); import_button.clicked.connect(import_ics); delete.clicked.connect(delete_course)
    def change_term(value):
        _save_selected_term(value)
        refresh()

    term.currentTextChanged.connect(change_term)
    term_bar = QHBoxLayout(); term_bar.setContentsMargins(0, 0, 0, 0); term_bar.setSpacing(10)
    term_label = QLabel("当前学期"); term_label.setObjectName("toolbarLabel")
    term_bar.addWidget(term_label); term_bar.addWidget(term); term_bar.addStretch()
    term_bar.addWidget(import_button); term_bar.addWidget(toggle_form)
    term_panel = QFrame(); term_panel.setObjectName("termPanel")
    toolbar = QVBoxLayout(term_panel); toolbar.setContentsMargins(16, 14, 16, 14); toolbar.setSpacing(10)
    toolbar.addLayout(term_bar); toolbar.addWidget(summary)
    form_panel = QFrame(); form_panel.setObjectName("courseFormPanel"); form_panel.setLayout(form)
    form_panel.setVisible(False)
    yield "正在构建课程表 · 课程数据…"
    def toggle_course_form(expanded):
        form_panel.setVisible(expanded)
        toggle_form.setText("收起表单" if expanded else "＋ 新增课程")
        if expanded:
            code.setFocus()
    toggle_form.toggled.connect(toggle_course_form)
    layout.addWidget(_heading("课程表", "按星期和时间查看你的课程安排"))
    layout.addWidget(term_panel); layout.addWidget(form_panel)
    yield "正在构建课程表 · 学期工具栏…"
    layout.addWidget(table, 1)
    footer = QFrame(); footer.setObjectName("scheduleFooter")
    selection_bar = QHBoxLayout(footer); selection_bar.setSpacing(12)
    selection_bar.setContentsMargins(16, 10, 12, 10)
    selection_bar.addWidget(selection, 1); selection_bar.addWidget(delete)
    layout.addWidget(footer)
    yield "正在构建课程表 · 课程布局…"
    refresh()
    return page


def _load_courses():
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        return [course for course in data if isinstance(course, dict)] if isinstance(data, list) else []
    except (OSError, UnicodeError, json.JSONDecodeError): return []


def _save_courses(courses):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = DATA_FILE.with_suffix(".json.tmp")
    try:
        temporary.write_text(json.dumps(courses, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(DATA_FILE)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_selected_term():
    try:
        settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return settings.get("term", "") if isinstance(settings, dict) else ""
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""


def _save_selected_term(term):
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_json(SETTINGS_FILE, {"term": term})


def _parse_ics(path):
    text = path.read_text(encoding="utf-8-sig")
    lines = re.sub(r"\r?\n[ \t]", "", text).splitlines(); events = []; current = None
    for line in lines:
        if line.upper() == "BEGIN:VEVENT": current = {}
        elif line.upper() == "END:VEVENT":
            if current:
                course = _event_to_course(current)
                if course: events.append(course)
            current = None
        elif current is not None and ":" in line:
            key, value = line.split(":", 1); current[key.split(";", 1)[0].upper()] = _unescape_ics(value)
    return events


def _event_to_course(event):
    title = event.get("SUMMARY", "").strip(); start = _parse_ics_datetime(event.get("DTSTART", ""))
    if not title or not start: return None
    end = _parse_ics_datetime(event.get("DTEND", ""))
    type_match = re.search(r"\b(LEC|TUT|PRA|LAB|SEM|DIS|TST)\d*\b", title, re.IGNORECASE)
    code_match = re.search(r"\b[A-Z]{2,}\d{3,}[A-Z]?\d?\b", title, re.IGNORECASE)
    date_value = start.strftime("%Y-%m-%d")
    return {"title": title, "code": code_match.group(0).upper() if code_match else title,
            "course_type": type_match.group(1).upper() if type_match else "课程",
            "weekday": WEEKDAYS[start.weekday()], "start": start.strftime("%H:%M"),
            "end": end.strftime("%H:%M") if end else "", "date": date_value,
            "date_start": date_value, "date_end": end.strftime("%Y-%m-%d") if end else date_value,
            "term": _term_from_month(start.month), "location": event.get("LOCATION", "").strip(),
            "arrangement": event.get("DESCRIPTION", "").strip(), "source": "ics"}


def _parse_ics_datetime(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value: return None
    raw = value.rstrip("Z")
    for pattern in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try: return datetime.strptime(raw, pattern)
        except ValueError: continue
    return None


def _parse_manual_arrangement(value):
    match = re.search(r"(周[一二三四五六日天])\s*(\d{1,2}:\d{2})\s*[-~至]\s*(\d{1,2}:\d{2})(?:\s*[·•,，/]?\s*(.*))?", value)
    if not match:
        return None
    day = "周日" if match.group(1) == "周天" else match.group(1)
    start = _normalize_clock(match.group(2)); end = _normalize_clock(match.group(3))
    if start >= end:
        return None
    return {"weekday": day, "start": start, "end": end, "location": (match.group(4) or "").strip()}


def _normalize_clock(value):
    hour, minute = value.split(":")
    hour = int(hour); minute = int(minute)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("invalid time")
    return f"{hour:02d}:{minute:02d}"


def _course_day(course):
    if course.get("weekday") in WEEKDAYS: return course["weekday"]
    parsed = _parse_ics_datetime(course.get("start", ""))
    return WEEKDAYS[parsed.weekday()] if parsed else ""


def _course_term(course):
    if course.get("term") in ("Fall", "Winter", "Summer"):
        return course["term"]
    date_value = course.get("date", "") or course.get("start", "")
    parsed = _parse_ics_datetime(date_value)
    return _term_from_month(parsed.month) if parsed else "Fall"


def _term_from_month(month):
    if month in (1, 2, 3, 4):
        return "Winter"
    if month in (5, 6, 7, 8):
        return "Summer"
    return "Fall"


def _time_slot(course):
    start = _clock(course.get("start", "")); end = _clock(course.get("end", ""))
    if not start: return ""
    return f"{start} - {end}" if end else start


def _course_start_end(course):
    start = _clock(course.get("start", "")); end = _clock(course.get("end", ""))
    if not start:
        return None, None, course
    try:
        start_minutes = _to_minutes(start)
        end_minutes = _to_minutes(end) if end else start_minutes + 30
    except (TypeError, ValueError):
        return None, None, course
    if not 0 <= start_minutes < 24 * 60 or not start_minutes < end_minutes <= 24 * 60:
        return None, None, course
    return start_minutes, end_minutes, course


def _course_contains(course, minutes):
    start, end, _ = _course_start_end(course)
    return start is not None and start <= minutes < end


def _to_minutes(value):
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def _format_clock(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _clock(value):
    if not isinstance(value, str) or not value:
        return ""
    if re.fullmatch(r"\d{1,2}:\d{2}", value):
        try:
            return _normalize_clock(value)
        except ValueError:
            return ""
    parsed = _parse_ics_datetime(value)
    return parsed.strftime("%H:%M") if parsed else value


def _course_text(course):
    code = str(course.get("code") or "").strip()
    title = str(course.get("title") or "")
    # Imported summaries may include section type, room, and other metadata.
    pattern = r"\b[A-Z]{2,}\d{3,}[A-Z]?\d?\b"
    match = re.search(pattern, code, re.IGNORECASE) or re.search(pattern, title, re.IGNORECASE)
    if match:
        return match.group(0).upper()
    # Preserve custom manual codes, but never fall back to a full summary.
    if code and not re.search(r"\s|\b(?:LEC|TUT|PRA|LAB|SEM|DIS|TST)\d*\b", code, re.IGNORECASE):
        return code
    return "未填写代码"



def _fit_course_text(text, font, width, height):
    """在窄课程框内先缩小字号，放不下再按字符换行，避免文字被裁切。"""
    if width <= 0 or height <= 0:
        return text
    base_size = font.pointSize() if font.pointSize() > 0 else 10
    floor = 6
    # 课程代码是一个整体，优先缩到单行，避免出现「MAT232 / H5」这类断行。
    for size in range(base_size, max(floor, 7) - 1, -1):
        font.setPointSize(size)
        metrics = QFontMetrics(font)
        if metrics.horizontalAdvance(text) <= width and metrics.lineSpacing() <= height:
            return text
    lines = [text]
    for size in range(base_size, floor - 1, -1):
        font.setPointSize(size)
        metrics = QFontMetrics(font)
        lines = []
        for line in text.splitlines():
            current = ""
            for char in line:
                if current and metrics.horizontalAdvance(current + char) > width:
                    lines.append(current); current = ""
                current += char
            lines.append(current)
        if lines and max(metrics.horizontalAdvance(line) for line in lines) <= width and metrics.lineSpacing() * len(lines) <= height:
            return "\n".join(lines)
    return "\n".join(lines)


def _rounded_polygon(points, radius=8):
    """将连续课程的折线轮廓转换为带圆角的闭合路径。"""
    if len(points) < 3:
        return QPainterPath()
    incoming = []; outgoing = []
    for index, point in enumerate(points):
        previous = points[index - 1]; following = points[(index + 1) % len(points)]
        incoming.append(_toward(point, previous, radius))
        outgoing.append(_toward(point, following, radius))
    path = QPainterPath(); path.moveTo(outgoing[0])
    for index in range(1, len(points) + 1):
        current = index % len(points)
        path.lineTo(incoming[current]); path.quadTo(QPointF(points[current]), outgoing[current])
    path.closeSubpath()
    return path


def _toward(point, target, distance):
    dx = target.x() - point.x(); dy = target.y() - point.y()
    length = math.hypot(dx, dy)
    if length == 0:
        return point
    distance = min(distance, length / 2)
    return QPointF(point.x() + dx * distance / length, point.y() + dy * distance / length)


def _unescape_ics(value):
    return value.replace(r"\n", "\n").replace(r"\,", ",").replace(r"\;", ";").replace(r"\\", "\\")


def _heading(title, sub):
    widget = QWidget(); layout = QVBoxLayout(widget); layout.setContentsMargins(0, 0, 0, 4)
    layout.setSpacing(4)
    heading = QLabel(title); heading.setObjectName("pageTitle")
    subtitle = QLabel(sub); subtitle.setObjectName("muted"); layout.addWidget(heading); layout.addWidget(subtitle)
    return widget

