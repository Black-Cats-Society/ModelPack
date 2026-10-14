"""Gold reflection plane, translation arrows, rotation rings and axis squares."""
import math
from PySide6 import QtCore,QtGui,QtWidgets
from shiboken6 import isValid
from maya import cmds
from maya.api import OpenMaya as om
from model_pack_linear_shift import overlay
from model_pack_bevel.session import without_undo
import model_pack_mirror_plane as plane
import model_pack_mirror_snap as snapping

GOLD=QtGui.QColor(245,174,48)
HOT=QtGui.QColor(255,215,115)
POSITIVE=QtGui.QColor(88,211,111)
NEGATIVE=QtGui.QColor(235,84,75)
GIZMO_PIXELS=100.
SIDE_RADIUS=6.


def distance(point,a,b):
    dx,dy=b[0]-a[0],b[1]-a[1];squared=dx*dx+dy*dy
    t=max(0.,min(1.,((point[0]-a[0])*dx+(point[1]-a[1])*dy)/squared)) if squared>1e-12 else 0.
    return math.hypot(point[0]-a[0]-t*dx,point[1]-a[1]-t*dy)


def global_scale():
    value=cmds.manipOptions(q=True,scale=True)
    return float(value[0] if isinstance(value,(list,tuple)) else value)


def project_geometry(node,pose,projection,size=1.):
    center=plane.origin(pose);axes=[v.normal() for v in plane.basis(pose)]
    start=projection.point(center)
    lengths=[math.hypot(projection.point(center+v)[0]-start[0],projection.point(center+v)[1]-start[1]) for v in axes]
    scale=GIZMO_PIXELS*size/max(max(lengths),1e-6)
    controls=[]
    for index,axis in enumerate(axes):
        segment=projection.line(center+axis*scale*.15,center+axis*scale)
        if segment:controls.append(dict(kind='move',axis=index,segments=[segment],tip=projection.point(center+axis*scale)))
        midpoint=projection.point(center+axis*scale*.50)
        if midpoint[2] and cmds.nodeType(node)=='modelPackMirrorCut':
            controls.append(dict(kind='direction',axis=index,point=midpoint[:2]))
        other=[v for i,v in enumerate(axes) if i!=index][0]
        u=other-axis*(other*axis)
        if u.length()<1e-10:u=min((om.MVector(1,0,0),om.MVector(0,1,0),om.MVector(0,0,1)),key=lambda v:abs(v*axis));u-=axis*(u*axis)
        u.normalize();v=axis^u
        ring=[center+(u*math.cos(a*math.tau/64)+v*math.sin(a*math.tau/64))*scale*.66 for a in range(64)]
        segments=[segment for a,b in zip(ring,ring[1:]+ring[:1]) if (segment:=projection.line(a,b)) is not None]
        controls.append(dict(kind='rotate',axis=index,segments=segments))
        flat=[i for i in range(3) if i!=index]
        square=[center+(axes[flat[0]]*x+axes[flat[1]]*y)*scale for x,y in ((.68,.68),(.90,.68),(.90,.90),(.68,.90))]
        polygon=projection.polygon(square)
        if polygon:
            low=[min(p[i] for p in polygon) for i in (0,1)];high=[max(p[i] for p in polygon) for i in (0,1)]
            if min(high[i]-low[i] for i in (0,1))<14:
                # An edge-on plane should still have a readable, clickable
                # selector. Keep its projected position with a minimum face.
                middle=[(a+b)*.5 for a,b in zip(low,high)]
                half=[max(7.,(b-a)*.5) for a,b in zip(low,high)]
                polygon=[(middle[0]+x*half[0],middle[1]+y*half[1]) for x,y in ((-1,-1),(1,-1),(1,1),(-1,1))]
            controls.append(dict(kind='axis',axis=index,polygon=polygon,segments=list(zip(polygon,polygon[1:]+polygon[:1]))))
    if start[2]:controls.append(dict(kind='free',axis=None,point=start[:2]))
    return dict(node=node,pose=pose,center=center,axes=axes,scale=scale,controls=controls,plane=projection.polygon(plane.bounds(node,pose)))


