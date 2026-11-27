"""Model Pack Quad Dome preview, compact settings and native Undo integration."""
import math
import weakref
from pathlib import Path
from maya import cmds
from maya.api import OpenMaya as om,OpenMayaUI as omui
from PySide6 import QtCore,QtGui,QtWidgets
from shiboken6 import isValid,wrapInstance
from model_pack_offset import transient,display_scale,MANIPULATORS
from model_pack_quad_dome_mesh import Mesh

CONTEXT='modelPackQuadDomeContext'
_HOST=None


class Session:
    def __init__(self,mesh=None,faces=None):
        self.geometry=Mesh(mesh,faces);self.mesh=self.geometry.path;self.closed=False;self.preview=None
        self.selection=om.MGlobal.getActiveSelectionList();self.hilite=om.MGlobal.getHiliteList()
        self.mode=om.MGlobal.selectionMode();self.mask=om.MGlobal.componentSelectionMask()
        self.visible=cmds.getAttr(self.mesh+'.visibility')
        if not cmds.getAttr(self.mesh+'.visibility',settable=True):raise ValueError('Quad Dome: видимость меша заблокирована.')
        self.height=self.geometry.default_height;self.rings=3;self.split_sides=True;self.after=None

    def update(self,height=None,rings=None,split_sides=None):
        if self.closed:raise ValueError('Quad Dome уже завершён.')
        height=self.height if height is None else float(height)
        rings=self.rings if rings is None else int(rings)
        split_sides=self.split_sides if split_sides is None else bool(split_sides)
        if not math.isfinite(height):raise ValueError('Quad Dome: высота должна быть конечным числом.')
        if not 1<=rings<=32:raise ValueError('Quad Dome: Rings должен быть от 1 до 32.')
        if abs(height)<=self.geometry.patch.scale*1e-6:
            self.geometry.validate();self.remove_preview()
            self.height,self.rings,self.split_sides=0.,rings,split_sides
            self.after=None;self.selected=[];self.owners=[];self.patch=self.geometry.patch
            return None
        data,selected,owners,patch=self.geometry.build(height,rings,split_sides)
        with transient():
            if self.preview is None:
                self.preview=cmds.createNode('transform',name='quadDomePreview#')
                parent=om.MSelectionList().add(self.preview).getDependNode(0)
                om.MFnTransform(parent).setTransformation(om.MTransformationMatrix(self.geometry.matrix))
                copied=om.MFnMesh();copied.copy(data,parent);self.preview_shape=copied.fullPathName()
                for panel in cmds.getPanel(type='modelPanel') or []:
                    if cmds.isolateSelect(panel,q=True,state=True):
                        group=cmds.isolateSelect(panel,q=True,viewObjects=True)
                        if group:cmds.sets(self.preview,add=group)
            else:
                plug=om.MFnDependencyNode(om.MSelectionList().add(self.preview_shape).getDependNode(0)).findPlug('cachedInMesh',False)
                modifier=om.MDGModifier();modifier.newPlugValue(plug,data);modifier.doIt()
            for index,shader in enumerate(self.geometry.shaders):
                members=[self.preview_shape+'.f[%d]'%f for f,owner in enumerate(owners) if self.geometry.materials[owner]==index]
                if members:cmds.sets(members,e=True,forceElement=om.MFnDependencyNode(shader).name())
            cmds.setAttr(self.preview_shape+'.displayColors',cmds.getAttr(self.mesh+'.displayColors'))
            cmds.setAttr(self.mesh+'.visibility',False)
            cmds.setAttr(self.preview+'.visibility',self.visible)
            selected_list=om.MSelectionList()
            for face in selected:selected_list.add(self.preview_shape+'.f[%d]'%face)
            hilite=om.MSelectionList();hilite.add(self.preview)
            om.MGlobal.setHiliteList(hilite);om.MGlobal.setActiveSelectionList(selected_list)
        self.after,self.selected,self.owners,self.patch=data,selected,owners,patch
        self.height,self.rings,self.split_sides=height,rings,split_sides
        return data

    def remove_preview(self):
        with transient():
            if self.preview and cmds.objExists(self.preview):cmds.delete(self.preview)
            self.preview=None
            if self.geometry.handle.isValid() and self.geometry.handle.isAlive():
                path=om.MDagPath.getAPathTo(self.geometry.handle.object());self.mesh=path.fullPathName()
                cmds.setAttr(self.mesh+'.visibility',self.visible)
            om.MGlobal.setSelectionMode(self.mode);om.MGlobal.setComponentSelectionMask(self.mask)
            om.MGlobal.setHiliteList(self.hilite);om.MGlobal.setActiveSelectionList(self.selection)

    def cancel(self):
        if self.closed:return
        self.remove_preview();self.closed=True

    def commit(self):
        if self.closed:return None
        self.geometry.validate()
        if self.after is None:self.cancel();return None
        if not cmds.undoInfo(q=True,state=True):raise ValueError('Quad Dome: включите Undo для применения.')
        import recurve
        if recurve.SESSION is not None:raise ValueError('Завершите текущий Control Curve.')
        if not cmds.pluginInfo('recurve',q=True,loaded=True):cmds.loadPlugin(str(Path(__file__).with_name('recurve.py')))
        self.mesh=self.geometry.path;self.remove_preview()
        selections=om.MSelectionList()
        for face in self.selected:selections.add(self.mesh+'.f[%d]'%face)
        recurve.PENDING=(self.mesh,self.geometry.before,self.after,self.selection,selections)
        changed=False;cmds.undoInfo(openChunk=True,chunkName='Quad Dome')
        try:
            cmds.recurveApply();changed=True
            # Author source UV arrays as native attributes for MA/MB persistence.
            source=cmds.listConnections(self.mesh+'.inMesh',s=True,d=False,p=True)[0].split('.')[0]
            fn=om.MFnMesh(self.after)
            for i,name in enumerate(fn.getUVSetNames()):
                cmds.setAttr(source+'.uvSet[%d].uvSetName'%i,name,type='string')
                us,vs=fn.getUVs(name)
                if len(us):cmds.setAttr(source+'.uvSet[%d].uvSetPoints[0:%d]'%(i,len(us)-1),*[v for pair in zip(us,vs) for v in pair],type='float2')
            if fn.getUVSetNames():cmds.setAttr(source+'.currentUVSet',fn.currentUVSetName(),type='string')
            if self.geometry.shaders:
                from slide_for_maya.edit_session import assign_materials
                assign_materials(self.mesh,[om.MFnDependencyNode(s).name() for s in self.geometry.shaders],
                                 [self.geometry.materials[f] for f in self.owners])
        except Exception:
            cmds.undoInfo(closeChunk=True)
            if changed:cmds.undo()
            self.closed=True;raise
        finally:recurve.PENDING=None
        cmds.undoInfo(closeChunk=True);self.closed=True
        return [self.mesh+'.f[%d]'%f for f in self.selected]


