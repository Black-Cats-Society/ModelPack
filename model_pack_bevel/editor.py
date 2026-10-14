"""Compact spline editor below Model Pack; no persistent parameter sliders."""
import math
from PySide6 import QtCore, QtGui, QtWidgets
from .profiles import Curve


class CurveEditor(QtWidgets.QWidget):
    changed = QtCore.Signal(object)
    actionStarted = QtCore.Signal()
    actionFinished = QtCore.Signal()

    def __init__(self, curve, parent=None):
        super().__init__(parent)
        self.curve = curve
        self.selected = None
        self.dragging = None
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setMinimumWidth(220)
        self.setFixedHeight(110)
        self.setToolTip("Двойной клик — точка; Delete — удалить; потяни точку или касательную; ПКМ — тип участка.")

    def sizeHint(self):
        return QtCore.QSize(240,120)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setFixedHeight(max(110, self.width() // 2))

    def area(self):
        return QtCore.QRectF(self.rect()).adjusted(14, 12, -14, -16)

    def screen(self, x, y):
        box = self.area()
        low = 0 if self.curve.falloff else -1
        return QtCore.QPointF(box.left() + x * box.width(),
                             box.bottom() - (y-low) / (1-low) * box.height())

    def normalized(self, point):
        box = self.area()
        low = 0 if self.curve.falloff else -1
        return ((point.x() - box.left()) / box.width(),
                low + (box.bottom() - point.y()) / box.height() * (1-low))

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.fillRect(self.rect(), QtGui.QColor("#3c3c3c"))
        painter.setPen(QtGui.QPen(QtGui.QColor("#555555"), 1))
        box = self.area()
        for i in range(5):
            x = box.left() + box.width() * i/4
            y = box.top() + box.height() * i/4
            painter.drawLine(QtCore.QPointF(x, box.top()), QtCore.QPointF(x, box.bottom()))
            painter.drawLine(QtCore.QPointF(box.left(), y), QtCore.QPointF(box.right(), y))
        path = QtGui.QPainterPath(self.screen(0, self.curve.value(0)))
        for i in range(1, 161):
            path.lineTo(self.screen(i/160, self.curve.value(i/160)))
        painter.setPen(QtGui.QPen(QtGui.QColor("#eeeeee"), 2))
        painter.drawPath(path)
        for index, point in enumerate(self.curve.points):
            center = self.screen(point[0], point[1])
            if index == self.selected:
                slope = point[2] if point[2] is not None else 0
                painter.setPen(QtGui.QPen(QtGui.QColor("#e39957"), 1))
                for sign in (-1, 1):
                    handle = self.screen(point[0]+sign*.1, point[1]+sign*.1*slope)
                    painter.drawLine(center, handle)
                    painter.drawEllipse(handle, 3, 3)
            painter.setPen(QtGui.QColor("#f0b578") if index == self.selected else QtGui.QColor("white"))
            painter.setBrush(QtGui.QColor("#333333"))
            painter.drawEllipse(center, 4, 4)
        painter.setPen(QtGui.QColor("#bbbbbb"))
        painter.drawText(3, self.height()-2, "0")
        painter.drawText(self.width()-32, self.height()-2, "100%")

    def hit(self, position):
        candidates = [(math.hypot(position.x()-self.screen(p[0],p[1]).x(),
                                 position.y()-self.screen(p[0],p[1]).y()), i)
                      for i, p in enumerate(self.curve.points)]
        distance, index = min(candidates)
        return index if distance <= 10 else None

    def change(self, points):
        try:
            self.curve = Curve(tuple(tuple(p) for p in points), self.curve.falloff)
        except ValueError:
            return
        self.update()
        self.changed.emit(self.curve)

    def mousePressEvent(self, event):
        if event.button() != QtCore.Qt.LeftButton:
            return super().mousePressEvent(event)
        if self.selected is not None:
            point = self.curve.points[self.selected]
            slope = point[2] if point[2] is not None else 0
            for sign in (-1,1):
                handle = self.screen(point[0]+sign*.1, point[1]+sign*.1*slope)
                if (handle-event.position()).manhattanLength() < 12:
                    self.dragging = "tangent"
                    self.actionStarted.emit()
                    return
        self.selected = self.hit(event.position())
        self.dragging = "point" if self.selected is not None else None
        if self.dragging is not None:
            self.actionStarted.emit()
        self.update()

    def mouseMoveEvent(self, event):
        if self.dragging is None or self.selected is None:
            return
        points = [list(p) for p in self.curve.points]
        index = self.selected
        x, y = self.normalized(event.position())
        if self.dragging == "tangent":
            if abs(x-points[index][0]) > .01:
                points[index][2] = max(-10, min(10, (y-points[index][1])/(x-points[index][0])))
        elif index not in (0, len(points)-1):
            points[index][0] = max(points[index-1][0]+.005, min(points[index+1][0]-.005, x))
            low, high = (-1,1) if not self.curve.falloff else (points[index+1][1], points[index-1][1])
            points[index][1] = max(low, min(high,y))
        self.change(points)

    def mouseReleaseEvent(self, event):
        if self.dragging is not None:
            self.actionFinished.emit()
        self.dragging = None

    def mouseDoubleClickEvent(self, event):
        if event.button() != QtCore.Qt.LeftButton:
            return
        if self.hit(event.position()) is not None:
            return
        x,y = self.normalized(event.position())
        if .005 < x < .995:
            points = [list(p) for p in self.curve.points]
            index = next(i for i,p in enumerate(points) if p[0] > x)
            if min(x-points[index-1][0],points[index][0]-x) < .005:
                return
            if self.curve.falloff:
                y = max(points[index][1], min(points[index-1][1], y))
            else:
                y = max(-1,min(1,y))
            points.insert(index,[x,y,None])
            self.selected = index
            self.actionStarted.emit()
            self.change(points)
            self.actionFinished.emit()

    def keyPressEvent(self, event):
        if event.key() in (QtCore.Qt.Key_Delete, QtCore.Qt.Key_Backspace):
            if self.selected not in (None, 0, len(self.curve.points)-1):
                points = list(self.curve.points)
                points.pop(self.selected)
                self.selected = None
                self.actionStarted.emit()
                self.change(points)
                self.actionFinished.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self,event):
        index = self.hit(event.pos())
        if index is None:
            return
        menu = QtWidgets.QMenu(self)
        auto = menu.addAction("Плавные автоматические касательные")
        linear = menu.addAction("Линейный участок")
        reset = menu.addAction("Сбросить кривую")
        result = menu.exec(event.globalPos())
        if result is None:
            return
        self.actionStarted.emit()
        points = [list(p) for p in self.curve.points]
        if result == reset:
            self.selected = None
            self.change(Curve().data() if self.curve.falloff else Curve(
                ((0,0,None),(.5,.5,None),(1,0,None)),False).data())
        elif result == auto:
            points[index][2] = None
            self.change(points)
        elif result == linear:
            following = min(index+1,len(points)-1)
            prior = following-1
            slope = (points[following][1]-points[prior][1])/(points[following][0]-points[prior][0])
            points[prior][2] = points[following][2] = slope
            self.change(points)
        self.actionFinished.emit()


class ProfilePanel(QtWidgets.QWidget):
    hidden = QtCore.Signal()

    def __init__(self, falloff, profile, on_falloff, on_profile, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0,4,0,0)
        self.tabs = QtWidgets.QTabWidget()
        self.falloff = CurveEditor(falloff)
        self.profile = CurveEditor(profile)
        self.tabs.addTab(self.falloff,"Затухание")
        self.tabs.addTab(self.profile,"Сечение")
        self.falloff.changed.connect(on_falloff)
        self.profile.changed.connect(on_profile)
        layout.addWidget(self.tabs)
        close = QtWidgets.QPushButton("Скрыть редактор")
        close.clicked.connect(self.hide)
        layout.addWidget(close)

    def hideEvent(self,event):
        super().hideEvent(event)
        self.hidden.emit()