def pick(layouts,point):
    candidates=[]
    for layout_index,layout in enumerate(layouts):
        for control in layout['controls']:
            kind=control['kind']
            if kind in ('free','direction'):
                value=math.hypot(point[0]-control['point'][0],point[1]-control['point'][1]);priority=0
                if value>(SIDE_RADIUS+2 if kind=='direction' else 7):continue
            elif kind=='axis':
                polygon=QtGui.QPolygonF([QtCore.QPointF(*p) for p in control['polygon']])
                inside=polygon.containsPoint(QtCore.QPointF(*point),QtCore.Qt.OddEvenFill)
                value=0. if inside else min((distance(point,a,b) for a,b in control['segments']),default=1e9);priority=0
                if value>5:continue
            else:
                value=min((distance(point,a,b) for a,b in control['segments']),default=1e9);priority=1 if kind=='move' else 2
                if value>7:continue
            candidates.append((priority,value,layout_index,control))
    if not candidates:return None
    _,_,index,control=min(candidates,key=lambda item:item[:3])
    return layouts[index],control


class Canvas(overlay.Canvas):
    def __init__(self,parent,manager):
        super().__init__(parent,manager);self.setObjectName('modelPackMirrorOverlay')

    def visible_region(self):
        # This composited window is above Maya's native GPU viewport. Cut holes
        # for floating Qt windows so it cannot cover the toolbox or dialogs.
        region=QtGui.QRegion(self.rect());origin=self.mapToGlobal(QtCore.QPoint())
        for window in QtWidgets.QApplication.topLevelWidgets():
            if window in (self,self.viewport.window()) or not window.isVisible() or window.isMinimized():continue
            if window.windowFlags()&QtCore.Qt.WindowTransparentForInput:continue
            rect=window.frameGeometry().translated(-origin)
            if rect.intersects(self.rect()):region-=QtGui.QRegion(rect)
        return region

    def sync_occlusion(self):
        region=self.visible_region()
        # Qt treats an empty mask as 'clear the mask', so keep an empty visible
        # area represented by a tiny region completely outside this window.
        mask=region if not region.isEmpty() else QtGui.QRegion(-2,-2,1,1)
        if self.mask()!=mask:self.setMask(mask);self.update()
        return region

    def paint_frame(self,painter):
        painter.save()
        try:
            painter.setCompositionMode(QtGui.QPainter.CompositionMode_Source);painter.fillRect(self.rect(),QtCore.Qt.transparent)
            painter.setCompositionMode(QtGui.QPainter.CompositionMode_SourceOver);painter.setRenderHint(QtGui.QPainter.Antialiasing)
            painter.setClipRegion(self.sync_occlusion())
            self.manager.layouts=self.manager.layout(overlay.Projection(self.manager.view,self.width(),self.height()))
            for layout in self.manager.layouts:self.draw_layout(painter,layout)
            target=self.manager.snap_target
            if target is not None:
                x,y,visible=overlay.Projection(self.manager.view,self.width(),self.height()).point(target)
                if visible:
                    center=QtCore.QPointF(x,self.height()-1-y)
                    painter.setPen(QtGui.QPen(HOT,2));painter.setBrush(QtCore.Qt.NoBrush)
                    painter.drawEllipse(center,7,7)
                    painter.drawLine(center+QtCore.QPointF(-10,0),center+QtCore.QPointF(10,0))
                    painter.drawLine(center+QtCore.QPointF(0,-10),center+QtCore.QPointF(0,10))
        finally:painter.restore()

    def draw_layout(self,painter,layout):
        def point(p):return QtCore.QPointF(p[0],self.height()-1-p[1])
        def letter(center,index):
            # Small vector letters stay readable on Maya's GPU overlay and
            # do not depend on the platform's Qt font initialization.
            strokes=((( -3,-4),(3,4)),((-3,4),(3,-4))) if index==0 else (
                ((-3,-4),(0,0),(3,-4)),((0,0),(0,4))) if index==1 else (((-3,-4),(3,-4),(-3,4),(3,4)),)
            for stroke in strokes:
                for a,b in zip(stroke,stroke[1:]):
                    painter.drawLine(center+QtCore.QPointF(*a),center+QtCore.QPointF(*b))
        polygon=layout['plane'];fill=QtGui.QColor(GOLD);fill.setAlpha(28)
        painter.setPen(QtGui.QPen(GOLD,1.4));painter.setBrush(fill)
        if polygon:painter.drawPolygon(QtGui.QPolygonF([point(p) for p in polygon]))
        axis=cmds.getAttr(layout['node']+'.'+plane.fields(layout['node'])[0])
        direction=plane.direction(layout['node'])
        for control in layout['controls']:
            key=(layout['node'],control['kind'],control['axis'])
            color=HOT if self.manager.hover==key else GOLD
            if control['kind']=='direction':
                color=NEGATIVE if control['axis']==axis and direction else POSITIVE
                if self.manager.hover==key:color=color.lighter(125)
            painter.setPen(QtGui.QPen(color,2. if control['kind']!='rotate' else 1.4));painter.setBrush(QtCore.Qt.NoBrush)
            if control['kind']=='axis':
                fill=QtGui.QColor(color);fill.setAlpha(135 if control['axis']==axis else 65);painter.setBrush(fill)
                polygon=control['polygon'];painter.drawPolygon(QtGui.QPolygonF([point(p) for p in polygon]))
                center=QtCore.QPointF(sum(p[0] for p in polygon)/len(polygon),self.height()-1-sum(p[1] for p in polygon)/len(polygon))
                letter(center,control['axis'])
            elif control['kind']=='free':
                p=point(control['point']);painter.setBrush(color);painter.drawRect(QtCore.QRectF(p.x()-5,p.y()-5,10,10))
            elif control['kind']=='direction':
                p=point(control['point']);fill=QtGui.QColor(color);fill.setAlpha(210);painter.setBrush(fill)
                painter.drawEllipse(p,SIDE_RADIUS,SIDE_RADIUS)
                painter.drawLine(p+QtCore.QPointF(-3,0),p+QtCore.QPointF(3,0))
                if control['axis']!=axis or not direction:
                    painter.drawLine(p+QtCore.QPointF(0,-3),p+QtCore.QPointF(0,3))
            else:
                for a,b in control['segments']:painter.drawLine(point(a),point(b))
                if control['kind']=='move' and control['tip'][2] and control['segments']:
                    a,b=control['segments'][-1];dx,dy=b[0]-a[0],b[1]-a[1];length=math.hypot(dx,dy)
                    if length>1:
                        dx,dy=dx/length,dy/length
                        vertices=[b,(b[0]-dx*13.75-dy*5,b[1]-dy*13.75+dx*5),(b[0]-dx*13.75+dy*5,b[1]-dy*13.75-dx*5)]
                        painter.setBrush(color);painter.drawPolygon(QtGui.QPolygonF([point(p) for p in vertices]))
                        p=point(b);letter(p+QtCore.QPointF(10,-10),control['axis'])


