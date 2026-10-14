"""Maya drag context, persistent editing and compact contextual controls."""
from __future__ import annotations
import math
import time

import maya.cmds as cmds
from maya.api import OpenMaya as om, OpenMayaUI as omui
from PySide6 import QtCore, QtGui, QtWidgets

from slide_for_maya import ui as slide_ui
from .interaction import Gesture, gesture_mode
from .profiles import Curve
from .session import BevelSession, without_undo

CONTEXT = "modelPackGestureBevelContext"
_SESSION = None
_DRAG = None
_FILTER = None
_TIMER = None
_PENDING = None
_TOOLBOX = None
_PANEL = None
_VALUES = None
_SHIFT_CLICK = None
_DEFAULTS = dict(falloff_kind="round", profile_kind="round", shape=0.0,segments=1,
                 all_sections=False,keep_equal=False,
                 falloff_curve=Curve(), profile_curve=Curve(((0,0,None),(.5,.5,None),(1,0,None)),False))


def active():
    return cmds.currentCtx() == CONTEXT


def _warn(exc):
    cmds.warning("Bevel: " + str(exc))


def _screen_graph(session):
    view = omui.M3dView.active3dView()
    return {v: view.worldToView(om.MPoint(*session.graph.points[v]))
            for v in session.graph.neighbors}


def _pick_graph(session, anchor):
    projected = _screen_graph(session)
    candidates = []
    for edge,(a,b) in session.graph.edges.items():
        first, second = projected[a], projected[b]
        if not first[2] and not second[2]:
            continue
        dx,dy = second[0]-first[0],second[1]-first[1]
        squared = dx*dx+dy*dy
        t = max(0,min(1,((anchor[0]-first[0])*dx+(anchor[1]-first[1])*dy)/squared)) if squared > 1 else 0
        distance = math.hypot(anchor[0]-first[0]-t*dx,anchor[1]-first[1]-t*dy)
        candidates.append((distance,edge,t))
    if not candidates:
        raise ValueError("Выбранные рёбра не видны в активном окне.")
    distance,edge,t = min(candidates)
    # A click must hit the visible Bevel, rather than an unrelated background
    # edge projected nearby. Query the preview only at gesture start.
    fn = slide_ui._mesh_fn(session.preview)
    view = omui.M3dView.active3dView()
    origin,direction = om.MPoint(),om.MVector()
    view.viewToWorld(int(anchor[0]),int(anchor[1]),origin,direction)
    hit = fn.closestIntersection(om.MFloatPoint(origin),om.MFloatVector(direction),
                                 om.MSpace.kWorld,1e10,False)
    if hit is None:
        raise ValueError("Начните локальный жест на поверхности фаски.")
    point = tuple(hit[0])[:3]
    choices = []
    for member,(a,b) in session.graph.edges.items():
        first,second = session.graph.points[a],session.graph.points[b]
        direction = tuple(y-x for x,y in zip(first,second))
        squared = sum(v*v for v in direction)
        fraction = max(0,min(1,sum((p-x)*d for p,x,d in zip(point,first,direction))/squared))
        closest = tuple(x+d*fraction for x,d in zip(first,direction))
        choices.append((math.dist(point,closest),member,fraction))
    distance,edge,t = min(choices)
    if distance > session.width * max((1,*session.weights.values())) * 2:
        raise ValueError("Начните локальный жест возле полосы фаски.")
    return edge,t


def _scale(session):
    # Scene-space size stays constant when the camera zooms or rotates.
    return session.motion_scale


def _modifiers():
    modifiers = QtWidgets.QApplication.keyboardModifiers()
    return bool(modifiers & QtCore.Qt.ShiftModifier),bool(modifiers & QtCore.Qt.ControlModifier)


def _start_session():
    global _SESSION
    if _SESSION is None:
        history = _history_selection()
        if history:
            _SESSION = BevelSession(history[0],(),history_node=history[1])
            _status()
            return _SESSION
        _SESSION = BevelSession(segments=_DEFAULTS["segments"])
        for key,value in _DEFAULTS.items():
            setattr(_SESSION,key,value)
    return _SESSION


