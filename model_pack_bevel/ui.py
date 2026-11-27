"""Maya drag context, persistent editing and compact contextual controls."""
from __future__ import annotations
import math
import time

import maya.cmds as cmds
from maya.api import OpenMaya as om, OpenMayaUI as omui
from PySide6 import QtCore, QtGui, QtWidgets

from slide_for_maya import ui as slide_ui
from . import band_overlay, history, support_ribbons
from .interaction import Gesture, gesture_mode, right_gesture_mode
from .profiles import Curve
from .session import BevelSession, DEFAULT_SUPPORT_PERCENT, MAX_SUPPORT_EDGES, without_undo

CONTEXT = "modelPackGestureBevelContext"
EDIT_WIDTH_MULTIPLIER = 3.0
_SESSION = None
_MANUAL_SELECT = False
_AUTOSAVE_PAUSED = False
_DRAG = None
_FILTER = None
_TIMER = None
_PENDING = None
_TOOLBOX = None
_PANEL = None
_VALUES = None
_SHIFT_CLICK = None
_BAND_CALLBACKS = []
_BAND_CHOICES = []
_BAND_OVERLAY = None
_BAND_TIMER = None
_DEFAULTS = dict(falloff_kind="round", profile_kind="round", shape=0.0,segments=1,
                 all_sections=False,keep_equal=False,sharp=False,support_edges=0,support_percent=DEFAULT_SUPPORT_PERCENT,
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
    if getattr(session,'recovered',False):
        # The visible end profile is a grip too. A ray just outside its
        # silhouette misses the surface, but must still allow a local drag.
        view=omui.M3dView.active3dView()
        points=slide_ui._mesh_fn(session.preview).getPoints(om.MSpace.kWorld)
        sections=[]
        for index,row in enumerate(session.after_rows):
            projected=[view.worldToView(points[v]) for v in row]
            distance=min((slide_ui._point_segment_distance(anchor,a[:2],b[:2])
                          for a,b in zip(projected,projected[1:]) if a[2] or b[2]),default=math.inf)
            sections.append((distance,index))
        distance,index=min(sections,default=(math.inf,None))
        if distance<=14:
            for edge,(a,b) in session.graph.edges.items():
                if index in (a,b):return edge,0. if index==a else 1.
    projected = _screen_graph(session)
    plan = getattr(session, 'history_plan', None)
    guides = plan.guide_edges if plan and plan.joined else session.graph.edges
    edges = [(edge, session.graph.edges[edge]) for edge in guides if edge in session.graph.edges]
    candidates = []
    for edge,(a,b) in edges:
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
    fn = slide_ui._mesh_fn(getattr(session, '_display_mesh', None) or session.preview)
    view = omui.M3dView.active3dView()
    origin,direction = om.MPoint(),om.MVector()
    view.viewToWorld(int(anchor[0]),int(anchor[1]),origin,direction)
    hit = fn.closestIntersection(om.MFloatPoint(origin),om.MFloatVector(direction),
                                 om.MSpace.kWorld,1e10,False)
    if hit is None:
        raise ValueError("Начните локальный жест на поверхности фаски.")
    point = tuple(hit[0])[:3]
    choices = []
    for member,(a,b) in edges:
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
    if session.edit_node or getattr(session, 'recovered', False):
        return session.motion_scale * EDIT_WIDTH_MULTIPLIER
    return session.motion_scale


def _modifiers():
    modifiers = QtWidgets.QApplication.keyboardModifiers()
    return bool(modifiers & QtCore.Qt.ShiftModifier),bool(modifiers & QtCore.Qt.ControlModifier)


def _set_session(session):
    global _SESSION, _AUTOSAVE_PAUSED
    _SESSION = session
    if cmds.autoSave(query=True, enable=True):
        cmds.autoSave(enable=False)
        _AUTOSAVE_PAUSED = True


def _resume_autosave():
    global _AUTOSAVE_PAUSED
    if _AUTOSAVE_PAUSED:
        cmds.autoSave(enable=True)
        _AUTOSAVE_PAUSED = False


def _start_session(existing_only=False, *, sharp=None, recognize_hard_guides=True):
    if _SESSION is None:
        saved = _history_selection()
        if saved:
            _set_session(BevelSession(saved.mesh, (), history_node=saved.node, history_plan=history.HistoryEdit(saved)))
            _status()
            return _SESSION
        from .recovered import selected_band, selected_components, RecoveredBevelSession
        band=selected_band()
        if band is not None:
            _set_session(RecoveredBevelSession(*band))
            _status()
            return _SESSION
        if existing_only:
            if not recognize_hard_guides:
                return None
            guides = selected_components(om.MFn.kMeshEdgeComponent)
            if guides is None or len(guides[1]) < 2:
                return None
            from .hard_loop_edit import find_band, hard_edges

            mesh, edges = guides
            edges = hard_edges(mesh, edges)
            if len(edges) < 2:
                return None
            saved = history.from_edges(mesh, edges)
            if saved:
                _set_session(BevelSession(saved.mesh, (), history_node=saved.node, history_plan=history.HistoryEdit(saved)))
            else:
                faces = find_band(mesh, edges)
                if faces:
                    _set_session(RecoveredBevelSession(mesh, faces))
            if _SESSION is not None:
                _status()
            return _SESSION
        settings = dict(_DEFAULTS)
        if sharp is not None:
            settings["sharp"] = sharp
        selection = om.MGlobal.getActiveSelectionList()
        if not selection.length() and active():
            selection = om.MGlobal.getHiliteList()
        target = _object_edges(selection)
        if target is not None:
            mesh, components = target
            if not components.length():
                raise ValueError('На меше нет внутренних хард-рёбер. Выделите рёбра для фаски.')
            _, component = components.getComponent(0)
            edges = om.MFnSingleIndexedComponent(component).getElements()
        else:
            mesh = edges = None
        _set_session(BevelSession(mesh, edges, **{key: settings[key] for key in
            ("segments", "sharp", "support_edges", "support_percent", "keep_equal")}))
        for key,value in settings.items():
            setattr(_SESSION,key,value)
    return _SESSION


def _history_selection():
    from .recovered import selected_faces
    selection=selected_faces()
    if selection is None:return None
    shape,faces=selection
    return history.from_faces(shape, faces)


def edit_selected_faces(toolbox=None):
    """Use the explicit polygon selection without any band detection."""
    try:
        if _SESSION is not None:
            raise ValueError('Завершите текущую фаску клавишей Enter или отмените Esc, затем выделите полигоны.')
        from .recovered import selected_faces, RecoveredBevelSession
        selection = selected_faces()
        if selection is None:
            raise ValueError('Выделите полигонами всю полосу нужной фаски на одном меше.')
        session = RecoveredBevelSession(*selection, manual=True)
        session.manual = True
        _set_session(session)
        if not active():
            activate(toolbox or _TOOLBOX, select_existing=False, select_object_edges=False)
        _status()
        cmds.refresh(currentView=True)
        return session
    except Exception as exc:
        _warn(exc)
        return None


def select_manual_faces(toolbox=None):
    """Leave gestures for native Maya polygon selection, keeping the panel."""
    global _MANUAL_SELECT, _TOOLBOX
    if _SESSION is not None:
        _warn('Завершите текущую фаску клавишей Enter или отмените Esc.')
        return
    selection = om.MGlobal.getActiveSelectionList()
    meshes = cmds.ls(sl=True, objectsOnly=True, long=True) or cmds.ls(hilite=True, long=True) or []
    cmds.setToolTo('selectSuperContext')
    _MANUAL_SELECT = True
    _TOOLBOX = toolbox or _TOOLBOX
    with without_undo():
        cmds.selectMode(component=True)
        cmds.selectType(allComponents=False)
        cmds.selectType(polymeshFace=True)
        if meshes:
            cmds.hilite(meshes, replace=True)
        om.MGlobal.setActiveSelectionList(selection)
    show_values(_TOOLBOX)
    cmds.inViewMessage(amg='Выберите полигоны фаски → «Редактировать выбранные полигоны»',
                       pos='midCenterTop', fade=True)


def _band_meshes():
    if _SESSION is not None:
        return [(cmds.ls(_SESSION.mesh, long=True) or [_SESSION.mesh])[0]]
    selection = cmds.ls(selection=True, objectsOnly=True, long=True) or cmds.ls(hilite=True, long=True) or []
    meshes = []
    for item in selection:
        if cmds.nodeType(item) == 'mesh':
            item = item.rsplit('|', 1)[0]
        if cmds.listRelatives(item, shapes=True, noIntermediate=True, type='mesh') and item not in meshes:
            meshes.append(item)
    return meshes


def _band_choices():
    meshes = _band_meshes()
    choices = []
    for mesh in meshes:
        try:
            saved = history.bands(mesh)
            if not saved:
                ribbons, corners = support_ribbons.detect(mesh)
                if ribbons and corners:
                    saved = tuple(band_overlay.BakedBand(mesh, faces, joined=True) for faces in ribbons)
            if not saved:
                from .bevel_detection import detect

                found = detect(mesh, include_corners=False)
                if not found.ready_faces:
                    found = detect(mesh, sensitivity='extended', include_corners=False)
                saved = tuple(band_overlay.BakedBand(mesh, group.faces) for group in found.groups if group.ready)
                if not saved:
                    from .hard_loop_edit import bands_from_hard_rims

                    saved = tuple(band_overlay.BakedBand(mesh, faces) for faces in bands_from_hard_rims(mesh))
        except (ValueError, RuntimeError):
            continue
        for index, band in enumerate(saved, 1):
            label = f'Фаска {index}'
            if len(meshes) > 1:
                label = f'{mesh.rsplit("|", 1)[-1]} · {label}'
            choices.append((label, band))
    return choices


def _active_band():
    plan = getattr(_SESSION, 'history_plan', None)
    return plan.band if plan else None


def _choose_band(band, selected=None):
    global _SESSION
    try:
        if not active():
            activate(_TOOLBOX, select_existing=False)
        if _SESSION is not None:
            plan = getattr(_SESSION, 'history_plan', None)
            if ((plan and band == plan.band and (selected is None or tuple(selected) == plan.selected_bands))
                    or (not _SESSION.edit_node and band is None)):
                _sync_bevels()
                return
            if plan and plan.blocked:
                raise ValueError(plan.blocked)
            needed = getattr(_SESSION, 'recovered', False) or _SESSION.edit_node or _SESSION.width > 1e-9
            node = commit()
            if node is None and needed:
                return
            if plan:
                band = plan.committed_routes.get(band, band)
                if selected is not None:
                    selected = tuple(plan.committed_routes.get(item, item) for item in selected)
                    available = set(history.bands(plan.band.mesh))
                    if node:
                        selected = tuple(item if item in available else history.Band(item.mesh, node, item.edges)
                                         for item in selected)
                    if not all(item in available for item in selected):
                        raise ValueError('После изменения топологии выберите кружки заново.')
                    band = selected[0] if selected else None
        if band is not None:
            _set_session(BevelSession(band.mesh, (), history_node=band.node, history_plan=history.HistoryEdit(band, selected)))
        _sync_editor()
        _status()
        cmds.refresh(currentView=True)
    except Exception as exc:
        _warn(exc)
    _sync_bevels()


def _sync_bevels():
    global _BAND_CHOICES
    if _VALUES is not None:
        _BAND_CHOICES = _band_choices()
        if _BAND_CHOICES:
            _queue_band_overlay()
        else:
            _clear_band_overlay()


def _queue_band_overlay():
    global _BAND_TIMER
    if not _BAND_CHOICES or cmds.about(batch=True):
        return
    if _BAND_TIMER is None:
        _BAND_TIMER = QtCore.QTimer()
        _BAND_TIMER.setSingleShot(True)
        _BAND_TIMER.timeout.connect(lambda: cmds.evalDeferred(_draw_band_overlay))
    if not _BAND_TIMER.isActive():
        _BAND_TIMER.start(24)


def _draw_band_overlay():
    global _BAND_OVERLAY
    if _VALUES is not None and _VALUES.isVisible() and _BAND_CHOICES:
        if _BAND_OVERLAY is None:
            _BAND_OVERLAY = band_overlay.BevelOverlay()
        _BAND_OVERLAY.sync(_BAND_CHOICES, _SESSION)
    else:
        _clear_band_overlay()


def _clear_band_overlay():
    global _BAND_OVERLAY, _BAND_TIMER, _BAND_CHOICES
    if _BAND_TIMER is not None:
        _BAND_TIMER.stop()
        _BAND_TIMER.deleteLater()
        _BAND_TIMER = None
    if _BAND_OVERLAY is not None:
        _BAND_OVERLAY.close()
        _BAND_OVERLAY = None
    _BAND_CHOICES = []


def _on_selection_changed(*_):
    if _VALUES is not None and _SESSION is None:
        QtCore.QTimer.singleShot(0, _sync_bevels)


def edit_saved():
    global _SESSION
    try:
        if not active():
            activate(_TOOLBOX, select_object_edges=False)
        if _SESSION is None:
            _start_session(existing_only=True)
        if _SESSION is None:
            raise ValueError('Выделите полигоны фаски или её луп хард-рёбер.')
        _status()
        _sync_bevels()
    except Exception as exc:
        _warn(exc)


def _on_press():
    global _SESSION,_DRAG,_SHIFT_CLICK
    try:
        button = cmds.draggerContext(CONTEXT,q=True,button=True)
        if button not in (1, 3):
            return
        shift,ctrl = _modifiers()
        anchor = tuple(cmds.draggerContext(CONTEXT,q=True,anchorPoint=True))[:2]
        if button == 3:
            _SHIFT_CLICK = None
            _begin_viewport_right_drag(anchor, shift, ctrl, anchor)
            return
        if not shift or ctrl:
            _SHIFT_CLICK = None
        if shift and not ctrl and (_SESSION is None or _can_box_markers(anchor)):
            _DRAG = dict(selection=True,anchor=anchor,end=anchor)
            return
        if not shift and not ctrl:
            _DRAG = dict(click_or_drag=True, anchor=anchor)
            return
        _begin_left_drag(anchor, shift, ctrl)
    except Exception as exc:
        _DRAG = None
        _warn(exc)


def _can_box_markers(anchor):
    if _BAND_OVERLAY is None or _SESSION.snapshot() != _SESSION._initial:
        return False
    view = omui.M3dView.active3dView()
    origin, direction = om.MPoint(), om.MVector()
    view.viewToWorld(round(anchor[0]), round(anchor[1]), origin, direction)
    fn = history._mesh_fn(_SESSION._display_mesh or _SESSION.mesh)
    inverse = fn.dagPath().inclusiveMatrixInverse()
    return history._ray_hit(fn, origin * inverse, direction * inverse) is None


def _begin_left_drag(anchor, shift, ctrl):
    global _DRAG
    if _SESSION is None:
        if ctrl:
            raise ValueError("Сначала создайте фаску движением без модификаторов.")
        _start_session(sharp=False)
    mode = gesture_mode(shift, ctrl, sharp=getattr(_SESSION, 'sharp', False))
    if getattr(_SESSION, 'width_only', False) and mode in ('shape', 'segments'):
        _DRAG = None
        return
    gesture = Gesture(mode, _SESSION.width, _SESSION.shape, _SESSION.segments,
                      _scale(_SESSION), fine=shift and mode == 'width')
    _DRAG = dict(anchor=anchor, gesture=gesture, weights=dict(_SESSION.weights))
    if shift and not ctrl:
        _DRAG.update(shift_click=True, moved=False)
    if mode == 'local':
        edge, t = _pick_graph(_SESSION, anchor)
        _DRAG['influence'] = _SESSION.influence(edge, t)
    _SESSION.begin_action()


def _begin_viewport_right_drag(anchor, shift, ctrl, pick_anchor):
    global _DRAG
    if not shift and not ctrl:
        _DRAG = dict(click_or_drag=True, anchor=anchor, right=True, pick_anchor=pick_anchor)
    else:
        _begin_right_drag(anchor, shift, ctrl)


def _begin_right_drag(anchor, shift, ctrl):
    global _DRAG
    mode = right_gesture_mode(shift, ctrl)
    existing = _SESSION is not None
    session = _start_session(sharp=mode == "width")
    if getattr(getattr(session, 'history_plan', None), 'joined', False):
        raise ValueError('Профиль и опорные рёбра общие для связанных фасок.')
    if getattr(session, "recovered", False) and (mode == 'width' or not session.can_edit_supports):
        raise ValueError("Эти жесты доступны для фасок с исходными рёбрами.")
    session.begin_action()
    try:
        if mode == "width":
            session.change_supports(sharp=not session.sharp if existing else True)
        gesture = Gesture(mode, session.width, session.shape, session.segments, _scale(session),
                          support_edges=session.support_edges, support_percent=session.display_support_percent)
        _DRAG = dict(anchor=anchor, gesture=gesture, last_value=gesture.value((0, 0)))
        _status()
    except Exception:
        _DRAG = None
        session.end_action()
        raise


def _timer():
    global _TIMER
    if _TIMER is None:
        _TIMER = QtCore.QTimer()
        _TIMER.setSingleShot(True)
        _TIMER.timeout.connect(_flush)
    return _TIMER


def _on_drag(end=None):
    global _DRAG,_PENDING,_SHIFT_CLICK
    if _DRAG is None:
        return
    if end is None:
        end = tuple(cmds.draggerContext(CONTEXT,q=True,dragPoint=True))[:2]
    if _DRAG.get('click_or_drag'):
        delta = tuple(b-a for a,b in zip(_DRAG['anchor'], end))
        if max(map(abs, delta)) < 4:
            return
        try:
            begin = _begin_right_drag if _DRAG.get('right') else _begin_left_drag
            begin(_DRAG['anchor'], False, False)
        except Exception as exc:
            _DRAG = None
            _warn(exc)
            return
    if _DRAG.get("selection"):
        _DRAG["end"] = end
        if _BAND_OVERLAY is not None and math.dist(_DRAG['anchor'], end) >= 4:
            _BAND_OVERLAY.draw_selection_box(_DRAG['anchor'], end)
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
        if getattr(_SESSION, 'sharp', False) and gesture.mode in ("shape", "segments"):
            return
        if gesture.mode == "support_edges":
            value = min(value, MAX_SUPPORT_EDGES)
        if value == _DRAG.get("last_value"):
            return
        if gesture.mode == "local":
            _SESSION.local_width(value,_DRAG["influence"],_DRAG["weights"])
        elif gesture.mode in ("support_edges", "support_percent"):
            _SESSION.change_supports(**{gesture.mode: value})
            _remember_settings()
        else:
            _SESSION.update(**{gesture.mode:value})
        _DRAG["last_value"] = value
        _status()
        cmds.refresh(currentView=True)
    except Exception as exc:
        _warn(exc)
        cancel()


def _on_release(end=None):
    global _DRAG,_PENDING
    if _DRAG is None:
        return
    _on_drag(end)
    if _DRAG is None:
        return
    if _DRAG.get('click_or_drag'):
        anchor, _DRAG = _DRAG.get('pick_anchor', _DRAG['anchor']), None
        try:
            _pick_bevel(anchor)
        except Exception as exc:
            _warn(exc)
        return
    _timer().stop()
    if _DRAG.get("selection"):
        try:
            start, finish = _DRAG['anchor'], _DRAG['end']
            bands = ()
            if _BAND_OVERLAY is not None:
                _BAND_OVERLAY.draw_selection_box()
                if math.dist(start, finish) >= 4:
                    bands = _BAND_OVERLAY.markers_in_box(start, finish)
            if bands:
                _select_marker_box(bands)
            elif _SESSION is None:
                slide_ui._select_from_screen(start, finish, context=CONTEXT, button=1)
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
            if _DRAG["gesture"].mode == "width":
                _SESSION.confirm_support_spacing()
            _SESSION.end_action()
    _DRAG = None


def _pick_bevel(anchor):
    if _BAND_OVERLAY is not None:
        marker = _BAND_OVERLAY.pick_marker(anchor)
        if marker is not None:
            _toggle_marker(marker)
            return
    meshes = _band_meshes()
    view = omui.M3dView.active3dView()
    recovered = getattr(_SESSION, 'recovered', False)
    hit = history.pick(meshes, view, anchor, None if recovered else _SESSION, with_face=True)
    if hit is None:
        return
    band, mesh, face = hit
    marked = next((item for _, item in _BAND_CHOICES
                   if isinstance(item, band_overlay.BakedBand) and item.joined and item.mesh == mesh
                   and face in item.faces), None)
    if marked is not None:
        _toggle_marker(marked)
        return
    if any(isinstance(item, band_overlay.BakedBand) and item.joined and item.mesh == mesh
           for _, item in _BAND_CHOICES):
        return
    if band is not None:
        if band != _active_band():
            _choose_band(band)
    elif _SESSION is None or recovered:
        from .recovered import recover_band, RecoveredBevelSession
        from slide_for_maya.core import SlideError

        try:
            selection = recover_band(mesh, (face,))
        except SlideError:
            return  # A click on a regular face has no bevel to open.
        if recovered:
            if set(selection[1]) & set(_SESSION.faces):
                return
            if _SESSION.snapshot() == _SESSION._initial:
                cancel()
            elif commit() is None:
                return
            hit = history.pick(meshes, view, anchor, with_face=True)
            if hit is None:
                return
            _, mesh, face = hit
            selection = recover_band(mesh, (face,))
        _set_session(RecoveredBevelSession(*selection))
        choice = next((band for _, band in _BAND_CHOICES
                       if isinstance(band, band_overlay.BakedBand) and band.mesh == mesh and face in band.faces), None)
        if choice is not None:
            _SESSION._colored_baked_band = (choice,)
        _sync_bevels()
        _status()
        cmds.refresh(currentView=True)


def _select_marker_box(bands):
    first = bands[0]
    if isinstance(first, band_overlay.BakedBand):
        if any(not isinstance(band, band_overlay.BakedBand) or band.mesh != first.mesh
               or band.joined != first.joined for band in bands):
            raise ValueError('Выберите фаски одного меша.')
        current = getattr(_SESSION, '_colored_baked_band', ())
        _select_baked_markers(tuple(dict.fromkeys((*current, *bands))))
    else:
        if any(not isinstance(band, history.Band) or (band.mesh, band.node) != (first.mesh, first.node)
               for band in bands):
            raise ValueError('Выберите фаски из одной операции Bevel.')
        current = getattr(getattr(_SESSION, 'history_plan', None), 'selected_bands', ())
        selected = tuple(dict.fromkeys((*current, *bands)))
        _choose_band(selected[0], selected)


def _select_baked_markers(selected):
    from .recovered import recover_band, RecoveredBevelSession

    current = getattr(_SESSION, '_colored_baked_band', ())
    if selected == current:
        return
    if selected and any(band.mesh != selected[0].mesh or band.joined != selected[0].joined for band in selected):
        raise ValueError('Выберите фаски одного меша.')
    if _SESSION is not None:
        if not current or _SESSION.snapshot() != _SESSION._initial:
            raise ValueError('Завершите текущую правку, затем выберите фаски для совместного редактирования.')
        cancel()
    if selected:
        marker = selected[0]
        faces = tuple(sorted({face for item in selected for face in item.faces}))
        if marker.joined:
            _set_session(RecoveredBevelSession(history._shape(marker.mesh), faces,
                                               network=support_ribbons.detect(marker.mesh)))
        else:
            _set_session(RecoveredBevelSession(*recover_band(marker.mesh, faces)))
        _SESSION._colored_baked_band = selected
        _sync_bevels()
        _status()


def _toggle_marker(marker):
    if isinstance(marker, band_overlay.BakedBand):
        current = getattr(_SESSION, '_colored_baked_band', ())
        selected = tuple(item for item in current if item != marker) if marker in current else (*current, marker)
        _select_baked_markers(selected)
        return
    plan = getattr(_SESSION, 'history_plan', None)
    current = plan.selected_bands if plan else ()
    if current and any(item.mesh != marker.mesh or item.node != marker.node for item in current):
        raise ValueError('Совместное редактирование доступно для фасок из одной операции Bevel.')
    selected = tuple(item for item in current if item != marker) if marker in current else (*current, marker)
    _choose_band(selected[0] if selected else None, selected)


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
    if getattr(_SESSION, 'recovered', False) or getattr(getattr(_SESSION, 'history_plan', None), 'joined', False):
        return
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
        _VALUES.manual_requested.connect(lambda: edit_selected_faces(toolbox))
        _VALUES.manual_select_requested.connect(lambda: select_manual_faces(toolbox))
        if not _BAND_CALLBACKS:
            for event in ('SelectionChanged', 'Undo', 'Redo'):
                _BAND_CALLBACKS.append(om.MEventMessage.addEventCallback(event, _on_selection_changed))
        if toolbox is not None:
            toolbox._bevel_values_size = QtCore.QSize(toolbox.size())
            row = toolbox.layout().rowCount()
            toolbox.layout().addWidget(_VALUES,row,0,1,2)
        else:
            _VALUES.setWindowTitle("Bevel — значения")
            _VALUES.setWindowFlag(QtCore.Qt.Window,True)
    _VALUES.show()
    _sync_bevels()
    _status()


def _values_destroyed(panel):
    global _VALUES
    if _VALUES is panel:
        _clear_band_overlay()
        _remove_band_callbacks()
        _VALUES = None


def _remove_band_callbacks():
    for callback in _BAND_CALLBACKS:
        om.MMessage.removeCallback(callback)
    _BAND_CALLBACKS.clear()


def _hide_values():
    global _VALUES
    _clear_band_overlay()
    _remove_band_callbacks()
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
        if name in ("sharp", "support_edges", "support_percent"):
            value = bool(value) if name == "sharp" else int(value) if name == "support_edges" else value
            if _SESSION is None:
                _DEFAULTS[name] = value
            elif not getattr(_SESSION, "recovered", False) or _SESSION.can_edit_supports:
                _SESSION.change_supports(**{name: value})
                _remember_settings()
                cmds.refresh(currentView=True)
            _status()
            return
        session = _start_session()
        with session.action():
            if name=="transverse_cuts":
                session.change_transverse_cuts(int(value)-session.transverse_cuts)
            elif name in ("keep_equal","all_sections"):
                setattr(session,name,bool(value))
                session.update()
            else:
                session.update(**{name:int(value) if name=="segments" else value})
                if name == "width":
                    session.confirm_support_spacing()
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
    try:
        _clear()
        node = session.commit()
        _DEFAULTS.update(shape=session.shape,segments=session.segments,profile_kind=session.profile_kind,
                         profile_curve=session.profile_curve,falloff_kind=session.falloff_kind,
                         falloff_curve=session.falloff_curve,keep_equal=session.keep_equal,
                         all_sections=session.all_sections,
                         **{key: getattr(session, key) for key in ("support_edges", "support_percent")
                            if hasattr(session, key)})
        _status()
        _sync_bevels()
        return node
    except Exception as exc:
        if not session.closed:
            session.cancel()
        _warn(exc)
        return None
    finally:
        _resume_autosave()


def cancel(restore_selection=True):
    global _SESSION
    _clear()
    session,_SESSION = _SESSION,None
    try:
        if session is not None:
            session.cancel(restore_selection)
    finally:
        _resume_autosave()
    _status()
    _sync_bevels()


def _status():
    _queue_band_overlay()
    if _VALUES is not None:
        state = _SESSION or type("State",(),dict(width=0,transverse_cuts=0,**_DEFAULTS))()
        for field in _VALUES.fields.values():
            field.setEnabled(True)
        _VALUES.sync(state)
        if _MANUAL_SELECT and _SESSION is None:
            _VALUES.setTitle('Bevel · выбор полигонов')
            for field in _VALUES.fields.values():
                field.setEnabled(False)
    if _SESSION is not None:
        contact = " · предел: контакт" if _SESSION.contact_faces else ""
        plan = getattr(_SESSION, 'history_plan', None)
        if plan and plan.blocked:
            contact = f" · {plan.blocked}"
        profile = ("острый профиль" if _SESSION.sharp else
                   f"{_SESSION.segments} сегм. · форма {_SESSION.shape:+.2f}")
        plan_label = f"Фаска {plan.index}" if plan else "Bevel"
        cmds.inViewMessage(amg=(f"{plan_label}: {_SESSION.width:.4f} · {profile} "
                               f"· {_SESSION.transverse_cuts} попереч.{contact} · Enter / Esc"),
                           pos="midCenterTop",fade=False)


def _remember_settings():
    if _SESSION is not None:
        for key in _DEFAULTS:
            if key != "sharp" and hasattr(_SESSION,key):
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
    if getattr(_SESSION,'recovered',False):return
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
    timer = getattr(_TOOLBOX, '_scene_reset_timer', None)
    if timer is not None and timer.isActive():
        cancel(restore_selection=False)
        _hide_values()
        return
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
    will_apply = _SESSION.width > 1e-9 or getattr(_SESSION,'recovered',False) or bool(getattr(_SESSION,'edit_node',None))
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
            except (TypeError, RuntimeError):
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
    def __init__(self):
        super().__init__()
        self.right_drag = None
        self.right_end = None
        self.suppress_context = False

    def _right_mouse(self, target, event):
        from model_pack_tube_viewport import viewport_widget

        kind = event.type()
        if self.right_drag is not None and self.right_drag is _DRAG:
            if kind in (QtCore.QEvent.ApplicationDeactivate, QtCore.QEvent.WindowDeactivate):
                self.right_drag = None
                _on_release(self.right_end)
                return False
            if kind == QtCore.QEvent.MouseMove or (kind == QtCore.QEvent.MouseButtonRelease and
                                                   event.button() == QtCore.Qt.RightButton):
                position = event.globalPosition()
                self.right_end = position.x(), -position.y()
                if kind == QtCore.QEvent.MouseMove:
                    _on_drag(self.right_end)
                    self.right_drag = _DRAG
                else:
                    self.right_drag = None
                    _on_release(self.right_end)
                return True
        if kind == QtCore.QEvent.ContextMenu and self.suppress_context:
            self.suppress_context = False
            return viewport_widget(target, event.globalPos()) is not None
        if kind != QtCore.QEvent.MouseButtonPress or event.button() != QtCore.Qt.RightButton:
            return False
        self.suppress_context = False
        if _DRAG is not None:
            return False
        modifiers = event.modifiers()
        if modifiers & (QtCore.Qt.AltModifier | QtCore.Qt.MetaModifier):
            return False
        shift = bool(modifiers & QtCore.Qt.ShiftModifier)
        ctrl = bool(modifiers & QtCore.Qt.ControlModifier)
        position = event.globalPosition()
        widget = viewport_widget(target, position.toPoint())
        if widget is None:
            return False
        point = widget.mapFromGlobal(position.toPoint())
        pick_anchor = point.x(), widget.height() - 1 - point.y()
        self.right_end = position.x(), -position.y()
        self.suppress_context = True
        try:
            _begin_viewport_right_drag(self.right_end, shift, ctrl, pick_anchor)
            self.right_drag = _DRAG
        except (RuntimeError, ValueError) as error:
            _warn(error)
        return True

    def eventFilter(self,target,event):
        kind = event.type()
        if kind not in (QtCore.QEvent.KeyPress, QtCore.QEvent.ShortcutOverride):
            if kind in (QtCore.QEvent.MouseButtonPress, QtCore.QEvent.MouseButtonRelease,
                        QtCore.QEvent.MouseMove, QtCore.QEvent.ContextMenu,
                        QtCore.QEvent.ApplicationDeactivate, QtCore.QEvent.WindowDeactivate) and active():
                return self._right_mouse(target, event)
            return False
        if not active():
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
            if not getattr(_SESSION,'recovered',False):command = add_transverse_cut
        elif key in (QtCore.Qt.Key_Minus,QtCore.Qt.Key_Underscore) and shift and not ctrl:
            if not getattr(_SESSION,'recovered',False):command = remove_transverse_cut
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


def _object_edges(selection):
    if selection.length() != 1:
        return None
    try:
        path, component = selection.getComponent(0)
    except (RuntimeError, TypeError):
        return None
    if not component.isNull():
        return None
    if path.node().hasFn(om.MFn.kTransform):
        shapes = cmds.listRelatives(path.fullPathName(), shapes=True, type='mesh',
                                    noIntermediate=True, fullPath=True) or []
        if len(shapes) != 1:
            return None
        path = om.MSelectionList().add(shapes[0]).getDagPath(0)
    if not path.node().hasFn(om.MFn.kMesh) or om.MFnDagNode(path).isIntermediateObject:
        return None
    from .hard_loop_edit import creation_edges

    shape = path.fullPathName()
    mesh = shape.rsplit('|',1)[0]
    edges = sorted(creation_edges(shape))
    components = om.MSelectionList()
    if edges:
        edge_component = om.MFnSingleIndexedComponent()
        selected_edges = edge_component.create(om.MFn.kMeshEdgeComponent)
        edge_component.addElements(edges)
        components.add((path, selected_edges))
    return mesh, components


def activate(toolbox=None, *, select_existing=True, select_object_edges=True):
    global _FILTER,_TOOLBOX,_MANUAL_SELECT
    _TOOLBOX = toolbox
    _MANUAL_SELECT = False
    if active():
        deactivate()
        return
    selection = om.MGlobal.getActiveSelectionList()
    selection_mode=om.MGlobal.selectionMode();component_mask=om.MGlobal.componentSelectionMask()
    prefix = "import model_pack_bevel.ui as bevel_ui; bevel_ui."
    callbacks = dict(pressCommand=prefix+"_on_press()",dragCommand=prefix+"_on_drag()",
                     releaseCommand=prefix+"_on_release()",finalize=prefix+"_on_finalize()",
                     undoMode="none",space="screen",projection="viewPlane")
    if cmds.draggerContext(CONTEXT,exists=True):
        cmds.draggerContext(CONTEXT,edit=True,**callbacks)
    else:
        cmds.draggerContext(CONTEXT,cursor="crossHair",**callbacks)
    # Native Maya filters face components when switching the selection mask
    # to edges. Resolve the selected baked band before changing that mask or
    # tool context. Standalone Maya does not perform this UI filtering.
    if select_existing and _SESSION is None:
        try:
            session=_start_session(existing_only=True, recognize_hard_guides=False)
            if getattr(session,'recovered',False):
                session.selection_mode=selection_mode;session.component_mask=component_mask
        except Exception as exc:_warn(exc)
    # Component mode clears a whole-object selection. Resolve the Bevel edges
    # up front and make them the editable selection once the mesh is hilited.
    object_edges = _object_edges(selection) if select_existing and select_object_edges and _SESSION is None else None
    cmds.setToolTo(CONTEXT)
    with without_undo():
        cmds.selectMode(component=True)
        cmds.selectType(allComponents=False)
        cmds.selectType(polymeshEdge=True)
        if getattr(_SESSION,'recovered',False):
            meshes=[_SESSION.preview]
        elif object_edges is not None:
            meshes = [object_edges[0]]
        else:
            om.MGlobal.setActiveSelectionList(selection)
            meshes = cmds.ls(sl=True,objectsOnly=True,long=True) or []
        if meshes:
            cmds.hilite(meshes,replace=True)
        if object_edges is not None:
            om.MGlobal.setActiveSelectionList(object_edges[1])
    if _FILTER is None:
        _FILTER = ViewportKeys()
        QtWidgets.QApplication.instance().installEventFilter(_FILTER)
    hint = ("Bevel · Ширина · Shift+Ctrl: локально · Enter / Esc"
            if getattr(_SESSION,'width_only',False) else
            "Bevel · ЛКМ: выбрать фаску · Перетаскивание: ширина · Enter / Esc")
    cmds.inViewMessage(amg=hint,
                       pos="midCenterTop",fade=True)
    show_values(toolbox)


def deactivate():
    _on_finalize()
    if active():
        cmds.setToolTo("selectSuperContext")


def _setting(key,value):
    _settings(**{key:value})


def _settings(**values):
    if _SESSION is not None:
        if getattr(getattr(_SESSION, 'history_plan', None), 'joined', False) and any(
                key == 'shape' or key.startswith('profile_') for key in values):
            _warn('Профиль общий для связанных фасок.')
            return
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
    manual = menu.addAction('Edit Selected Faces (Manual)')
    manual.setEnabled(_SESSION is None)
    manual.triggered.connect(lambda checked=False: select_manual_faces(parent))
    menu.addSeparator()
    menu.addSection("Затухание локальной ширины")
    falloff_group = QtGui.QActionGroup(menu)
    falloff_group.setExclusive(True)
    for label,kind in (('Linear',"linear"),('Round',"round"),('Custom…',"custom")):
        action = menu.addAction(label)
        action.setCheckable(True)
        action.setChecked(state.falloff_kind == kind)
        falloff_group.addAction(action)
        action.triggered.connect(lambda checked=False,k=kind:(_setting("falloff_kind",k),
                                    show_editor("falloff",parent) if k == "custom" else None))
    menu.addSection("Форма сечения")
    profile_group = QtGui.QActionGroup(menu)
    profile_group.setExclusive(True)
    for label,kind,shape in (('Flat',"round",0),('Round',"round",1),
                              ('Concave',"round",-1),('Custom…',"custom",0)):
        action = menu.addAction(label)
        action.setEnabled(not getattr(state,'width_only',False) and not getattr(state,'sharp',False))
        action.setCheckable(True)
        action.setChecked(state.profile_kind == kind and (kind == "custom" or state.shape == shape))
        profile_group.addAction(action)
        action.triggered.connect(lambda checked=False,k=kind,s=shape:(_settings(profile_kind=k,shape=s),
                                    show_editor("profile",parent) if k == "custom" else None))
    menu.addSeparator()
    edit = menu.addAction('Edit Selected Bevel')
    edit.setEnabled(_SESSION is None)
    edit.triggered.connect(edit_saved)
    cuts = menu.addAction('Add Cross Cut   Shift + +')
    cuts.setEnabled(not getattr(_SESSION,'recovered',False))
    cuts.triggered.connect(add_transverse_cut)
    remove = menu.addAction('Remove Cross Cut   Shift + −')
    remove.setEnabled(_SESSION is not None and not getattr(_SESSION,'recovered',False) and _SESSION.transverse_cuts>0)
    remove.triggered.connect(remove_transverse_cut)
    reset = menu.addAction('Reset Local Width')
    reset.triggered.connect(lambda:_setting("weights",{}))
    return menu


def show_menu(parent=None,pos=None):
    menu = _build_menu(parent)
    menu.exec(pos or QtGui.QCursor.pos())


def shutdown():
    """Clean up before reloading modules in an already open Maya."""
    global _FILTER,_TIMER,_PANEL,_VALUES,_MANUAL_SELECT
    _MANUAL_SELECT = False
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