class AngleInput(QtWidgets.QFrame):
    """Focusable numeric input beside the input-transparent viewport overlay."""
    def __init__(self,manager,axis):
        super().__init__(manager.canvas.viewport.window(),QtCore.Qt.Tool|QtCore.Qt.FramelessWindowHint)
        self.setObjectName('modelPackMirrorAngleInput')
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating)
        self.setFont(manager.owner.font())
        self.setStyleSheet('QFrame#modelPackMirrorAngleInput {background:#303030;border:1px solid #f5ae30;border-radius:4px;} '
                          'QLabel {color:#f5ae30;border:0;} QLineEdit {background:#202020;color:white;padding:4px;border:1px solid #777;}')
        row=QtWidgets.QHBoxLayout(self);row.setContentsMargins(8,6,8,6)
        self.label=QtWidgets.QLabel('Δ '+'XYZ'[axis]+' (°)',self);row.addWidget(self.label)
        self.input=QtWidgets.QLineEdit('0',self);self.input.setFixedWidth(88)
        self.input.setFont(self.font())
        self.input.setToolTip('Угол относительно положения при захвате оси. Enter — применить, Esc — закрыть.')
        self.input.textChanged.connect(lambda *_:self.input.setStyleSheet(''))
        self.input.returnPressed.connect(manager.apply_angle);row.addWidget(self.input)
        self.adjustSize()