def _history_selection(allow_components=False):
    if not allow_components and any(".e[" in s for s in (cmds.ls(sl=True,fl=True) or [])):
        return None
    selected = cmds.ls(sl=True,objectsOnly=True,long=True) or []
    if not selected:
        return None
    item = selected[0]
    if cmds.nodeType(item)=="modelPackGestureBevel":
        meshes = cmds.listConnections(item+".outputMesh",s=False,d=True,shapes=True,type="mesh") or []
        if meshes:
            return cmds.listRelatives(meshes[0],parent=True,fullPath=True)[0],item
        return None
    try:
        fn = slide_ui._mesh_fn(item)
    except (RuntimeError,ValueError):
        return None
    nodes = [n for n in (cmds.listHistory(fn.dagPath().fullPathName()) or [])
             if cmds.nodeType(n)=="modelPackGestureBevel"]
    if nodes:
        return cmds.listRelatives(fn.dagPath().fullPathName(),parent=True,fullPath=True)[0],nodes[0]


def edit_saved():
    global _SESSION
    try:
        history = _history_selection(allow_components=True)
        if history is None:
            raise ValueError("Выберите меш с сохранённой фаской Bevel.")
        if not active():
            activate(_TOOLBOX)
        if _SESSION is None:
            _SESSION = BevelSession(history[0],(),history_node=history[1])
        _status()
    except Exception as exc:
        _warn(exc)


def _on_press():
    global _SESSION,_DRAG,_SHIFT_CLICK
    try:
        button = cmds.draggerContext(CONTEXT,q=True,button=True)
        if button == 3:
            show_menu(_TOOLBOX)
            return
        if button != 1:
            return
        anchor = tuple(cmds.draggerContext(CONTEXT,q=True,anchorPoint=True))[:2]
        shift,ctrl = _modifiers()
        if not shift or ctrl:
            _SHIFT_CLICK = None
        if _SESSION is None and shift and not ctrl:
            _DRAG = dict(selection=True,anchor=anchor,end=anchor)
            return
        if _SESSION is None:
            if ctrl:
                raise ValueError("Сначала создайте фаску движением без модификаторов.")
            _start_session()
        mode = gesture_mode(shift,ctrl)
        gesture = Gesture(mode,_SESSION.width,_SESSION.shape,_SESSION.segments,_scale(_SESSION))
        _DRAG = dict(anchor=anchor,gesture=gesture,weights=dict(_SESSION.weights))
        if shift and not ctrl:
            _DRAG["shift_click"] = True
            _DRAG["moved"] = False
        if mode == "local":
            edge,t = _pick_graph(_SESSION,anchor)
            _DRAG["influence"] = _SESSION.influence(edge,t)
        _SESSION.begin_action()
    except Exception as exc:
        _DRAG = None
        _warn(exc)


def _timer():
    global _TIMER
    if _TIMER is None:
        _TIMER = QtCore.QTimer()
        _TIMER.setSingleShot(True)
        _TIMER.timeout.connect(_flush)
    return _TIMER


def _on_drag():
    global _PENDING,_SHIFT_CLICK
    if _DRAG is None:
        return
    end = tuple(cmds.draggerContext(CONTEXT,q=True,dragPoint=True))[:2]
    if _DRAG.get("selection"):
        _DRAG["end"] = end
        return
    _PENDING = tuple(b-a for a,b in zip(_DRAG["anchor"],end))
    if _DRAG.get("shift_click"):
        _DRAG["moved"] |= math.hypot(*_PENDING)>=4
        if not _DRAG["moved"]:
            _PENDING = None
            return
        _SHIFT_CLICK = None
    if not _timer().isActive():
        _timer().start(16)


def _flush():
    global _PENDING
    if _SESSION is None or _DRAG is None or _PENDING is None:
        return
    delta,_PENDING = _PENDING,None
    gesture = _DRAG["gesture"]
    value = gesture.value(delta)
    try:
        if gesture.axis is None:
            return
        if gesture.mode == "local":
            _SESSION.local_width(value,_DRAG["influence"],_DRAG["weights"])
        else:
            _SESSION.update(**{gesture.mode:value})
        _status()
        cmds.refresh(currentView=True)
    except Exception as exc:
        _warn(exc)
        cancel()


