"""Transient geometry and a screen-color wire over Maya's GPU viewport."""

from maya import cmds
from maya.api import OpenMaya as om, OpenMayaUI as omui
from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid, wrapInstance
from model_pack_linear_shift.overlay import Canvas, Projection

# Maya's active toolbar button: #5285A6, in display/UI sRGB.
# Draw as UI pixels: VP2 reference/template wires and scene color management
# must not replace this with black, gray, or a different shade of cyan.
TURQUOISE = (82 / 255.0, 133 / 255.0, 166 / 255.0)


def wire_edges(counts, indices):
    edges = set()
    offset = 0
    for count in counts:
        face = indices[offset:offset + count]
        edges.update(tuple(sorted((a, b))) for a, b in zip(face, face[1:] + face[:1]))
        offset += count
    return sorted(edges)


class WireCanvas(Canvas):
    """Composited, input-transparent window; no black VP2 wire underneath."""
    def __init__(self, parent, manager):
        super().__init__(parent, manager)
        self.setObjectName('modelPackFastDraftOverlay')

    def paint_frame(self, painter):
        painter.save()
        try:
            painter.setCompositionMode(QtGui.QPainter.CompositionMode_Source)
            painter.fillRect(self.rect(), QtCore.Qt.transparent)
            painter.setCompositionMode(QtGui.QPainter.CompositionMode_SourceOver)
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            # Leave floating Maya panels and dialogs unobscured.
            region = QtGui.QRegion(self.rect())
            origin = self.mapToGlobal(QtCore.QPoint())
            for window in QtWidgets.QApplication.topLevelWidgets():
                if window in (self, self.viewport.window()) or not window.isVisible() or window.isMinimized():continue
                if window.windowFlags() & QtCore.Qt.WindowTransparentForInput:continue
                region -= QtGui.QRegion(window.frameGeometry().translated(-origin))
            painter.setClipRegion(region)
            color = QtGui.QColor.fromRgbF(*self.manager.color)
            painter.setPen(QtGui.QPen(color, 2.0))
            projection = Projection(self.manager.view, self.width(), self.height())
            for a, b in self.manager.edges:
                segment = projection.line(self.manager.points[a], self.manager.points[b])
                if segment is not None:
                    painter.drawLine(*(QtCore.QPointF(x, self.height() - 1 - y) for x, y in segment))
            snap=getattr(self.manager,'snap',None)
            if snap is not None:
                x,y,visible=projection.point(snap.point)
                if visible:
                    center=QtCore.QPointF(x,self.height()-1-y)
                    painter.setBrush(QtCore.Qt.NoBrush)
                    painter.drawPolygon(QtGui.QPolygonF([center+QtCore.QPointF(dx,dy)
                        for dx,dy in ((0,-6),(6,0),(0,6),(-6,0))]))
                    painter.drawLine(center+QtCore.QPointF(-10,0),center+QtCore.QPointF(10,0))
                    painter.drawLine(center+QtCore.QPointF(0,-10),center+QtCore.QPointF(0,10))
                    painter.drawText(center+QtCore.QPointF(13,-9),snap.label)
        finally:
            painter.restore()