class Viewport(overlay.Overlay):
    canvas_class=Canvas
    def __init__(self,owner,nodes):
        self.owner=owner;self.nodes=list(nodes);self.layouts=[];self.hover=None;self.dragging=None;self.closed=False
        self.context=cmds.currentCtx()
        self.callbacks=[]
        self.angle_editor=None;self.angle_session=None
        self.vertex_snap_held=False;self.grid_snap_held=False;self.snap_target=None
        self.gizmo_size=global_scale()
        super().__init__()
        app=QtWidgets.QApplication.instance()
        if app is not None:app.installEventFilter(self)
        for event in ('Undo','Redo'):
            self.callbacks.append(om.MEventMessage.addEventCallback(event,self.history_changed))
        self.callbacks.append(om.MEventMessage.addEventCallback('ToolChanged',self.tool_changed))
        for event in (om.MSceneMessage.kBeforeNew,om.MSceneMessage.kBeforeOpen,om.MSceneMessage.kMayaExiting):
            self.callbacks.append(om.MSceneMessage.addCallback(event,self.scene_changed))
        self.tick()

    def layout(self,projection):
        result=[]
        for node in self.nodes:
            if not cmds.objExists(node) or not cmds.objExists(node+'.'+plane.POSE):continue
            try:
                layout=project_geometry(node,plane.frame(node),projection,self.gizmo_size)
                result.append(layout)
            except (RuntimeError,ValueError):continue
        return result

    def tick(self):
        if self.closed:return
        super().tick()
        size=global_scale();changed=size!=self.gizmo_size;self.gizmo_size=size
        if self.canvas is not None and isValid(self.canvas):
            self.canvas.sync_occlusion()
            if changed:self.canvas.update()

    def scene_changed(self,*_):
        self.owner._close_edit_panel();self.owner._active_tool=None;self.owner._sync_slide_button()

    def tool_changed(self,*_):
        if self.closed:return
        context=cmds.currentCtx()
        if context==self.context:return
        self.context=context;self.release();self.hide_angle();self.hover=None;self.tick()

    def native_mesh_tool(self):
        return any(word in cmds.currentCtx().lower() for word in ('move','rotate','scale'))

    def coordinates(self,event):
        if self.canvas is None or not isValid(self.canvas):return None
        point=self.canvas.viewport.mapFromGlobal(event.globalPosition().toPoint())
        return float(point.x()),float(self.canvas.height()-1-point.y())

    def ray(self,point):
        origin,direction=om.MPoint(),om.MVector()
        try:rx=self.view.portWidth()/self.canvas.width();ry=self.view.portHeight()/self.canvas.height()
        except AttributeError:rx=ry=1.
        self.view.viewToWorld(int(point[0]*rx),int(point[1]*ry),origin,direction)
        return om.MVector(origin),direction.normal()

    def radial(self,point,center,axis):
        origin,ray=self.ray(point);denominator=axis*ray
        if abs(denominator)<.03:return None
        radial=origin+ray*((axis*(center-origin))/denominator)-center
        return radial.normal() if radial.length()>1e-10 else None

    def history_changed(self,*_):
        self.hide_angle();self.tick()

    def hide_angle(self):
        editor=self.angle_editor;self.angle_editor=None;self.angle_session=None
        if editor is not None and isValid(editor):editor.hide();editor.deleteLater()

    def angle_input_target(self,target,event):
        editor=self.angle_editor
        if editor is None or not isValid(editor):return False
        if target==editor or target==editor.windowHandle():return True
        if isinstance(target,QtWidgets.QWidget) and editor.isAncestorOf(target):return True
        # Native QWindow routing and owner propagation happen before the
        # QLineEdit receives a floating-window click. Use screen coordinates
        # as well as QObject ancestry to keep these clicks inside the editor.
        if hasattr(event,'globalPosition'):
            return editor.frameGeometry().contains(event.globalPosition().toPoint())
        return False

    def cancel_if_inactive(self):
        # Activation of an owned input window can briefly precede the
        # application's activation event on Windows. Check after that handoff.
        app=QtWidgets.QApplication.instance()
        if not self.closed and app is not None and app.applicationState()!=QtCore.Qt.ApplicationActive:
            self.release(cancel=True)

    def show_angle(self,drag,point):
        if self.angle_session is drag:return
        self.hide_angle();self.angle_session=drag;self.angle_editor=AngleInput(self,drag['axis_index'])
        parent=self.canvas.viewport;local=QtCore.QPoint(round(point[0])+18,round(parent.height()-1-point[1])+18)
        local.setX(max(0,min(local.x(),parent.width()-self.angle_editor.width())))
        local.setY(max(0,min(local.y(),parent.height()-self.angle_editor.height())))
        self.angle_editor.move(parent.mapToGlobal(local));self.angle_editor.show()
        QtWidgets.QToolTip.hideText()

    def apply_angle(self):
        context=self.angle_session;editor=self.angle_editor
        if context is None or editor is None:return
        try:
            degrees=float(editor.input.text().strip().replace(',','.'))
            if not math.isfinite(degrees):raise ValueError()
        except ValueError:
            editor.input.setStyleSheet('border:1px solid #eb544b;');editor.input.setToolTip('Введите угол числом, например 17,5.')
            return
        self.release()
        if cmds.objExists(context['node']):
            result=plane.rotated(context['pose'],context['axis'],math.radians(degrees))
            if not plane.frame(context['node']).isEquivalent(result,1e-10):
                cmds.undoInfo(openChunk=True,chunkName='Mirror plane angle')
                try:plane.write(context['node'],result)
                finally:cmds.undoInfo(closeChunk=True)
        self.hide_angle();self.tick()
        if self.canvas is not None:self.canvas.viewport.setFocus(QtCore.Qt.OtherFocusReason)
        if not cmds.about(batch=True):cmds.refresh(currentView=True)

    def snap_mode(self):
        if self.vertex_snap_held:return 'point'
        if self.grid_snap_held:return 'grid'
        if snapping.point_mode():return 'point'
        if snapping.grid_mode():return 'grid'
        return None

    def snap_position(self,drag,point,proposed):
        self.snap_target=None
        mode=self.snap_mode()
        if mode=='grid':
            origin,ray=self.ray(point)
            axis=drag['axis'] if drag['kind']=='move' else None
            self.snap_target=snapping.grid_position(proposed,origin,ray,self.up_axis,axis,drag['center'])
            return self.snap_target
        if mode!='point':return None
        if 'snap_cache' not in drag:
            # V may be pressed after a live preview has already moved the
            # reflected vertices. Read the geometry at the gesture's start,
            # so the plane cannot chase its own changing reflection.
            with without_undo():
                if drag['changed']:plane.restore(drag['node'],drag['before'])
                try:drag['snap_cache']=snapping.PointCache(drag['projection'],self.panel)
                finally:
                    if drag['changed']:plane.write(drag['node'],drag['final'])
        limit=snapping.tolerance()
        if limit is not None:
            try:limit/=self.view.portWidth()/self.canvas.width()
            except AttributeError:pass
        self.snap_target=drag['snap_cache'].nearest(point,limit)
        return self.snap_target

    def press(self,point,shift=False):
        if self.view is None:return False
        projection=overlay.Projection(self.view,self.canvas.width(),self.canvas.height())
        self.layouts=self.layout(projection);picked=pick(self.layouts,point)
        if picked is None:return False
        layout,control=picked
        self.hide_angle()
        if control['kind'] in ('axis','direction'):
            if control['kind']=='direction':
                selected=cmds.getAttr(layout['node']+'.'+plane.fields(layout['node'])[0])
                direction=1-plane.direction(layout['node']) if selected==control['axis'] else 0
                cmds.optionVar(intValue=('ModelPack_MirrorDirection',direction))
            cmds.optionVar(intValue=('ModelPack_MirrorAxis',control['axis']))
            self.owner._modifier_settings_changed(self.owner._edit_kind)
            self.owner._restore_modifier_controls();self.tick();return True
        pose=layout['pose'];center=layout['center'];index=control['axis']
        axis=layout['axes'][index] if index is not None else om.MVector()
        _,ray=self.ray(point);right=om.MVector(0,1,0)^ray
        if right.length()<1e-8:right=om.MVector(1,0,0)
        right.normalize();up=ray^right
        self.dragging=dict(node=layout['node'],kind=control['kind'],axis=axis,pose=pose,
                           before=plane.snapshot(layout['node']),anchor=point,scale=layout['scale'],projection=projection,
                           center=center,right=right,up=up,ray=ray,changed=False,axis_index=index,last_point=point)
        self.snap_target=None
        if control['kind'] in ('move','free') and self.snap_mode()=='point':
            self.dragging['snap_cache']=snapping.PointCache(projection,self.panel)
        if control['kind']=='rotate':
            self.dragging.update(radial=self.radial(point,center,axis),last_raw=0.,angle_raw=0.,angle=0.)
            if shift:self.show_angle(self.dragging,point)
        return True

    def move(self,point,shift=False):
        drag=self.dragging
        if drag is None:return
        drag['last_point']=point
        dx,dy=point[0]-drag['anchor'][0],point[1]-drag['anchor'][1]
        pose=drag['pose'];projection=drag['projection'];center=drag['center'];scale=drag['scale']
        start=projection.point(center)
        if self.canvas is not None:self.canvas.update()
        if drag['kind']=='move':
            end=projection.point(center+drag['axis']*scale);gx,gy=end[0]-start[0],end[1]-start[1];squared=gx*gx+gy*gy
            amount=(dx*gx+dy*gy)/squared*scale if squared>4 else dy*scale/GIZMO_PIXELS
            result=plane.moved(pose,drag['axis']*amount)
        elif drag['kind']=='free':
            delta=om.MVector()
            for direction in (drag['right'],drag['up']):
                end=projection.point(center+direction*scale);gx,gy=end[0]-start[0],end[1]-start[1];squared=gx*gx+gy*gy
                if squared>4:delta+=direction*((dx*gx+dy*gy)/squared*scale)
            result=plane.moved(pose,delta)
        else:
            a=drag.get('radial');b=self.radial(point,center,drag['axis'])
            if a is not None and b is not None:
                raw=math.atan2(drag['axis']*(a^b),a*b)
                drag['angle_raw']+=math.remainder(raw-drag['last_raw'],math.tau);drag['last_raw']=raw
                angle=drag['angle_raw']
            else:angle=(dx+dy)*math.pi/180
            degrees=math.degrees(angle)
            if shift:
                degrees=math.copysign(math.floor(abs(degrees)/5.+.5)*5.,degrees)
                self.show_angle(drag,point)
            drag['angle']=degrees
            if self.angle_session is drag and not self.angle_editor.input.hasFocus():
                self.angle_editor.input.setText(f'{degrees:.8g}')
            angle=math.radians(degrees)
            result=plane.rotated(pose,drag['axis'],angle)
        if drag['kind'] in ('move','free'):
            target=self.snap_position(drag,point,plane.origin(result))
            if target is not None:
                delta=drag['axis']*((target-center)*drag['axis']) if drag['kind']=='move' else target-center
                result=plane.moved(pose,delta)
        if result.isEquivalent(drag.get('final',pose),1e-10):return
        reset=result.isEquivalent(pose,1e-10)
        with without_undo():
            if reset:plane.restore(drag['node'],drag['before'])
            else:plane.write(drag['node'],result)
        drag['final']=result;drag['changed']=not reset;self.tick()
        if not cmds.about(batch=True):cmds.refresh(currentView=True)

    def release(self,cancel=False):
        drag=self.dragging;self.dragging=None
        self.snap_target=None
        if self.canvas is not None:self.canvas.update()
        if cancel:self.hide_angle()
        if drag is None or not drag['changed'] or not cmds.objExists(drag['node']):return
        with without_undo():plane.restore(drag['node'],drag['before'])
        if not cancel:
            cmds.undoInfo(openChunk=True,chunkName='Mirror plane')
            try:plane.write(drag['node'],drag['final'])
            finally:cmds.undoInfo(closeChunk=True)
        self.tick()
        if not cmds.about(batch=True):cmds.refresh(currentView=True)

    def eventFilter(self,target,event):
        if self.closed:return False
        kind=event.type()
        if kind==QtCore.QEvent.ApplicationDeactivate:
            self.vertex_snap_held=False;self.grid_snap_held=False
            QtCore.QTimer.singleShot(0,self.cancel_if_inactive)
        editor=self.angle_editor
        in_editor=self.angle_input_target(target,event)
        if in_editor and kind in (QtCore.QEvent.KeyPress,QtCore.QEvent.ShortcutOverride):
            if event.matches(QtGui.QKeySequence.Undo) or event.matches(QtGui.QKeySequence.Redo):
                if kind==QtCore.QEvent.KeyPress:
                    editor.input.undo() if event.matches(QtGui.QKeySequence.Undo) else editor.input.redo()
                event.accept();return True
        if kind==QtCore.QEvent.KeyPress and self.angle_session is not None:
            if event.key()==QtCore.Qt.Key_Escape:
                self.release(cancel=True);self.hide_angle();return True
            if not in_editor and event.key() in (QtCore.Qt.Key_Return,QtCore.Qt.Key_Enter):
                self.apply_angle();return True
        if kind in (QtCore.QEvent.KeyPress,QtCore.QEvent.KeyRelease) and event.key()==QtCore.Qt.Key_Shift:
            if self.dragging is not None and self.dragging['kind']=='rotate':
                self.move(self.dragging['last_point'],shift=kind==QtCore.QEvent.KeyPress)
        if kind in (QtCore.QEvent.KeyPress,QtCore.QEvent.KeyRelease,QtCore.QEvent.ShortcutOverride) and event.key() in (QtCore.Qt.Key_V,QtCore.Qt.Key_X):
            attribute='vertex_snap_held' if event.key()==QtCore.Qt.Key_V else 'grid_snap_held'
            held=getattr(self,attribute)
            # A V press that happened before this overlay existed belongs to
            # Maya. Let Maya receive that release and restore its own snap mode.
            if kind==QtCore.QEvent.KeyRelease and not held:return False
            # Keep native mesh snapping usable while its manipulator is active.
            # The plane reads Maya's snap mode when its own drag starts.
            if not held and self.dragging is None and self.native_mesh_tool():return False
            parent=self.canvas.viewport if self.canvas is not None else None
            in_view=parent is not None and (target==parent or isinstance(target,QtWidgets.QWidget) and parent.isAncestorOf(target))
            editing=isinstance(target,(QtWidgets.QLineEdit,QtWidgets.QTextEdit,QtWidgets.QPlainTextEdit,QtWidgets.QAbstractSpinBox))
            release_held=kind==QtCore.QEvent.KeyRelease and held
            allowed=release_held or not editing and not event.modifiers()&(QtCore.Qt.ControlModifier|QtCore.Qt.AltModifier|QtCore.Qt.MetaModifier)
            if allowed and (in_view or held):
                if kind!=QtCore.QEvent.ShortcutOverride and not event.isAutoRepeat():
                    setattr(self,attribute,kind==QtCore.QEvent.KeyPress)
                    if self.dragging is not None and self.dragging['kind'] in ('move','free'):
                        self.move(self.dragging['last_point'])
                event.accept();return True
        if kind==QtCore.QEvent.MouseButtonPress and editor is not None:
            if in_editor:
                self.release();return False
            if self.dragging is None:self.hide_angle()
        if kind in (QtCore.QEvent.Move,QtCore.QEvent.Resize,QtCore.QEvent.Show,QtCore.QEvent.Hide,QtCore.QEvent.WindowStateChange):
            if self.canvas is not None and target!=self.canvas and isinstance(target,QtWidgets.QWidget) and target.isWindow():self.canvas.update()
        if kind==QtCore.QEvent.KeyPress and self.canvas is not None:
            parent=self.canvas.viewport
            in_view=target==parent or isinstance(target,QtWidgets.QWidget) and parent.isAncestorOf(target)
            editing=isinstance(target,(QtWidgets.QLineEdit,QtWidgets.QTextEdit,QtWidgets.QPlainTextEdit,QtWidgets.QAbstractSpinBox))
            if in_view and not editing and not event.modifiers()&(QtCore.Qt.ControlModifier|QtCore.Qt.AltModifier|QtCore.Qt.MetaModifier):
                key=event.key();shift=bool(event.modifiers()&QtCore.Qt.ShiftModifier)
                grow=key==QtCore.Qt.Key_Plus or key==QtCore.Qt.Key_Equal and shift
                shrink=key in (QtCore.Qt.Key_Minus,QtCore.Qt.Key_Underscore)
                if grow or shrink:
                    # Use Maya's global manipulator preference and its native
                    # doubling/halving increments, without a geometry Undo step.
                    with without_undo():cmds.manipOptions(relative=True,scale=2. if grow else .5)
                    cmds.optionVar(floatValue=('manipGlobalScale',global_scale()))
                    self.tick();return True
        if kind==QtCore.QEvent.KeyPress and self.dragging is not None:
            if event.key()==QtCore.Qt.Key_Escape:self.release(cancel=True);return True
            if event.matches(QtGui.QKeySequence.Undo) or event.matches(QtGui.QKeySequence.Redo):self.release(cancel=True)
        if kind not in (QtCore.QEvent.MouseButtonPress,QtCore.QEvent.MouseMove,QtCore.QEvent.MouseButtonRelease):return False
        if event.modifiers()&QtCore.Qt.AltModifier:
            if self.dragging is not None:self.release(cancel=True)
            self.hide_angle()
            return False
        point=self.coordinates(event)
        if point is None:return False
        try:
            if self.dragging is not None:
                if kind==QtCore.QEvent.MouseMove:self.move(point,shift=bool(event.modifiers()&QtCore.Qt.ShiftModifier));return True
                if kind==QtCore.QEvent.MouseButtonRelease and event.button()==QtCore.Qt.LeftButton:self.release();return True
            if self.canvas is None or not self.canvas.isVisible():return False
            parent=self.canvas.viewport
            if target!=parent and (not isinstance(target,QtWidgets.QWidget) or not parent.isAncestorOf(target)):return False
            if kind==QtCore.QEvent.MouseButtonPress and event.button()==QtCore.Qt.LeftButton:return self.press(point,shift=bool(event.modifiers()&QtCore.Qt.ShiftModifier))
            if kind==QtCore.QEvent.MouseMove:
                picked=pick(self.layouts,point)
                hover=(picked[0]['node'],picked[1]['kind'],picked[1]['axis']) if picked else None
                if hover!=self.hover:self.hover=hover;self.canvas.update()
                if picked:
                    control=picked[1];axis='XYZ'[control['axis']] if control['axis'] is not None else ''
                    label={'axis':'Плоскость '+axis,'direction':'Сменить сторону отражения '+axis+' ↔ −'+axis,
                           'move':'Переместить по '+axis,'rotate':'Повернуть вокруг '+axis,'free':'Переместить плоскость'}[control['kind']]
                    QtWidgets.QToolTip.showText(event.globalPosition().toPoint(),label,parent)
                else:QtWidgets.QToolTip.hideText()
        except (ValueError,RuntimeError) as error:
            self.release(cancel=True);cmds.warning('Mirror: '+str(error))
        return False

    def close(self):
        if self.closed:return
        self.release();self.hide_angle();self.closed=True;self.timer.stop()
        self.vertex_snap_held=False;self.grid_snap_held=False
        app=QtWidgets.QApplication.instance()
        if app is not None:app.removeEventFilter(self)
        QtWidgets.QToolTip.hideText()
        for callback in self.callbacks:om.MMessage.removeCallback(callback)
        self.callbacks=[];self.clear_canvas();self.layouts=[];self.nodes=[];self.deleteLater()
