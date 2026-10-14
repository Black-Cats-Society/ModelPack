"""Viewport drag with linear weights and one Undo entry per gesture."""
import math
from PySide6 import QtCore,QtGui,QtWidgets
from maya import cmds
from maya.api import OpenMaya as om,OpenMayaUI as omui
from model_pack_bevel.session import without_undo
from . import core,components,overlay

CONTEXT="modelPackLinearShiftContext"
MODE_OPTION='ModelPack_PlaneSlideFrameMode'
_DRAG=None
_FILTER=None
_CALLBACKS=[]
_OVERLAY=None


def active():return cmds.currentCtx()==CONTEXT


def frame_mode():
    value=cmds.optionVar(q=MODE_OPTION) if cmds.optionVar(exists=MODE_OPTION) else 0
    return {0:'local',1:'global',2:'world'}.get(value,'local')


def set_frame_mode(mode):
    if mode not in ('local','global','world'):raise ValueError('Неизвестное положение плоскости.')
    if _DRAG is not None:_finish()
    cmds.optionVar(intValue=(MODE_OPTION,{'local':0,'global':1,'world':2}[mode]))
    if _OVERLAY is not None:
        _OVERLAY.mode=mode;_OVERLAY.up_axis=cmds.upAxis(q=True,axis=True)
        _sync_overlay();cmds.refresh(force=True)


def plane_axis():
    """The closest world axis to the original plane, before any drag tilt."""
    if _DRAG is not None and _DRAG.get('section') is not None:return _DRAG['section'].normal_axis()
    try:
        chains=core.selected_chains()
        if _OVERLAY is not None:
            _OVERLAY.sync(chains)
            sections=_OVERLAY.sections
        else:
            mode=frame_mode();up=cmds.upAxis(q=True,axis=True);sections=[]
            for chain in chains:
                saved=next((s for s in reversed(overlay._RETAINED) if s.matches(chain) and s.mode==mode),None)
                if saved is None:
                    source=next((s for s in reversed(overlay._RETAINED) if s.matches(chain)),None) or overlay.Section(chain)
                    saved=source.in_mode(mode,up)
                sections.append(saved)
        return sections[0].normal_axis() if sections else None
    except (ValueError,RuntimeError):return None


def set_plane_axis(axis):
    if axis not in ('x','y','z'):raise ValueError('Неизвестная ось плоскости.')
    if _DRAG is not None:_finish()
    try:
        chains=core.selected_chains();mode=frame_mode();up=cmds.upAxis(q=True,axis=True)
        history=_OVERLAY.history if _OVERLAY is not None else overlay._RETAINED
        for chain in chains:
            # Changing the axis starts a new reference in the current mode.
            # Other coordinate modes keep their own frame on this selection.
            source=next((s for s in reversed(history) if s.matches(chain)),None) or overlay.Section(chain)
            section=source.with_axis(axis).oriented(mode,up)
            history[:]=[s for s in history if not (s.chain.topology==chain.topology and
                s.chain.points==chain.points and set(s.chain.vertices)==set(chain.vertices) and s.mode==mode)]
            history.append(section)
        history[:]=history[-64:]
        _sync_overlay();cmds.refresh(force=True)
    except (ValueError,RuntimeError) as error:cmds.warning('Plane Deform: '+str(error))


def _sync_overlay(*_):
    if _OVERLAY is None:return
    _OVERLAY.drag=_DRAG
    if _DRAG is None:
        try:_OVERLAY.sync(core.selected_chains())
        except (ValueError,RuntimeError):_OVERLAY.sync([])
    else:_OVERLAY.tick()