class WireOverlay(QtCore.QObject):
    def __init__(self):
        super().__init__()
        self.points, self.edges = [], []
        self.snap=None
        self.color = TURQUOISE
        self.view = self.canvas = self.pointer = None
        self.callback = self.panel = None
        self.painting = False
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self.tick)

    def update(self, points, counts, indices, color, view):
        self.points, self.edges, self.color = points, wire_edges(counts, indices), color
        self.bind(view)

    def bind(self,view):
        if view is None:
            if cmds.about(batch=True):return
            view = omui.M3dView.active3dView()
        if not hasattr(view, 'widget'):return  # Geometry-only/headless views.
        pointer = view.widget()
        if not isinstance(pointer, int) or not pointer:return
        if pointer != self.pointer:self.clear_canvas()
        self.view, self.pointer = view, pointer
        self.timer.start()
        self.tick()
        if self.canvas is not None and self.canvas.isVisible():self.canvas.repaint()

    def set_snap(self,snap,view):
        self.snap=snap
        if snap is None and not self.points:
            self.close();return
        self.bind(view)

    def tick(self):
        if self.view is None:return
        try:
            parent = wrapInstance(self.pointer, QtWidgets.QWidget)
            if parent is None or not isValid(parent):self.close();return
            if self.canvas is None:
                self.canvas = WireCanvas(parent, self)
                for panel in cmds.getPanel(type='modelPanel') or []:
                    if int(omui.M3dView.getM3dViewFromModelPanel(panel).widget()) == self.pointer:
                        self.panel = panel
                        self.callback = omui.MUiMessage.add3dViewPostRenderMsgCallback(panel, self.rendered)
                        break
            if not parent.isVisible() or parent.window().isMinimized():self.canvas.hide();return
            rect = QtCore.QRect(parent.mapToGlobal(QtCore.QPoint()), parent.size())
            if self.canvas.geometry() != rect:self.canvas.setGeometry(rect)
            if self.canvas.isHidden():self.canvas.show();self.canvas.raise_()
            self.canvas.update()
        except (RuntimeError, TypeError):
            self.close()

    def rendered(self, panel, *_):
        if self.painting or panel != self.panel or self.canvas is None:return
        self.painting = True
        try:
            if isValid(self.canvas) and self.canvas.isVisible():self.canvas.repaint()
        finally:
            self.painting = False

    def clear_canvas(self):
        if self.callback is not None:
            try:om.MMessage.removeCallback(self.callback)
            except RuntimeError:pass
        self.callback = self.panel = None
        if self.canvas is not None and isValid(self.canvas):
            self.canvas.hide();self.canvas.deleteLater()
        self.canvas = None

    def close(self):
        self.timer.stop()
        self.clear_canvas()
        self.view = self.pointer = None
        self.points, self.edges = [], []
        self.snap=None


class Preview:
    def __init__(self, color=None):
        self.handle = None
        self.mesh = None
        self.count = 0
        self.color = color
        self.overlay = None
        self.snap=None

    def set_snap(self,snap,view=None):
        self.snap=snap
        if cmds.about(batch=True):return
        if self.overlay is None:
            if snap is None:return
            self.overlay=WireOverlay()
        self.overlay.set_snap(snap,view)

    def clear_mesh(self):
        if self.handle and self.handle.isValid() and self.handle.isAlive():
            modifier = om.MDagModifier()
            modifier.deleteNode(self.handle.object())
            modifier.doIt()
        self.handle = self.mesh = None
        self.count = 0

    def clear(self):
        if self.overlay is not None:self.overlay.close()
        self.overlay = None
        self.snap=None
        self.clear_mesh()

    def update(self, footprint, depth, view=None):
        points, counts, indices = footprint.mesh(depth)
        if self.mesh is None or self.count != len(points):
            self.clear_mesh()
            transform = om.MFnTransform().create()
            self.handle = om.MObjectHandle(transform)
            fn = om.MFnDependencyNode(transform)
            fn.setName('fdPreview#')
            fn.setDoNotWrite(True)
            # Retain API geometry for topology, with no selectable/black wire.
            fn.findPlug('visibility', False).setBool(False)
            self.mesh = om.MFnMesh()
            self.mesh.create(points, counts, indices, parent=transform)
            self.mesh.setDoNotWrite(True)
            self.count = len(points)
        else:
            self.mesh.setPoints(points, om.MSpace.kObject)
        self.mesh.updateSurface()
        # Geometry-only mayapy views have no native QWidget. In batch mode
        # don't allocate GUI/timer objects in Maya's standalone event loop.
        if not cmds.about(batch=True):
            if self.overlay is None:self.overlay = WireOverlay()
            self.overlay.update(points, counts, indices, self.color or TURQUOISE, view)
