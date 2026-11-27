"""Freehand viewport curves, with a compact Model Pack settings panel."""
import math
import weakref
from pathlib import Path
from maya import cmds
from maya.api import OpenMaya as om,OpenMayaUI as omui
from PySide6 import QtCore,QtGui,QtWidgets
from shiboken6 import isValid,wrapInstance
from model_pack_draft import core
from model_pack_draft.preview import WireOverlay,TURQUOISE
from model_pack_pencil_fit import create,curve_fn

CONTEXT='modelPackPencilCurveContext'
_HOST=None
MANIPULATORS={QtCore.Qt.Key_Q:'selectSuperContext',QtCore.Qt.Key_W:'moveSuperContext',
              QtCore.Qt.Key_E:'RotateSuperContext',QtCore.Qt.Key_R:'scaleSuperContext'}


def active():return _HOST is not None and not _HOST.stopped and cmds.currentCtx()==CONTEXT


def shutdown():
    global _HOST
    host,_HOST=_HOST,None
    if host is not None and isValid(host):host.stop()


def toggle(window):
    global _HOST
    if active() and _HOST.window() is window:shutdown();return
    shutdown();window._prepare_tool('pencil_curve');host=Host(window);_HOST=host
    try:host.start()
    except Exception:
        shutdown();raise


def sync(window):
    if _HOST is not None and _HOST.window() is window and not active():shutdown()
    checked=active() and _HOST.window() is window
    if window.pencil_curve_button.isChecked()!=checked:set_button(window,checked)


def set_button(window,checked):
    button=window.pencil_curve_button;button.setChecked(checked)
    name='pencil_curve_active.svg' if checked else 'pencil_curve.svg'
    button.setIcon(QtGui.QIcon(str(Path(__file__).resolve().parent/'icons'/name)))


def option(name,default):return cmds.optionVar(q=name) if cmds.optionVar(exists=name) else default