def _on_release():
    global _DRAG,_PENDING
    if _DRAG is None:
        return
    _on_drag()
    _timer().stop()
    if _DRAG.get("selection"):
        try:
            slide_ui._select_from_screen(_DRAG["anchor"],_DRAG["end"],context=CONTEXT,button=1)
        except Exception as exc:
            _warn(exc)
    else:
        if _DRAG.get("shift_click") and not _DRAG["moved"]:
            _PENDING = None
            try:
                _toggle_source_edge(_DRAG["anchor"])
            except Exception as exc:
                _warn(exc)
        else:
            _flush()
        if _SESSION is not None:
            _SESSION.end_action()
    _DRAG = None


def _source_edge_at(anchor):
    session = _SESSION
    fn = slide_ui._mesh_fn(session.mesh)
    matrix = fn.dagPath().inclusiveMatrix()
    if session._base_input:
        selection = om.MSelectionList(); selection.add(session._base_input)
        fn = om.MFnMesh(selection.getPlug(0).asMObject())
        points = [p*matrix for p in fn.getPoints()]
    else:
        points = fn.getPoints(om.MSpace.kWorld)
    view = omui.M3dView.active3dView()
    projected = [view.worldToView(p) for p in points]
    choices = []
    for edge in range(fn.numEdges):
        a,b = fn.getEdgeVertices(edge)
        if projected[a][2] or projected[b][2]:
            choices.append((slide_ui._point_segment_distance(anchor,projected[a][:2],projected[b][:2]),edge))
    if not choices:
        return None
    distance,edge = min(choices)
    if distance<=20:
        return edge
    # A wide bevel can move its visible borders away from the original edge.
    # Map a nearby visible preview edge back to the closest source segment.
    visible = slide_ui._mesh_fn(session.preview)
    preview_points = visible.getPoints(om.MSpace.kWorld)
    screens = [view.worldToView(p) for p in preview_points]
    picks = [(slide_ui._point_segment_distance(anchor,screens[a][:2],screens[b][:2]),a,b)
             for e in range(visible.numEdges) for a,b in (visible.getEdgeVertices(e),)
             if screens[a][2] or screens[b][2]]
    if not picks or min(picks)[0]>20:
        return None
    _,a,b = min(picks)
    point = preview_points[a]+(preview_points[b]-preview_points[a])*.5
    def distance_to_edge(e):
        a,b = fn.getEdgeVertices(e)
        vector = points[b]-points[a]
        t = max(0,min(1,((point-points[a])*vector)/(vector*vector)))
        return (point-(points[a]+vector*t)).length()
    return min(range(fn.numEdges),key=distance_to_edge)


def _toggle_source_edge(anchor):
    global _SHIFT_CLICK
    edge = _source_edge_at(anchor)
    if edge is None:
        _SHIFT_CLICK = None
        return
    session = _SESSION
    selected = set(session.edges)
    now = time.monotonic()
    interval,distance = slide_ui._double_click_limits()
    previous = _SHIFT_CLICK
    members = {edge}
    remove = edge in selected
    if (previous and previous["edge"]==edge and previous["session"] is session and
            now-previous["time"]<=interval and math.dist(anchor,previous["anchor"])<=distance):
        # Both clicks are one loop-selection action, including its Undo.
        before = previous["before"]
        if session._undo and session._undo[-1]==before:
            session._undo.pop()
        session._action_before = before
        if session.edit_node:
            with without_undo():
                mesh = session._base_copy("bevelLoopSource#")
                try:
                    members = set(cmds.polySelect(mesh,edgeLoopOrBorder=edge,noSelection=True) or [edge])
                finally:
                    cmds.delete(mesh)
        else:
            members = set(cmds.polySelect(session.mesh,edgeLoopOrBorder=edge,noSelection=True) or [edge])
        remove = members.issubset(previous["selected"])
        _SHIFT_CLICK = None
    else:
        _SHIFT_CLICK = dict(edge=edge,session=session,time=now,anchor=anchor,selected=selected.copy(),
                            before=session._action_before or session.snapshot())
    selected = selected-members if remove else selected|members
    session.change_edges(selected)
    _status()
    cmds.refresh(currentView=True)