def _pick_plane(sections,view,anchor):
    handles=[];interiors=[];projection=overlay.Projection(view)
    for index,section in enumerate(sections):
        corners=projection.polygon(section.corners)
        # An edge-on plane cannot provide an unambiguous screen-space grip.
        area=abs(sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(corners,corners[1:]+corners[:1])))*.5
        if area<9 and section.axis_choice is None:continue
        for point in section.controls:
            x,y,visible=projection.point(point)
            distance=math.hypot(anchor[0]-x,anchor[1]-y)
            if visible and distance<=10:handles.append((distance,index,point))
        if area<9:continue
        point=section.hit(view,anchor)
        if point is not None:interiors.append((index,point))
    if handles:
        _,index,point=min(handles,key=lambda p:p[:2])
    elif interiors:
        # If several helper rectangles overlap, pick the frontmost plane.
        origin,direction=om.MPoint(),om.MVector();view.viewToWorld(int(anchor[0]),int(anchor[1]),origin,direction)
        index,point=min(interiors,key=lambda p:(p[1]-om.MVector(origin)).length())
    else:return None
    chain=sections[index].chain;edge,fraction=overlay.nearest_edge(chain,point)
    return chain,edge,fraction,om.MPoint(point)


def _pick(chains,view,anchor):
    candidates=[];projection=overlay.Projection(view)
    for chain_index,chain in enumerate(chains):
        if getattr(chain,'kind','edge')!='edge':
            for vertex,position in chain.points.items():
                x,y,visible=projection.point(position)
                if visible:candidates.append((math.hypot(anchor[0]-x,anchor[1]-y),chain_index,vertex,0.,om.MPoint(position)))
            for a,b in chain.segments:
                first,second=projection.point(chain.points[a]),projection.point(chain.points[b])
                if projection.line(chain.points[a],chain.points[b]) is None:continue
                dx,dy=second[0]-first[0],second[1]-first[1];squared=dx*dx+dy*dy
                t=max(0.,min(1.,((anchor[0]-first[0])*dx+(anchor[1]-first[1])*dy)/squared)) if squared>1e-9 else 0.
                distance=math.hypot(anchor[0]-first[0]-t*dx,anchor[1]-first[1]-t*dy)
                point=om.MPoint(chain.points[a])+(om.MPoint(chain.points[b])-om.MPoint(chain.points[a]))*t
                candidates.append((distance,chain_index,a,0.,point))
            continue
        fn=core._mesh_fn(chain.mesh)
        for edge in chain.edges:
            a,b=fn.getEdgeVertices(edge)
            first,second=(projection.point(chain.points[v]) for v in (a,b))
            if projection.line(chain.points[a],chain.points[b]) is None:continue
            dx,dy=second[0]-first[0],second[1]-first[1];squared=dx*dx+dy*dy
            fraction=max(0.,min(1.,((anchor[0]-first[0])*dx+(anchor[1]-first[1])*dy)/squared)) if squared>1e-9 else 0.
            distance=math.hypot(anchor[0]-first[0]-fraction*dx,anchor[1]-first[1]-fraction*dy)
            point=om.MPoint(chain.points[a])+(om.MPoint(chain.points[b])-om.MPoint(chain.points[a]))*fraction
            candidates.append((distance,chain_index,edge,fraction,point))
    if not candidates:raise ValueError("Выбранные компоненты не видны в активном окне.")
    distance,index,edge,fraction,point=min(candidates,key=lambda item:item[:4])
    if distance>16:raise ValueError("Начните движение возле выделенного компонента или на плоскости.")
    # Chain order follows its vertices, while API edge endpoints may reverse.
    chain=chains[index]
    if getattr(chain,'kind','edge')!='edge':return chain,edge,fraction,point
    edge_index=chain.edges.index(edge)
    a=core._mesh_fn(chain.mesh).getEdgeVertices(edge)[0]
    if a!=chain.vertices[edge_index]:fraction=1.-fraction
    return chain,edge,fraction,point