def active():return _HOST is not None and not _HOST.stopped


def shutdown(apply=False):
    global _HOST
    host,_HOST=_HOST,None
    if host is not None and isValid(host):host.stop(apply)


def toggle(window):
    global _HOST
    if active() and _HOST.window() is window:shutdown(apply=True);return
    shutdown();window._prepare_tool('quad_dome')
    _HOST=Host(window)
    try:_HOST.start()
    except Exception as exc:shutdown();cmds.warning(str(exc))


def set_button(window,checked):
    window.quad_dome_button.setChecked(checked)
    name='quad_dome_active.svg' if checked else 'quad_dome.svg'
    window.quad_dome_button.setIcon(QtGui.QIcon(str(Path(__file__).resolve().parent/'icons'/name)))


def sync(window):
    if _HOST is not None and _HOST.window() is window and cmds.currentCtx()!=CONTEXT:shutdown(apply=True)
    checked=active() and _HOST.window() is window
    if window.quad_dome_button.isChecked()!=checked:set_button(window,checked)


class Host(QtCore.QObject):
    def __init__(self,window):
        super().__init__(window);self.window_ref=weakref.ref(window);self.panel=None;self.session=None
        self.compact_size=QtCore.QSize(window.size());self.stopped=False;self.callbacks=[];self.anchor=None
        self.valid=False;self.history=None
    def window(self):
        w=self.window_ref();return w if w is not None and isValid(w) else None
    def start(self):
        self.session=Session();window=self.window();self.panel=QtWidgets.QWidget(window)
        self.panel.setObjectName('modelPackQuadDomeSettings');layout=QtWidgets.QVBoxLayout(self.panel)
        layout.setContentsMargins(0,5,0,0);layout.setSpacing(6)
        self.height=QtWidgets.QDoubleSpinBox(self.panel);self.height.setDecimals(3)
        self.height.setRange(-1000000.,1000000.)
        self.height.setToolTip('Положительное значение — купол, отрицательное — углубление, 0 — исходная поверхность.')
        self.height.setKeyboardTracking(False);self.height.setSingleStep(max(self.session.height*display_scale()*.05,.001))
        self.rings=QtWidgets.QSpinBox(self.panel);self.rings.setRange(1,32);self.rings.setValue(3);self.rings.setKeyboardTracking(False)
        for title,field in (('Height',self.height),('Rings',self.rings)):
            row=QtWidgets.QHBoxLayout();row.addWidget(QtWidgets.QLabel(title,self.panel));row.addWidget(field);layout.addLayout(row)
        self.split=QtWidgets.QCheckBox('Split Side Strip',self.panel);self.split.setChecked(True)
        self.split.setToolTip('Для нечётного контура добавить одну вершину и продолжить разрез через соседние квады.')
        self.split.setVisible(len(self.session.geometry.boundary)%2==1);layout.addWidget(self.split)
        hint=QtWidgets.QLabel('Height + / − · 0 — Original Surface\nDrag LMB — Height · Shift+Wheel — Rings',self.panel)
        hint.setWordWrap(True);hint.setAlignment(QtCore.Qt.AlignCenter);hint.setStyleSheet('color:#a4adb7;font-size:10px;');layout.addWidget(hint)
        row=QtWidgets.QHBoxLayout();self.apply_button=QtWidgets.QPushButton('Apply',self.panel);self.cancel_button=QtWidgets.QPushButton('Cancel',self.panel)
        for button in (self.apply_button,self.cancel_button):button.setAutoDefault(False);button.setMinimumHeight(30);row.addWidget(button)
        self.apply_button.setProperty('primary',True);self.apply_button.clicked.connect(lambda:shutdown(apply=True))
        self.cancel_button.clicked.connect(lambda:shutdown());layout.addLayout(row)
        self.height.valueChanged.connect(self.change);self.rings.valueChanged.connect(self.change);self.split.toggled.connect(self.change)
        window.layout().addWidget(self.panel,window._panel_row,0,1,2);self.panel.show()
        cmds.draggerContext(CONTEXT,pressCommand=self.press,dragCommand=self.drag,releaseCommand=self.release,cursor='hand',space='screen',undoMode='all')
        cmds.setToolTo(CONTEXT)
        self.callbacks=[om.MEventMessage.addEventCallback('ToolChanged',lambda *_:self.tool_changed())]
        self.callbacks.extend(om.MSceneMessage.addCallback(kind,lambda *_:shutdown()) for kind in (om.MSceneMessage.kBeforeNew,om.MSceneMessage.kBeforeOpen))
        self.height.setValue(self.session.height*display_scale())
        QtWidgets.QApplication.instance().installEventFilter(self);set_button(window,True)
        from model_pack_theme import PreviewHistory
        self.history=PreviewHistory(self,(self.height,self.rings,self.split),self.change)
        window.layout().activate();window.resize(window.width(),window.sizeHint().height())
    def change(self,*_):
        if self.stopped:return
        try:
            self.session.update(self.height.value()/display_scale(),self.rings.value(),self.split.isChecked())
            self.valid=True;self.apply_button.setEnabled(self.session.after is not None);cmds.refresh(currentView=True)
        except Exception as exc:
            self.valid=False;self.apply_button.setEnabled(False);cmds.warning(str(exc))
    def press(self):
        if cmds.draggerContext(CONTEXT,q=True,button=True)!=1 or QtWidgets.QApplication.keyboardModifiers()&QtCore.Qt.AltModifier:self.anchor=None;return
        self.anchor=tuple(cmds.draggerContext(CONTEXT,q=True,anchorPoint=True));self.start_height=self.session.height
        if self.history:self.history.begin()
    def drag(self):
        if self.anchor is None:return
        end=cmds.draggerContext(CONTEXT,q=True,dragPoint=True)
        self.height.setValue((self.start_height+(end[0]-self.anchor[0])*self.session.geometry.patch.scale*.003)*display_scale())
    def release(self):
        self.drag();self.anchor=None
        if self.history:self.history.end()
    def tool_changed(self):
        QtCore.QTimer.singleShot(0, self.finish_context_change)

    def finish_context_change(self):
        if self.stopped or _HOST is not self or cmds.currentCtx() == CONTEXT:
            return
        timer = getattr(self.window(), '_scene_reset_timer', None)
        if timer is None or not timer.isActive():
            shutdown(apply=True)
    def owns_input(self,target):
        window=self.window()
        if window is not None and (target is window or window.isAncestorOf(target)):return True
        for panel in cmds.getPanel(type='modelPanel') or []:
            try:
                widget=wrapInstance(int(omui.M3dView.getM3dViewFromModelPanel(panel).widget()),QtWidgets.QWidget)
                if target is widget or widget.isAncestorOf(target):return True
            except (RuntimeError,ValueError):pass
        return False
    def eventFilter(self,target,event):
        if self.stopped or not active():return False
        if event.type()==QtCore.QEvent.Wheel and event.modifiers()==QtCore.Qt.ShiftModifier and self.owns_input(target):
            self.rings.setValue(self.rings.value()+(1 if event.angleDelta().y()>0 else -1));event.accept();return True
        if event.type() not in (QtCore.QEvent.KeyPress,QtCore.QEvent.KeyRelease,QtCore.QEvent.ShortcutOverride):return False
        inputs=(QtWidgets.QLineEdit,QtWidgets.QAbstractSpinBox,QtWidgets.QTextEdit,QtWidgets.QPlainTextEdit)
        if isinstance(target,inputs) or isinstance(QtWidgets.QApplication.instance().focusWidget(),inputs):return False
        if event.modifiers()!=QtCore.Qt.NoModifier or not self.owns_input(target):return False
        key=event.key()
        if key not in (QtCore.Qt.Key_Return,QtCore.Qt.Key_Enter,QtCore.Qt.Key_Escape) and key not in MANIPULATORS:return False
        if event.type()==QtCore.QEvent.KeyPress and not event.isAutoRepeat():
            if key in (QtCore.Qt.Key_Return,QtCore.Qt.Key_Enter) and not self.valid:
                event.accept();return True
            context=MANIPULATORS.get(key);shutdown(apply=key!=QtCore.Qt.Key_Escape)
            if context:cmds.setToolTo(context)
        event.accept();return True
    def stop(self,apply=False):
        if self.stopped:return
        if self.history:self.history.close()
        self.stopped=True
        app=QtWidgets.QApplication.instance()
        if app:app.removeEventFilter(self)
        for callback in self.callbacks:om.MMessage.removeCallback(callback)
        self.callbacks=[]
        if cmds.currentCtx()==CONTEXT:cmds.setToolTo('selectSuperContext')
        if cmds.draggerContext(CONTEXT,exists=True):cmds.deleteUI(CONTEXT,toolContext=True)
        try:
            if self.session is not None:
                if apply and self.valid:
                    try:self.session.commit()
                    except Exception as exc:self.session.cancel();cmds.warning(str(exc))
                else:self.session.cancel()
        finally:
            window=self.window()
            if self.panel is not None and isValid(self.panel):
                self.panel.hide()
                if window is not None:window.layout().removeWidget(self.panel)
                self.panel.deleteLater()
            if window is not None:
                if window._active_tool=='quad_dome':window._active_tool=None
                set_button(window,False);window.layout().activate();window.resize(self.compact_size)
            self.deleteLater()