def show_values(toolbox=None):
    global _VALUES
    if not isinstance(QtWidgets.QApplication.instance(),QtWidgets.QApplication):
        return
    if _VALUES is None:
        from .values import ValuesPanel
        _VALUES = ValuesPanel(toolbox)
        panel = _VALUES
        _VALUES.destroyed.connect(lambda:_values_destroyed(panel))
        _VALUES.changed.connect(_numeric_value)
        if toolbox is not None:
            toolbox._bevel_values_size = QtCore.QSize(toolbox.size())
            row = toolbox.layout().rowCount()
            toolbox.layout().addWidget(_VALUES,row,0,1,2)
        else:
            _VALUES.setWindowTitle("Bevel — значения")
            _VALUES.setWindowFlag(QtCore.Qt.Window,True)
    _VALUES.show()
    _status()


def _values_destroyed(panel):
    global _VALUES
    if _VALUES is panel:
        _VALUES = None


def _hide_values():
    global _VALUES
    if _PANEL is not None:
        _PANEL.hide()
        _editor_hidden(_PANEL)
    if _VALUES is not None:
        host = _VALUES.parentWidget()
        _VALUES.hide()
        if host is not None and hasattr(host,"_bevel_values_size"):
            size = host._bevel_values_size
            host.layout().removeWidget(_VALUES)
            host.layout().activate()
            host.resize(size)
            del host._bevel_values_size
        _VALUES.deleteLater()
        _VALUES = None


def _numeric_value(name,value):
    try:
        session = _start_session()
        with session.action():
            if name=="transverse_cuts":
                session.change_transverse_cuts(int(value)-session.transverse_cuts)
            elif name in ("keep_equal","all_sections"):
                setattr(session,name,bool(value))
                session.update()
            else:
                session.update(**{name:int(value) if name=="segments" else value})
        _remember_settings()
        _status()
        cmds.refresh(currentView=True)
    except Exception as exc:
        _warn(exc)
        _status()


def _clear():
    global _DRAG,_PENDING,_SHIFT_CLICK
    _DRAG = _PENDING = None
    _SHIFT_CLICK = None
    if _TIMER is not None:
        _TIMER.stop()
    cmds.inViewMessage(clear="midCenterTop")


def commit():
    global _SESSION
    if _SESSION is None:
        return None
    _flush()
    if _SESSION is None:
        return None
    session,_SESSION = _SESSION,None
    _clear()
    try:
        node = session.commit()
        _DEFAULTS.update(shape=session.shape,segments=session.segments,profile_kind=session.profile_kind,
                         profile_curve=session.profile_curve,falloff_kind=session.falloff_kind,
                         falloff_curve=session.falloff_curve,keep_equal=session.keep_equal,
                         all_sections=session.all_sections)
        _status()
        return node
    except Exception as exc:
        _warn(exc)
        return None


def cancel(restore_selection=True):
    global _SESSION
    _clear()
    session,_SESSION = _SESSION,None
    if session is not None:
        session.cancel(restore_selection)
    _status()


def _status():
    if _VALUES is not None:
        state = _SESSION or type("State",(),dict(width=0,transverse_cuts=0,**_DEFAULTS))()
        _VALUES.sync(state)
    if _SESSION is not None:
        contact = " · предел: контакт" if _SESSION.contact_faces else ""
        cmds.inViewMessage(amg=(f"Bevel: {_SESSION.width:.3f} · {_SESSION.segments} сегм. "
                               f"· {_SESSION.transverse_cuts} попереч. · форма {_SESSION.shape:+.2f}{contact} · Enter / Esc"),
                           pos="midCenterTop",fade=False)


def _remember_settings():
    if _SESSION is not None:
        for key in _DEFAULTS:
            if hasattr(_SESSION,key):
                _DEFAULTS[key] = getattr(_SESSION,key)


def _sync_editor():
    if _PANEL is not None and _SESSION is not None:
        for which in ("falloff","profile"):
            widget = getattr(_PANEL,which)
            widget.curve = getattr(_SESSION,which+"_curve")
            widget.selected = widget.dragging = None
            widget.update()


def undo_action():
    if _SESSION is None:
        return False
    _flush()
    _clear()
    changed = _SESSION.undo()
    _remember_settings()
    _sync_editor()
    _status()
    cmds.refresh(currentView=True)
    return changed