def _press():
    global _DRAG
    if cmds.draggerContext(CONTEXT,q=True,button=True)!=1:return
    try:
        if _DRAG is not None:_finish()
        chains=core.selected_chains();anchor=cmds.draggerContext(CONTEXT,q=True,anchorPoint=True)
        view=omui.M3dView.active3dView();sections=[]
        if _OVERLAY is not None:
            _OVERLAY.sync(chains);sections=_OVERLAY.sections
        else:
            for chain in chains:
                try:
                    section=overlay.Section(chain)
                    section=section.in_mode(frame_mode(),cmds.upAxis(q=True,axis=True))
                    sections.append(section)
                except (ValueError,RuntimeError):continue
        plane_pick=_pick_plane(sections,view,anchor)
        # Keep direct edge dragging, including edge-on views. Interior plane
        # picks use the actual ray-plane point for the tilt influence.
        if plane_pick is not None:chain,edge,fraction,point=plane_pick
        else:chain,edge,fraction,point=_pick(chains,view,anchor)
        section=next((s for s in sections if s.chain is chain),None)
        if getattr(chain,'kind','edge')!='edge':
            if section is None:raise ValueError('Не удалось построить управляющую плоскость.')
            gesture=components.Gesture(chain,tuple(point)[:3],tuple(section.opposite(point)),plane=(section.center,section.normal),axis=section.slide_axis,directions=section.slide_directions,field_axis=section.slide_field_axis)
        else:
            gesture=core.Gesture(chain,edge,fraction,grip=tuple(point)[:3] if plane_pick is not None else None,
                pivot=tuple(section.opposite(point)) if section is not None and plane_pick is not None else None,
                plane=(section.center,section.normal) if section is not None and (section.can_tilt or plane_pick is not None) else None,
                axis=section.slide_axis if section is not None and section.can_tilt else None,
                helper_axis=section.slide_field_axis if section is not None else None,
                movement_axis=section.slide_axis if section is not None and section.axis_choice is not None else None)
        projection=overlay.Projection(view);start=projection.point(point)
        guides={}
        for side,vector in gesture.guides.items():
            end=projection.point(point+vector);dx,dy=end[0]-start[0],end[1]-start[1]
            if dx*dx+dy*dy>=1.:guides[side]=(dx,dy,vector.length())
        if not guides:raise ValueError("Направляющие направлены в глубину экрана. Поверните вид.")
        _DRAG=dict(gesture=gesture,guides=guides,anchor=tuple(anchor),grip=tuple(point)[:3],section=section)
        _sync_overlay()
    except (ValueError,RuntimeError) as error:
        _DRAG=None;cmds.warning("Plane Deform: "+str(error))


def _drag():
    if _DRAG is None:return
    try:
        screen=cmds.draggerContext(CONTEXT,q=True,dragPoint=True)
        dx,dy=screen[0]-_DRAG['anchor'][0],screen[1]-_DRAG['anchor'][1]
        choices=[]
        for side,(gx,gy,length) in _DRAG['guides'].items():
            squared=gx*gx+gy*gy;along=dx*gx+dy*gy
            choices.append((max(0.,along/math.sqrt(squared)),side,max(0.,along/squared)*length))
        _,side,amount=max(choices)
        _DRAG['side']=side
        _DRAG['gesture'].slide(amount,side)
        gesture=_DRAG['gesture']
        if amount>gesture.chain.length*1e-6 and not _DRAG.get('stop_reported'):
            if max((om.MVector(gesture.current[v])-om.MVector(p)).length() for v,p in gesture.original.items())<gesture.chain.length*1e-10:
                cmds.warning('Plane Deform: Движение ограничено закреплёнными вершинами или соседним невыделенным рядом. Потяните в другую сторону или расширьте выделение.')
                _DRAG['stop_reported']=True
        _sync_overlay()
        cmds.refresh(force=True)
    except (ValueError,RuntimeError) as error:
        cancel_gesture();cmds.warning("Plane Deform: "+str(error))


def _finish():
    global _DRAG
    drag,_DRAG=_DRAG,None
    if drag is not None:
        try:
            changed=drag['gesture'].commit()
            if changed and _OVERLAY is not None:_OVERLAY.preserve(drag,core.selected_chains())
        except (ValueError,RuntimeError) as error:cmds.warning("Plane Deform: "+str(error))
    _sync_overlay()


def cancel_gesture(*,after_history=False):
    global _DRAG
    drag,_DRAG=_DRAG,None
    if drag is not None:
        try:drag['gesture'].cancel(after_history=after_history)
        except (ValueError,RuntimeError) as error:cmds.warning("Plane Deform: "+str(error))
    _sync_overlay()