class Host(QtCore.QObject):
    def __init__(self,window):
        super().__init__(window);self.window_ref=weakref.ref(window);self.panel=None
        self.compact_size=QtCore.QSize(window.size());self.stopped=False;self.switching=False
        self.callbacks=[];self.points=[];self.positions=[];self.drawing=False;self.interrupted=False
        self.view=None;self.viewport_panel=None;self.targets=[];self.normal=None;self.origin=None
        self.units_per_pixel=.01;self.pixel_ratio=1.;self.overlay=WireOverlay();self.last_curve=None
        self.plane=str(option('ModelPack_PencilPlane','view'))
        if self.plane not in ('view','surface','xy','xz','yz'):self.plane='view'
        self.tolerance=max(.25,min(20.,float(option('ModelPack_PencilTolerance',2.))))
        self.auto_close=bool(option('ModelPack_PencilAutoClose',1))
        self.undo_group=None

    def window(self):
        window=self.window_ref();return window if window is not None and isValid(window) else None

    def start(self):
        from model_pack_theme import UndoGroup
        self.undo_group=UndoGroup('Pencil Curve')
        window=self.window();self.panel=QtWidgets.QWidget(window)
        self.panel.setObjectName('modelPackPencilCurveSettings')
        layout=QtWidgets.QVBoxLayout(self.panel);layout.setContentsMargins(0,5,0,0);layout.setSpacing(6)
        self.plane_choice=QtWidgets.QComboBox(self.panel)
        for label,value in (('View Plane','view'),('Surface','surface'),('World XY','xy'),('World XZ','xz'),('World YZ','yz')):
            self.plane_choice.addItem(label,value)
        self.plane_choice.setCurrentIndex(self.plane_choice.findData(self.plane))
        self.plane_choice.setToolTip('View Plane — плоскость экрана через точку на меше или центр выделения. Surface — по поверхности меша. World — заданная плоскость.')
        self.plane_choice.currentIndexChanged.connect(self.change_plane);layout.addWidget(self.plane_choice)
        row=QtWidgets.QHBoxLayout();row.addWidget(QtWidgets.QLabel('Simplify (px)',self.panel))
        self.accuracy=QtWidgets.QDoubleSpinBox(self.panel);self.accuracy.setRange(.25,20.);self.accuracy.setDecimals(2)
        self.accuracy.setSingleStep(.25);self.accuracy.setValue(self.tolerance);self.accuracy.setKeyboardTracking(False)
        self.accuracy.setToolTip('Больше — меньше точек и плавнее кривая. Меньше — точнее повторяет штрих.')
        self.accuracy.valueChanged.connect(self.change_tolerance);row.addWidget(self.accuracy);layout.addLayout(row)
        self.close_check=QtWidgets.QCheckBox('Auto Close',self.panel);self.close_check.setChecked(self.auto_close)
        self.close_check.setToolTip('Замкнуть кривую, если конец штриха вернулся к началу.')
        self.close_check.toggled.connect(self.change_close);layout.addWidget(self.close_check)
        self.edit_button=QtWidgets.QPushButton('Edit Points',self.panel);self.edit_button.setMinimumHeight(30)
        self.edit_button.setAutoDefault(False);self.edit_button.setEnabled(False)
        self.edit_button.clicked.connect(self.edit_points);layout.addWidget(self.edit_button)
        self.hint=QtWidgets.QLabel('Draw with LMB · Esc cancels · Q / W / E / R exit',self.panel)
        self.hint.setWordWrap(True);self.hint.setAlignment(QtCore.Qt.AlignCenter)
        self.hint.setStyleSheet('color:#a4adb7;font-size:10px;');layout.addWidget(self.hint)
        window.layout().addWidget(self.panel,window._panel_row,0,1,2);self.panel.show()
        cmds.draggerContext(CONTEXT,pressCommand=self.press,dragCommand=self.drag,releaseCommand=self.release,
            finalize=self.finalize,cursor='crossHair',space='screen',undoMode='all')
        cmds.setToolTo(CONTEXT)
        self.callbacks=[om.MEventMessage.addEventCallback('ToolChanged',self.tool_changed)]
        self.callbacks.extend(om.MSceneMessage.addCallback(kind,lambda *_:shutdown())
            for kind in (om.MSceneMessage.kBeforeNew,om.MSceneMessage.kBeforeOpen))
        QtWidgets.QApplication.instance().installEventFilter(self)
        set_button(window,True);window.layout().activate()
        window.resize(window.width(),window.sizeHint().height())

    def change_plane(self,*_):
        self.cancel();self.plane=self.plane_choice.currentData()
        cmds.optionVar(stringValue=('ModelPack_PencilPlane',self.plane))

    def change_tolerance(self,value):
        self.tolerance=float(value);cmds.optionVar(floatValue=('ModelPack_PencilTolerance',self.tolerance))

    def change_close(self,value):
        self.auto_close=bool(value);cmds.optionVar(intValue=('ModelPack_PencilAutoClose',int(value)))

    def position(self,anchor=False):
        return tuple(cmds.draggerContext(CONTEXT,q=True,**{'anchorPoint' if anchor else 'dragPoint':True})[:2])

    def ray(self,position):
        origin,direction=om.MPoint(),om.MVector()
        self.view.viewToWorld(int(position[0]),int(position[1]),origin,direction)
        return origin,direction.normal()

    def meshes(self):
        panel=self.viewport_panel
        if panel and not cmds.modelEditor(panel,q=True,polymeshes=True):return []
        allowed=None
        if panel and cmds.isolateSelect(panel,q=True,state=True):
            group=cmds.isolateSelect(panel,q=True,viewObjects=True);allowed=set()
            for member in cmds.sets(group,q=True) or []:
                allowed.update(cmds.ls(member.split('.')[0],dag=True,allPaths=True,type='mesh',long=True) or [])
        result=[]
        for name in cmds.ls(dag=True,allPaths=True,type='mesh',long=True,noIntermediate=True,visible=True) or []:
            path=core.path_for(name)
            if path.isVisible() and (allowed is None or name in allowed):result.append(path)
        return result

    def plane_origin(self,hit):
        if hit is not None:return om.MPoint(hit.point)
        camera=om.MFnCamera(self.view.getCamera())
        selected=cmds.ls(sl=True,long=True) or []
        if selected:
            bounds=cmds.exactWorldBoundingBox(selected)
            center=om.MPoint(*(core.internal_distance((bounds[i]+bounds[i+3])*.5) for i in range(3)))
            if (center-camera.eyePoint(om.MSpace.kWorld))*camera.viewDirection(om.MSpace.kWorld)>camera.nearClippingPlane:return center
        if self.plane in ('xy','xz','yz'):return om.MPoint()
        return camera.eyePoint(om.MSpace.kWorld)+camera.viewDirection(om.MSpace.kWorld).normal()*camera.centerOfInterest

    def press(self,*_):
        if self.stopped or self.switching:return
        try:
            if cmds.getModifiers()&8:return
            if cmds.draggerContext(CONTEXT,q=True,button=True)!=1:return
            self.cancel();position=self.position(anchor=True)
            panel=cmds.getPanel(underPointer=True)
            self.viewport_panel=panel if panel and cmds.getPanel(typeOf=panel)=='modelPanel' else None
            self.view=omui.M3dView.getM3dViewFromModelPanel(panel) if self.viewport_panel else omui.M3dView.active3dView()
            self.targets=self.meshes();hit=core.raycast(*self.ray(position),self.targets) if self.targets else None
            self.origin=self.plane_origin(hit)
            camera=om.MFnCamera(self.view.getCamera());facing=camera.viewDirection(om.MSpace.kWorld).normal()
            self.normal={'xy':om.MVector(0,0,1),'xz':om.MVector(0,1,0),'yz':om.MVector(1,0,0)}.get(self.plane,facing)
            if self.plane=='surface':
                if hit is None:raise ValueError('Начните штрих на видимом меше или выберите View Plane.')
                self.targets=[hit.path];point=om.MPoint(hit.point)
            else:
                point=core.intersect_plane(*self.ray(position),self.origin,self.normal)
                if point is None:raise ValueError('Плоскость видна с ребра. Поверните камеру или выберите View Plane.')
            origin,direction=self.ray(position)
            base=core.intersect_plane(origin,direction,point,facing)
            adjacent=core.intersect_plane(*self.ray((position[0]+1,position[1])),point,facing)
            self.units_per_pixel=max((adjacent-base).length() if adjacent is not None and base is not None else .01,1e-9)
            self.pixel_ratio=1.
            if not cmds.about(batch=True):
                widget=wrapInstance(int(self.view.widget()),QtWidgets.QWidget)
                if widget and widget.width():self.pixel_ratio=self.view.portWidth()/widget.width()
            self.drawing=True;self.points=[point];self.positions=[position]
            self.draw_preview()
        except (RuntimeError,ValueError,TypeError) as error:self.fail(error)

    def drag(self,*_,force=False):
        if not self.drawing or self.interrupted or self.stopped:return
        try:
            position=self.position()
            if not force and math.dist(position,self.positions[-1])<self.pixel_ratio:return
            if self.plane=='surface':
                hit=core.raycast(*self.ray(position),self.targets)
                if hit is None:
                    self.interrupted=True;self.hint.setText('End of surface · release to finish');return
                point=om.MPoint(hit.point)
            else:point=core.intersect_plane(*self.ray(position),self.origin,self.normal)
            if point is None:return
            if (point-self.points[-1]).length()<=self.units_per_pixel*.01:return
            if len(self.points)>=8000:
                self.interrupted=True;self.hint.setText('Long stroke · release to finish');return
            self.points.append(point);self.positions.append(position);self.draw_preview()
        except (RuntimeError,ValueError,TypeError) as error:self.fail(error)

    def draw_preview(self):
        self.overlay.points=[om.MPoint(p) for p in self.points]
        self.overlay.edges=list(zip(range(len(self.points)-1),range(1,len(self.points))))
        self.overlay.color=TURQUOISE
        if not cmds.about(batch=True):self.overlay.bind(self.view)

    def release(self,*_):
        if not self.drawing or self.stopped:return
        try:
            self.drag(force=True)
            if not self.drawing:return
            self.drawing=False
            length=sum(math.dist(a,b) for a,b in zip(self.positions,self.positions[1:]))/self.pixel_ratio
            if len(self.points)<2 or length<3:self.cancel();return
            tolerance=self.units_per_pixel*self.pixel_ratio*self.tolerance
            closed=self.auto_close and length>30 and math.dist(self.positions[0],self.positions[-1])/self.pixel_ratio<=6
            result=create(self.points,tolerance,closed=closed)
            self.last_curve=cmds.ls(result,uuid=True)[0];count=curve_fn(result).numCVs
            self.cancel();self.edit_button.setEnabled(True)
            self.hint.setText('Points: {} · draw next stroke or Edit Points'.format(count))
        except (RuntimeError,ValueError,TypeError) as error:self.fail(error)

    def cancel(self):
        self.drawing=False;self.interrupted=False;self.points=[];self.positions=[];self.targets=[];self.overlay.close()

    def fail(self,error):
        self.cancel();self.hint.setText(str(error));cmds.warning('Pencil Curve: '+str(error))

    def finalize(self,*_):
        self.cancel();QtCore.QTimer.singleShot(0,self.check_context)

    def tool_changed(self,*_):QtCore.QTimer.singleShot(0,self.check_context)

    def check_context(self):
        if not self.stopped and _HOST is self and cmds.currentCtx()!=CONTEXT:shutdown()

    def edit_points(self):
        names=cmds.ls(self.last_curve,long=True) if self.last_curve else []
        if not names:cmds.warning('Pencil Curve: нарисуйте кривую перед редактированием.');return
        name=names[0];shutdown();cmds.setToolTo('moveSuperContext')
        cmds.selectMode(component=True);cmds.selectType(allComponents=False,controlVertex=True)
        cmds.hilite(name,replace=True);cmds.select(name+'.cv[*]',replace=True)

    def owns_input(self,target):
        if not isinstance(target,QtWidgets.QWidget):return False
        window=self.window()
        if window is not None and (target is window or window.isAncestorOf(target)):return True
        for panel in cmds.getPanel(type='modelPanel') or []:
            try:
                widget=wrapInstance(int(omui.M3dView.getM3dViewFromModelPanel(panel).widget()),QtWidgets.QWidget)
                if widget is not None and (target is widget or widget.isAncestorOf(target) or
                        self.drawing and target is widget.window()):return True
            except (RuntimeError,TypeError):continue
        return False

    def eventFilter(self,target,event):
        if self.stopped or not active():return False
        if event.type()==QtCore.QEvent.ApplicationDeactivate:self.cancel();return False
        if event.type() not in (QtCore.QEvent.KeyPress,QtCore.QEvent.KeyRelease,QtCore.QEvent.ShortcutOverride):return False
        inputs=(QtWidgets.QLineEdit,QtWidgets.QAbstractSpinBox,QtWidgets.QTextEdit,QtWidgets.QPlainTextEdit)
        if isinstance(target,inputs):return False
        if not self.drawing and isinstance(QtWidgets.QApplication.instance().focusWidget(),inputs):return False
        undo=event.matches(QtGui.QKeySequence.Undo) or (event.key()==QtCore.Qt.Key_Z and not event.modifiers())
        redo=event.matches(QtGui.QKeySequence.Redo) or (event.key()==QtCore.Qt.Key_Z and event.modifiers()==QtCore.Qt.ShiftModifier)
        if (undo or redo) and self.owns_input(target):
            if event.type()==QtCore.QEvent.KeyPress and not event.isAutoRepeat():
                self.cancel();cmds.undo() if undo else cmds.redo()
            event.accept();return True
        key=event.key()
        if key!=QtCore.Qt.Key_Escape and key not in MANIPULATORS:return False
        if event.modifiers()!=QtCore.Qt.NoModifier or not self.owns_input(target):return False
        if event.type()==QtCore.QEvent.KeyPress and not event.isAutoRepeat() and not self.switching:
            self.cancel()
            if key in MANIPULATORS:
                self.switching=True
                def switch():
                    if self.stopped or _HOST is not self:return
                    context=MANIPULATORS[key];shutdown();cmds.setToolTo(context)
                QtCore.QTimer.singleShot(0,switch)
            else:self.hint.setText('Stroke cancelled · draw a new line')
        event.accept();return True

    def stop(self):
        if self.stopped:return
        self.stopped=True;self.cancel()
        app=QtWidgets.QApplication.instance()
        if app is not None:app.removeEventFilter(self)
        for callback in self.callbacks:om.MMessage.removeCallback(callback)
        self.callbacks=[]
        if cmds.currentCtx()==CONTEXT:cmds.setToolTo('selectSuperContext')
        if cmds.draggerContext(CONTEXT,exists=True):cmds.deleteUI(CONTEXT,toolContext=True)
        if self.undo_group:self.undo_group.close();self.undo_group=None
        window=self.window()
        if self.panel is not None and isValid(self.panel):
            self.panel.hide()
            if window is not None:window.layout().removeWidget(self.panel)
            self.panel.deleteLater()
        if window is not None:
            if window._active_tool=='pencil_curve':window._active_tool=None
            set_button(window,False);window.layout().activate();window.resize(self.compact_size)
        self.deleteLater()