def redo_action():
    if _SESSION is None:
        return False
    _clear()
    changed = _SESSION.redo()
    _remember_settings()
    _sync_editor()
    _status()
    cmds.refresh(currentView=True)
    return changed


def add_transverse_cut():
    _change_transverse_cut(1)


def remove_transverse_cut():
    if _SESSION is not None:
        _change_transverse_cut(-1)


def _change_transverse_cut(delta):
    try:
        _flush()
        if _SESSION is not None:
            _SESSION.end_action()
        _clear()
        _start_session().change_transverse_cuts(delta)
        _status()
        cmds.refresh(currentView=True)
    except Exception as exc:
        _warn(exc)
        cancel()


def _on_finalize():
    _flush()
    if _SESSION is None:
        _clear()
        _hide_values()
        return
    # Apply before leaving the tool. Native tools may already have selected
    # another object; preserve it without recalling obsolete component IDs of
    # the source mesh or the preview that commit is about to delete.
    selection = om.MGlobal.getActiveSelectionList()
    hilite = om.MGlobal.getHiliteList()
    mesh = _SESSION.mesh
    source_path = (cmds.ls(mesh,long=True) or [""])[0]
    preview_path = (cmds.ls(_SESSION.preview,long=True) or [""])[0]
    will_apply = _SESSION.width > 1e-9
    snapshots = []
    for current,is_hilite in ((selection,False),(hilite,True)):
        preserved = om.MSelectionList()
        preview_hilited = False
        for index in range(current.length()):
            try:
                dag,component = current.getComponent(index)
                path = dag.fullPathName()
                if path == preview_path or path.startswith(preview_path+"|"):
                    preview_hilited = is_hilite
                    continue
                if will_apply and not component.isNull() and (
                        path == source_path or path.startswith(source_path+"|")):
                    continue
            except RuntimeError:
                pass
            for item in current.getSelectionStrings(index):
                preserved.add(item)
        if is_hilite:
            if preview_hilited and cmds.objExists(mesh):
                preserved.add(mesh)
        snapshots.append(preserved)
    commit()
    om.MGlobal.setActiveSelectionList(snapshots[0])
    om.MGlobal.setHiliteList(snapshots[1])
    _hide_values()


class ViewportKeys(QtCore.QObject):
    def eventFilter(self,target,event):
        if not active() or event.type() not in (QtCore.QEvent.KeyPress,QtCore.QEvent.ShortcutOverride):
            return False
        key, modifiers = event.key(), event.modifiers()
        shift = bool(modifiers & QtCore.Qt.ShiftModifier)
        ctrl = bool(modifiers & QtCore.Qt.ControlModifier)
        command = None
        if key in (QtCore.Qt.Key_Return,QtCore.Qt.Key_Enter):
            command = commit
        elif key == QtCore.Qt.Key_Escape:
            command = cancel
        elif key in (QtCore.Qt.Key_Plus,QtCore.Qt.Key_Equal) and shift and not ctrl:
            command = add_transverse_cut
        elif key in (QtCore.Qt.Key_Minus,QtCore.Qt.Key_Underscore) and shift and not ctrl:
            command = remove_transverse_cut
        elif _SESSION is not None and key == QtCore.Qt.Key_Z:
            command = redo_action if shift else undo_action
        elif _SESSION is not None and ctrl and key == QtCore.Qt.Key_Y:
            command = redo_action
        if command is None or modifiers & (QtCore.Qt.AltModifier | QtCore.Qt.MetaModifier):
            return False
        focus = QtWidgets.QApplication.focusWidget()
        if focus is not None:
            if isinstance(focus,(QtWidgets.QLineEdit,QtWidgets.QAbstractSpinBox,
                                 QtWidgets.QTextEdit,QtWidgets.QPlainTextEdit)):
                return False
            if isinstance(focus,QtWidgets.QMenu):
                return False
            editor_undo = (_PANEL is not None and (_PANEL is focus or _PANEL.isAncestorOf(focus))
                           and command in (undo_action,redo_action))
            if _TOOLBOX is not None and (_TOOLBOX is focus or _TOOLBOX.isAncestorOf(focus)) and not editor_undo:
                return False
            if _PANEL is not None and (_PANEL is focus or _PANEL.isAncestorOf(focus)) and command not in (undo_action,redo_action):
                return False
        panel = cmds.getPanel(withFocus=True)
        editor_focus = focus is not None and _PANEL is not None and (_PANEL is focus or _PANEL.isAncestorOf(focus))
        if not editor_focus and panel not in (cmds.getPanel(type="modelPanel") or []):
            return False
        if event.type() == QtCore.QEvent.ShortcutOverride:
            event.accept()
            return True
        if event.isAutoRepeat():
            return True
        command()
        event.accept()
        return True