class Keys(QtCore.QObject):
    def eventFilter(self,target,event):
        if not active() or event.type() not in (QtCore.QEvent.KeyPress,QtCore.QEvent.ShortcutOverride):return False
        if isinstance(QtWidgets.QApplication.focusWidget(),(QtWidgets.QLineEdit,QtWidgets.QAbstractSpinBox,QtWidgets.QTextEdit,QtWidgets.QPlainTextEdit)):
            return False
        key=event.key()
        handled=key in (QtCore.Qt.Key_Return,QtCore.Qt.Key_Enter,QtCore.Qt.Key_Escape) or event.matches(QtGui.QKeySequence.Undo) or event.matches(QtGui.QKeySequence.Redo)
        if not handled:return False
        if event.type()==QtCore.QEvent.ShortcutOverride:event.accept();return True
        if key in (QtCore.Qt.Key_Return,QtCore.Qt.Key_Enter):shutdown();return True
        if key==QtCore.Qt.Key_Escape:
            if _DRAG is not None:cancel_gesture()
            else:shutdown()
            return True
        if event.matches(QtGui.QKeySequence.Undo):
            if _DRAG is not None:cancel_gesture()
            else:cmds.undo()
            return True
        if event.matches(QtGui.QKeySequence.Redo):cancel_gesture();cmds.redo();return True
        return False


def _finalize(*_):
    global _FILTER,_OVERLAY
    _finish()
    app=QtWidgets.QApplication.instance()
    if _FILTER is not None and app is not None:app.removeEventFilter(_FILTER)
    _FILTER=None
    if _OVERLAY is not None:
        _OVERLAY.close();cmds.refresh(force=True)
    _OVERLAY=None
    for callback in _CALLBACKS:om.MMessage.removeCallback(callback)
    _CALLBACKS.clear()


def activate():
    global _FILTER,_OVERLAY
    if active():shutdown();return False
    try:
        chains=core.selected_chains();selection=om.MGlobal.getActiveSelectionList()
        with without_undo():
            cmds.selectMode(component=True);cmds.selectType(allComponents=False)
            cmds.selectType(**{dict(edge='polymeshEdge',vertex='polymeshVertex',face='polymeshFace')[getattr(chains[0],'kind','edge')]:True})
            parents=[]
            for mesh in sorted({chain.mesh for chain in chains}):
                path=core._mesh_fn(mesh).dagPath();path.pop();parents.append(path.fullPathName())
            cmds.hilite(parents,replace=True)
            om.MGlobal.setActiveSelectionList(selection)
        callbacks=dict(pressCommand=_press,dragCommand=_drag,releaseCommand=_finish,finalize=_finalize,
                       space="screen",undoMode="none",cursor="crossHair")
        if cmds.draggerContext(CONTEXT,exists=True):cmds.draggerContext(CONTEXT,edit=True,**callbacks)
        else:cmds.draggerContext(CONTEXT,**callbacks)
        cmds.setToolTo(CONTEXT)
        cmds.refresh(force=True)
        app=QtWidgets.QApplication.instance()
        if app is not None:
            _FILTER=Keys();app.installEventFilter(_FILTER)
            _OVERLAY=overlay.Overlay(mode=frame_mode());_OVERLAY.sync(chains)
        for event in ('Undo','Redo'):
            _CALLBACKS.append(om.MEventMessage.addEventCallback(event,lambda *_:cancel_gesture(after_history=True)))
        _CALLBACKS.append(om.MEventMessage.addEventCallback('SelectionChanged',_sync_overlay))
        cmds.inViewMessage(amg="Plane Deform · тяните плоскость или точки · Enter / Esc",pos="midCenterTop",fade=True)
        return True
    except (ValueError,RuntimeError) as error:
        _finalize();cmds.warning("Plane Deform: "+str(error));return False


def shutdown():
    if active():cmds.setToolTo("selectSuperContext")
    _finalize()