def activate(toolbox=None):
    global _FILTER,_TOOLBOX
    _TOOLBOX = toolbox
    if active():
        deactivate()
        return
    selection = om.MGlobal.getActiveSelectionList()
    prefix = "import model_pack_bevel.ui as bevel_ui; bevel_ui."
    callbacks = dict(pressCommand=prefix+"_on_press()",dragCommand=prefix+"_on_drag()",
                     releaseCommand=prefix+"_on_release()",finalize=prefix+"_on_finalize()",
                     undoMode="none",space="screen",projection="viewPlane")
    if cmds.draggerContext(CONTEXT,exists=True):
        cmds.draggerContext(CONTEXT,edit=True,**callbacks)
    else:
        cmds.draggerContext(CONTEXT,cursor="crossHair",**callbacks)
    cmds.setToolTo(CONTEXT)
    with without_undo():
        cmds.selectMode(component=True)
        cmds.selectType(allComponents=False)
        cmds.selectType(polymeshEdge=True)
        om.MGlobal.setActiveSelectionList(selection)
        meshes = cmds.ls(sl=True,objectsOnly=True,long=True) or []
        if meshes:
            cmds.hilite(meshes,replace=True)
    if _FILTER is None:
        _FILTER = ViewportKeys()
        QtWidgets.QApplication.instance().installEventFilter(_FILTER)
    cmds.inViewMessage(amg="Bevel · Shift: сегменты · Ctrl: форма · Shift+Ctrl: локально · Shift + плюс: поперечный разрез · Z: отмена",
                       pos="midCenterTop",fade=True)
    show_values(toolbox)
    if _SESSION is None and _history_selection():
        _start_session()


def deactivate():
    _on_finalize()
    if active():
        cmds.setToolTo("selectSuperContext")


def _setting(key,value):
    _settings(**{key:value})


def _settings(**values):
    if _SESSION is not None:
        with _SESSION.action():
            for key,value in values.items():
                setattr(_SESSION,key,value)
            if values.get("profile_kind") == "custom" and _SESSION.segments == 1:
                _SESSION.update(segments=3)
            else:
                _SESSION.update(shape=values.get("shape"))
        _remember_settings()
        _status()
        return
    _DEFAULTS.update({key:value for key,value in values.items() if key in _DEFAULTS})
    if (abs(values.get("shape",0))>1e-6 or values.get("profile_kind") == "custom"):
        _DEFAULTS["segments"] = max(3,_DEFAULTS["segments"])
    _status()


def _edit_curve(which,curve):
    _settings(**{which+"_kind":"custom",which+"_curve":curve})


def _begin_curve_action():
    if _SESSION is not None:
        _SESSION.begin_action()


def _end_curve_action():
    if _SESSION is not None:
        _SESSION.end_action()


def _editor_hidden(panel):
    _end_curve_action()
    try:
        host = panel.parentWidget()
        if host is not None and hasattr(host,"_bevel_editor_size"):
            original_size = host._bevel_editor_size
            del host._bevel_editor_size
            host.layout().removeWidget(panel)
            host.layout().invalidate()
            host.layout().activate()
            host.resize(original_size)
    except RuntimeError:
        pass


def _editor_destroyed(panel):
    global _PANEL
    if _PANEL is panel:
        _PANEL = None


def show_editor(which,toolbox=None):
    global _PANEL
    toolbox = toolbox or _TOOLBOX
    state = _SESSION or type("Settings",(),_DEFAULTS)()
    if _PANEL is not None:
        try:
            _PANEL.hide()
            _editor_hidden(_PANEL)
            _PANEL.deleteLater()
        except RuntimeError:
            pass
    _PANEL = __import__("model_pack_bevel.editor",fromlist=["ProfilePanel"]).ProfilePanel(
        state.falloff_curve,state.profile_curve,
        lambda curve:_edit_curve("falloff",curve),lambda curve:_edit_curve("profile",curve),toolbox)
    _PANEL.tabs.setCurrentIndex(0 if which == "falloff" else 1)
    panel = _PANEL
    panel.hidden.connect(lambda:_editor_hidden(panel))
    panel.destroyed.connect(lambda:_editor_destroyed(panel))
    for editor in (_PANEL.falloff,_PANEL.profile):
        editor.actionStarted.connect(_begin_curve_action)
        editor.actionFinished.connect(_end_curve_action)
    if toolbox is not None:
        toolbox._bevel_editor_size = QtCore.QSize(toolbox.size())
        if not hasattr(toolbox,"_bevel_profile_row"):
            toolbox._bevel_profile_row = toolbox.layout().rowCount()
        toolbox.layout().addWidget(_PANEL,toolbox._bevel_profile_row,0,1,2)
    else:
        _PANEL.setWindowTitle("Bevel — профили")
        _PANEL.resize(320,220)
    _PANEL.show()


def _build_menu(parent=None):
    menu = QtWidgets.QMenu(parent)
    state = _SESSION or type("Settings",(),_DEFAULTS)()
    menu.addSection("Затухание локальной ширины")
    falloff_group = QtGui.QActionGroup(menu)
    falloff_group.setExclusive(True)
    for label,kind in (("Линейное","linear"),("Округлое","round"),("Своё…","custom")):
        action = menu.addAction(label)
        action.setCheckable(True)
        action.setChecked(state.falloff_kind == kind)
        falloff_group.addAction(action)
        action.triggered.connect(lambda checked=False,k=kind:(_setting("falloff_kind",k),
                                    show_editor("falloff",parent) if k == "custom" else None))
    menu.addSection("Форма сечения")
    profile_group = QtGui.QActionGroup(menu)
    profile_group.setExclusive(True)
    for label,kind,shape in (("Плоская","round",0),("Округлая","round",1),
                              ("Вогнутая","round",-1),("Своё…","custom",0)):
        action = menu.addAction(label)
        action.setCheckable(True)
        action.setChecked(state.profile_kind == kind and (kind == "custom" or state.shape == shape))
        profile_group.addAction(action)
        action.triggered.connect(lambda checked=False,k=kind,s=shape:(_settings(profile_kind=k,shape=s),
                                    show_editor("profile",parent) if k == "custom" else None))
    menu.addSeparator()
    edit = menu.addAction("Редактировать последнюю фаску")
    edit.setEnabled(_SESSION is None)
    edit.triggered.connect(edit_saved)
    cuts = menu.addAction("Добавить поперечный разрез   Shift + +")
    cuts.triggered.connect(add_transverse_cut)
    remove = menu.addAction("Убрать поперечный разрез   Shift + −")
    remove.setEnabled(_SESSION is not None and _SESSION.transverse_cuts>0)
    remove.triggered.connect(remove_transverse_cut)
    reset = menu.addAction("Сбросить локальную ширину")
    reset.triggered.connect(lambda:_setting("weights",{}))
    return menu


def show_menu(parent=None,pos=None):
    menu = _build_menu(parent)
    menu.exec(pos or QtGui.QCursor.pos())


def shutdown():
    """Clean up before reloading modules in an already open Maya."""
    global _FILTER,_TIMER,_PANEL,_VALUES
    deactivate()
    app = QtWidgets.QApplication.instance()
    if _FILTER is not None and app is not None:
        app.removeEventFilter(_FILTER)
        _FILTER.deleteLater()
    _FILTER = None
    if _TIMER is not None:
        _TIMER.deleteLater()
    _TIMER = None
    if _PANEL is not None:
        try:
            _PANEL.hide()
            _editor_hidden(_PANEL)
            _PANEL.deleteLater()
        except RuntimeError:
            pass
    _PANEL = None
    if _VALUES is not None:
        _VALUES.hide()
        _VALUES.deleteLater()
    _VALUES = None
