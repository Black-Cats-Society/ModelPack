"""Clone Slide viewport gesture and optional numeric controls."""

from __future__ import annotations

import math
import time
import sys
import json
from contextlib import contextmanager
from dataclasses import replace

import maya.cmds as cmds
from maya.api import OpenMaya as om
from maya.api import OpenMayaUI as omui

from .core import LiveEdit, SlideError, _EPS, _mesh_fn, _normal_distances, analyze_selection, apply
from .bevel import LiveBevel, analyze_bevel_selection
from .multiple import LiveMultiple, _matching_clone_side
from . import edit_session


_WINDOW = "slideForMayaWindow"
_CONTEXT = "slideForMayaDragContext"
_BEVEL_CONTEXT = "slideBevelForMayaDragContext"
_AMOUNT = "slideForMayaAmount"
_SIDE = "slideForMayaSide"
_MODE = "slideForMayaMode"
_STATE = None
_SESSION = None
_SESSION_CALLBACKS = []
_SESSION_UPDATING = False
_LAST_SHIFT_CLICK = None
_DRAG_MODE = "uniform"
_BEVEL_DRAG_MODE = "uniform"
_DRAG_COUNT = 1
_BEVEL_DRAG_COUNT = 1
_DRAG_BIAS_PERCENT = 50
_BEVEL_DRAG_BIAS_PERCENT = 50
_DRAG_SENSITIVITY = 3.0


@contextmanager
def _without_undo():
    enabled = cmds.undoInfo(query=True, state=True)
    if enabled:
        cmds.undoInfo(stateWithoutFlush=False)
    try:
        yield
    finally:
        if enabled:
            cmds.undoInfo(stateWithoutFlush=True)


def _discard_preview(state, restore_source=True):
    with _without_undo():
        if state.get("live") is not None:
            state["live"].cancel()
        temp = state.get("temp")
        for panel in state.get("isolate_panels", ()):
            if cmds.modelPanel(panel, exists=True):
                if temp and cmds.objExists(temp):
                    cmds.isolateSelect(panel, removeDagObject=temp)
                cmds.isolateSelect(panel, update=True)
        state["isolate_panels"] = []
        if temp and cmds.objExists(temp):
            cmds.delete(temp)
        if restore_source:
            cmds.setAttr(state["mesh"] + ".visibility", state["visible"])
            cmds.select(state["selection"], replace=True)
            prior_hilite = [node for node in state["hilite"] if cmds.objExists(node)]
            cmds.hilite(prior_hilite or state["mesh"], replace=True)
    state["live"] = None
    state["temp"] = None
    state["side"] = None
    state["multi_amount"] = None


def _include_isolated_preview(state, temp):
    """Display the duplicate wherever the source is isolated, preserving the set."""
    source = cmds.ls(state["mesh"], long=True)[0]
    shapes = cmds.listRelatives(source, shapes=True, fullPath=True) or []
    state["isolate_panels"] = []
    for panel in cmds.getPanel(type="modelPanel") or []:
        if not cmds.isolateSelect(panel, query=True, state=True):
            continue
        members = cmds.isolateSelect(panel, query=True, viewObjects=True)
        nodes = cmds.ls(cmds.sets(members, query=True) or [],
                        objectsOnly=True, long=True) if members else []
        if not any(node == source or node in shapes or source.startswith(node + "|")
                   for node in nodes or []):
            continue
        cmds.isolateSelect(panel, addDagObject=temp)
        state["isolate_panels"].append(panel)


def _start_preview(state, side):
    with _without_undo():
        temp = (edit_session.preview(state) if state.get('editing') else
                cmds.duplicate(state["mesh"], returnRootsOnly=True)[0])
        state["temp"] = temp
        try:
            _include_isolated_preview(state, temp)
            cmds.setAttr(state["mesh"] + ".visibility", False)
            cmds.setAttr(temp + ".visibility", True)
            cmds.hilite(temp, replace=True)
        except Exception:
            _discard_preview(state)
            raise
    if state["count"] > 1:
        state["side"] = side
        return
    if state.get("bevel"):
        source = state["bevel_plan"]
        plan = replace(source, mesh=temp,
                       runs=tuple(replace(run, mesh=temp) for run in source.runs))
        state["live"] = LiveBevel(plan, mode=state["mode"], preview=True)
    else:
        plan = replace(state["plans"][side], mesh=temp)
        state["live"] = LiveEdit(plan, clone=True, mode=state["mode"], preview=True)
    state["side"] = side


def _settings():
    if not cmds.window(_WINDOW, exists=True):
        return 0, True, _DRAG_MODE, 0.25
    side = cmds.radioButtonGrp(_SIDE, query=True, select=True) - 1
    mode = ("relative", "uniform")[cmds.optionMenuGrp(_MODE, query=True, select=True) - 1]
    amount = cmds.floatFieldGrp(_AMOUNT, query=True, value1=True)
    return side, True, mode, amount


def _run_from_window(*_):
    side, clone, mode, amount = _settings()
    try:
        actual = apply(amount, side=side, clone=True, mode=mode)
    except SlideError as exc:
        cmds.warning(f"Slide for Maya: {exc}")
        return
    cmds.inViewMessage(
        amg=f"Clone + Slide: {actual:.3f}",
        pos="midCenterTop",
        fade=True,
    )


def show():
    """Show the numeric controls; select a mesh edge before applying."""
    if cmds.window(_WINDOW, exists=True):
        cmds.deleteUI(_WINDOW)
    window = cmds.window(_WINDOW, title="Slide for Maya", sizeable=False, widthHeight=(430, 215))
    column = cmds.columnLayout(adjustableColumn=True, rowSpacing=8)
    cmds.text(label="Выделите ребро, открытую цепочку или петлю", align="left")
    cmds.radioButtonGrp(_SIDE, label="Сторона", numberOfRadioButtons=2, labelArray2=("0", "1"), select=1)
    cmds.optionMenuGrp(_MODE, label="Смещение")
    cmds.menuItem(label="Пропорционально")
    cmds.menuItem(label="Равная ширина")
    cmds.optionMenuGrp(_MODE, edit=True, select=2)
    cmds.setParent(column)
    cmds.floatFieldGrp(_AMOUNT, label="Значение", numberOfFields=1, value1=0.25)
    cmds.button(label="Применить", command=_run_from_window)
    cmds.button(label="Инструмент перетаскивания", command=lambda *_: activate_drag())
    cmds.text(label="Направление мыши выбирает сторону; Shift позволяет выделять рёбра.", align="left")
    cmds.showWindow(window)
    return window


def _screen_guide(plan, anchor):
    view = omui.M3dView.active3dView()
    starts = [view.worldToView(om.MPoint(*point)) for point in plan.starts]
    ends = [view.worldToView(om.MPoint(*point)) for point in plan.ends]
    nearest = min(range(len(starts)), key=lambda i: math.hypot(starts[i][0] - anchor[0], starts[i][1] - anchor[1]))
    return (ends[nearest][0] - starts[nearest][0], ends[nearest][1] - starts[nearest][1])


def _choose_by_drag(delta, guides, plans, mode):
    """Select the side whose projected rail best follows the cursor."""
    choices = []
    for side, guide in guides.items():
        squared = guide[0] ** 2 + guide[1] ** 2
        if squared < 4.0:
            continue
        along = delta[0] * guide[0] + delta[1] * guide[1]
        if along <= 0:
            continue
        relative = _DRAG_SENSITIVITY * along / squared
        if mode == "relative":
            amount = relative
        elif mode == "distance":
            amount = relative * min(math.dist(start, end) for start, end in
                                    zip(plans[side].starts, plans[side].ends))
        else:
            amount = relative * min(_normal_distances(plans[side]))
        choices.append((along / math.sqrt(squared), side, amount))
    if not choices:
        return None
    _, side, amount = max(choices)
    return side, amount


def _bevel_guide_index(delta, guides):
    """Choose a projected direction once, at the start of a gesture."""
    choices = []
    for index, guide in enumerate(guides):
        length = math.hypot(*guide)
        if length < 2.0:
            continue
        along = abs(delta[0] * guide[0] + delta[1] * guide[1])
        choices.append((along / length, index))
    best = max(choices, default=None)
    return best[1] if best is not None and best[0] > 0 else None


def _choose_bevel_by_drag(delta, guide, plan, mode):
    squared = guide[0] ** 2 + guide[1] ** 2
    if squared < 4.0:
        return None
    along = abs(delta[0] * guide[0] + delta[1] * guide[1])
    if along <= 0:
        return None
    relative = _DRAG_SENSITIVITY * along / squared
    if mode == "relative":
        return relative
    width = min((distance for run in plan.runs for distance in _normal_distances(run)
                 if distance > _EPS), default=None)
    return relative * width if width is not None else None


def _on_press(bevel=False):
    global _STATE, _LAST_SHIFT_CLICK
    _on_finalize(clear_selection_click=False,keep_session=True)
    context = _BEVEL_CONTEXT if bevel else _CONTEXT
    button = cmds.draggerContext(context, query=True, button=True)
    if button not in (1, 2):
        _LAST_SHIFT_CLICK = None
        return
    mode = _BEVEL_DRAG_MODE if bevel else _DRAG_MODE
    count = _BEVEL_DRAG_COUNT if bevel else _DRAG_COUNT
    bias_percent = (_BEVEL_DRAG_BIAS_PERCENT if bevel else _DRAG_BIAS_PERCENT)
    modifier = cmds.draggerContext(context, query=True, modifier=True) or "none"
    if "shift" in modifier.lower():
        if _SESSION is not None:
            same=edit_session.selection_key()==_SESSION['result_selection']
            _end_session()
            if same:cmds.select(clear=True)
        anchor = cmds.draggerContext(context, query=True, anchorPoint=True)
        _STATE = {"select_anchor": (int(anchor[0]), int(anchor[1])), "context": context,
                  "select_button": button, "select_time": time.monotonic()}
        return
    _LAST_SHIFT_CLICK = None
    _check_session_selection()
    if _SESSION is not None:
        if (bool(_SESSION.get('bevel'))==bevel and cmds.objExists(_SESSION['result_node']) and
                edit_session.selection_key()==_SESSION['result_selection']):
            _STATE=edit_session.clone_state(_SESSION)
            anchor=cmds.draggerContext(context,query=True,anchorPoint=True)
            _STATE['anchor']=tuple(anchor[:2])
            _STATE['mode']=mode;_STATE['count']=count;_STATE['bias_percent']=bias_percent
            if bevel:
                _STATE['guides']=tuple(_screen_guide(run,anchor) for run in _STATE['bevel_plan'].runs)
                _STATE['guide_index']=_SESSION['guide_index']
            else:_STATE['guides']={side:_screen_guide(plan,anchor) for side,plan in _STATE['plans'].items()}
            return
        _end_session()
    if bevel:
        try:
            plan = analyze_bevel_selection()
        except SlideError as exc:
            cmds.warning(f"Slide Bevel: {exc}")
            return
        anchor = cmds.draggerContext(context, query=True, anchorPoint=True)
        try:
            guides = tuple(_screen_guide(run, anchor) for run in plan.runs)
        except RuntimeError as exc:
            cmds.warning(f"Slide Bevel: не удалось получить проекцию viewport: {exc}")
            return
        _STATE = {
            "bevel": True, "bevel_plan": plan, "guides": guides, "mode": mode,
            "count": count, "bias_percent": bias_percent, "multi_amount": None,
            "live": None, "side": None, "temp": None, "guide_index": None,
            "anchor": (anchor[0], anchor[1]), "mesh": plan.mesh, "context": context,
            "selection": cmds.ls(selection=True, flatten=True, long=True),
            "hilite": cmds.ls(hilite=True, long=True) or [],
            "visible": cmds.getAttr(plan.mesh + ".visibility"),
        }
        _STATE['base_mesh']=edit_session.snapshot(plan.mesh)
        _STATE['base_materials']=edit_session.materials(plan.mesh)
        return
    plans = {}
    errors = []
    for side in (0, 1):
        try:
            plans[side] = analyze_selection(side)
        except SlideError as exc:
            errors.append(str(exc))
    if not plans:
        cmds.warning(f"Slide for Maya: {errors[0] if errors else 'Выделите рёбра mesh.'}")
        return
    anchor = cmds.draggerContext(context, query=True, anchorPoint=True)
    try:
        guides = {side: _screen_guide(plan, anchor) for side, plan in plans.items()}
    except RuntimeError as exc:
        cmds.warning(f"Slide for Maya: не удалось получить проекцию viewport: {exc}")
        return
    _STATE = {
        "anchor": (anchor[0], anchor[1]), "plans": plans, "guides": guides,
        "clone": True, "mode": mode, "count": count,
        "bias_percent": bias_percent, "multi_amount": None,
        "live": None, "side": None, "temp": None,
        "mesh": next(iter(plans.values())).mesh,
        "context": context,
        "selection": cmds.ls(selection=True, flatten=True, long=True),
        "hilite": cmds.ls(hilite=True, long=True) or [],
        "visible": cmds.getAttr(next(iter(plans.values())).mesh + ".visibility"),
    }
    _STATE['base_mesh']=edit_session.snapshot(_STATE['mesh'])
    _STATE['base_materials']=edit_session.materials(_STATE['mesh'])


def _update_multi_preview(state, amount, side):
    if (state["temp"] is not None and state["side"] == side and
            state["multi_amount"] is not None and
            math.isclose(state["requested_amount"], amount, rel_tol=1e-4, abs_tol=1e-6)):
        return
    first = state["temp"] is None or state["side"] != side
    if first:
        if state["temp"] is not None:
            _discard_preview(state, restore_source=False)
        _start_preview(state, side)
    with _without_undo():
        if first:
            source_ids = (state["bevel_plan"].edge_ids if state.get("bevel") else
                          state["plans"][side].edge_ids)
            cmds.select([f"{state['temp']}.e[{edge}]" for edge in source_ids], replace=True)
            preview_side = side or 0
            if not state.get("bevel"):
                original_ids = {int(component.rsplit("[", 1)[1][:-1])
                                for component in state["selection"]}
                if set(source_ids) != original_ids:
                    preview_side = _matching_clone_side(state["plans"][side])
            state["live"] = LiveMultiple(
                bevel=bool(state.get("bevel")), count=state["count"],
                mode=state["mode"], side=preview_side, bias_percent=state["bias_percent"])
        state["multi_amount"] = state["live"].update(amount)
    state["requested_amount"] = amount


def _on_drag():
    global _STATE
    if _STATE is None:
        return
    if "select_anchor" in _STATE:
        point = cmds.draggerContext(_STATE["context"], query=True, dragPoint=True)
        _STATE["select_end"] = (int(point[0]), int(point[1]))
        return
    point = cmds.draggerContext(_STATE["context"], query=True, dragPoint=True)
    delta = (point[0] - _STATE["anchor"][0], point[1] - _STATE["anchor"][1])
    if _STATE.get("bevel"):
        if math.hypot(*delta) >= 2.0 and _STATE["guide_index"] is None:
            _STATE["guide_index"] = _bevel_guide_index(delta, _STATE["guides"])
        guide_index = _STATE["guide_index"]
        amount = (_choose_bevel_by_drag(delta, _STATE["guides"][guide_index],
                                         _STATE["bevel_plan"], _STATE["mode"])
                  if guide_index is not None else None)
        if amount is not None:
            guide=_STATE['guides'][guide_index]
            direction=1 if delta[0]*guide[0]+delta[1]*guide[1]>=0 else -1
            if _STATE.get('editing'):
                amount=max(_EPS,_STATE['base_amount']+amount*direction*_STATE['direction'])
            else:_STATE['direction']=direction
        if math.hypot(*delta) < 2.0 or amount is None:
            if _STATE["temp"] is not None:
                try:
                    _discard_preview(_STATE)
                except Exception as exc:
                    _STATE = None
                    cmds.warning(f"Slide Bevel: {exc}")
                    return
                cmds.refresh(force=True, currentView=True)
            return
        try:
            if _STATE["count"] > 1:
                _update_multi_preview(_STATE, amount, None)
                cmds.refresh(force=True, currentView=True)
                return
            first = _STATE["temp"] is None
            if first:
                _start_preview(_STATE, None)
            with _without_undo():
                _STATE["live"].update(amount)
                if first:
                    cmds.select([f"{_STATE['temp']}.e[{edge}]" for edge in
                                 _STATE["live"]._new_edges],
                                replace=True)
        except Exception as exc:
            try:
                _discard_preview(_STATE)
            except Exception:
                pass
            _STATE = None
            cmds.warning(f"Slide Bevel: {exc}")
            return
        cmds.refresh(force=True, currentView=True)
        return
    chosen = _choose_by_drag(delta, _STATE["guides"], _STATE["plans"], _STATE["mode"])
    if _STATE.get('editing'):
        base_side=_STATE['base_side'];base=_STATE['base_amount'];guide=_STATE['guides'][base_side]
        along=delta[0]*guide[0]+delta[1]*guide[1]
        movement=delta if along>=0 else (-delta[0],-delta[1])
        change=_choose_by_drag(movement,{base_side:guide},_STATE['plans'],_STATE['mode'])
        if change is not None:
            value=base+change[1]*(1 if along>=0 else -1)
            side=base_side if value>=0 else next((s for s in _STATE['plans'] if s!=base_side),base_side)
            chosen=(side,max(_EPS,abs(value)))
    if math.hypot(*delta) < 2.0 or chosen is None:
        if _STATE["temp"] is not None:
            try:
                _discard_preview(_STATE)
            except Exception as exc:
                _STATE = None
                cmds.warning(f"Slide for Maya: {exc}")
                return
            cmds.refresh(force=True, currentView=True)
        return
    side, amount = chosen
    try:
        if _STATE["count"] > 1:
            _update_multi_preview(_STATE, amount, side)
            cmds.refresh(force=True, currentView=True)
            return
        new_side = _STATE["side"] != side
        if new_side:
            if _STATE["temp"] is not None:
                _discard_preview(_STATE, restore_source=False)
            _start_preview(_STATE, side)
        with _without_undo():
            _STATE["live"].update(amount)
            if new_side:
                cmds.select([f"{_STATE['temp']}.e[{edge}]" for edge in
                             _STATE["plans"][side].edge_ids], replace=True)
    except Exception as exc:
        try:
            _discard_preview(_STATE)
        except Exception:
            pass
        _STATE = None
        cmds.warning(f"Slide for Maya: {exc}")
        return
    cmds.refresh(force=True, currentView=True)


def _edge_pick_meshes():
    selected = cmds.ls(selection=True, flatten=True, long=True) or []
    meshes = list(dict.fromkeys(item.split(".e[")[0] for item in selected if ".e[" in item))
    if meshes:
        return meshes
    hilited = cmds.ls(hilite=True, long=True) or []
    if hilited:
        return hilited
    shapes = cmds.ls(type="mesh", long=True) or []
    result = []
    for shape in shapes:
        if cmds.getAttr(shape + ".intermediateObject"):
            continue
        parent = cmds.listRelatives(shape, parent=True, fullPath=True)
        if parent and cmds.getAttr(parent[0] + ".visibility"):
            result.append(parent[0])
    return list(dict.fromkeys(result))


def _point_segment_distance(point, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    squared = dx * dx + dy * dy
    if squared < 1e-9:
        return math.dist(point, a)
    t = max(0.0, min(1.0, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / squared))
    return math.hypot(point[0] - a[0] - t * dx, point[1] - a[1] - t * dy)


def _segment_hits_rectangle(a, b, corner_a, corner_b):
    xmin, xmax = sorted((corner_a[0], corner_b[0]))
    ymin, ymax = sorted((corner_a[1], corner_b[1]))
    dx, dy = b[0] - a[0], b[1] - a[1]
    low, high = 0.0, 1.0
    for p, q in ((-dx, a[0] - xmin), (dx, xmax - a[0]),
                 (-dy, a[1] - ymin), (dy, ymax - a[1])):
        if abs(p) < 1e-9:
            if q < 0:
                return False
            continue
        ratio = q / p
        if p < 0:
            low = max(low, ratio)
        else:
            high = min(high, ratio)
        if low > high:
            return False
    return True


def _double_click_limits():
    """Allow twice Qt's double-click interval and movement tolerance."""
    try:
        try:
            from PySide6.QtGui import QGuiApplication
        except ImportError:
            from PySide2.QtGui import QGuiApplication
        app = QGuiApplication.instance()
        if app is not None:
            hints = app.styleHints()
            return hints.mouseDoubleClickInterval() / 500.0, hints.mouseDoubleClickDistance() * 2
    except (ImportError, AttributeError, RuntimeError):
        pass
    return 0.8, 10


def _selected_mesh_edges(shape):
    """Read edge IDs independently of transform/shape component spelling."""
    selection = om.MGlobal.getActiveSelectionList()
    edges = set()
    for index in range(selection.length()):
        try:
            path, component = selection.getComponent(index)
        except RuntimeError:
            continue
        if component.isNull() or not component.hasFn(om.MFn.kMeshEdgeComponent):
            continue
        if path.node().hasFn(om.MFn.kTransform):
            path.extendToShape()
        if path.fullPathName() != shape:
            continue
        indexed = om.MFnSingleIndexedComponent(component)
        edges.update(range(indexed.getCompleteData()) if indexed.isComplete
                     else indexed.getElements())
    return frozenset(edges)


def _shift_click(component, anchor, *, context=None, button=1, clicked_at=None):
    """Toggle one edge or a Maya loop using selection before the first click."""
    global _LAST_SHIFT_CLICK
    mesh, suffix = component.rsplit(".e[", 1)
    edge = int(suffix[:-1])
    shape = _mesh_fn(mesh).dagPath().fullPathName()
    now = time.monotonic() if clicked_at is None else clicked_at
    interval, distance = _double_click_limits()
    previous = _LAST_SHIFT_CLICK
    if (button == 1 and previous is not None and
            previous["edge"] == (shape, edge) and previous["context"] == context and
            previous["button"] == button and 0 <= now - previous["time"] <= interval and
            math.dist(anchor, previous["anchor"]) <= distance):
        _LAST_SHIFT_CLICK = None
        loop = set(cmds.polySelect(shape, edgeLoopOrBorder=edge, noSelection=True) or [edge])
        components = [f"{shape}.e[{member}]" for member in sorted(loop)]
        if loop.issubset(previous["selection"]):
            cmds.select(components, deselect=True)
        else:
            cmds.select(components, add=True)
        return
    selected = _selected_mesh_edges(shape)
    cmds.select(component, toggle=True)
    _LAST_SHIFT_CLICK = {"edge": (shape, edge), "time": now, "anchor": anchor,
                         "context": context, "button": button, "selection": selected}


def _select_from_screen(anchor, end, *, context=None, button=1, clicked_at=None):
    global _LAST_SHIFT_CLICK
    view = omui.M3dView.active3dView()
    click = math.dist(anchor, end) < 3.0
    nearest = (20.0, None)
    matches = []
    for mesh in _edge_pick_meshes():
        try:
            fn = _mesh_fn(mesh)
        except (SlideError, RuntimeError):
            continue
        projected = {}
        for edge_id in range(fn.numEdges):
            a, b = fn.getEdgeVertices(edge_id)
            for vertex in (a, b):
                if vertex not in projected:
                    projected[vertex] = view.worldToView(fn.getPoint(vertex, om.MSpace.kWorld))
            screen_a, screen_b = projected[a], projected[b]
            if not screen_a[2] and not screen_b[2]:
                continue
            component = f"{mesh}.e[{edge_id}]"
            if click:
                distance = _point_segment_distance(anchor, screen_a[:2], screen_b[:2])
                if distance < nearest[0]:
                    nearest = (distance, component)
            elif _segment_hits_rectangle(screen_a[:2], screen_b[:2], anchor, end):
                matches.append(component)
    if click and nearest[1] is not None:
        _shift_click(nearest[1], anchor, context=context, button=button, clicked_at=clicked_at)
    else:
        _LAST_SHIFT_CLICK = None
        if matches:
            cmds.select(matches, add=True)


def _on_release():
    global _STATE
    if _STATE is None:
        return
    if "select_anchor" in _STATE:
        state = _STATE
        anchor = _STATE["select_anchor"]
        end = _STATE.get("select_end", anchor)
        _STATE = None
        try:
            _select_from_screen(anchor, end, context=state["context"],
                                button=state["select_button"], clicked_at=state["select_time"])
        except Exception as exc:
            cmds.warning(f"Slide for Maya: выделение ребра не удалось: {exc}")
        cmds.refresh(force=True, currentView=True)
        return
    _on_drag()
    if _STATE is None:
        return
    state = _STATE
    _STATE = None
    if state["live"] is None and state["multi_amount"] is None:
        return
    try:
        actual=_publish_session(state)
    except Exception as exc:
        cmds.warning(f"Slide for Maya: {exc}")
        return
    cmds.inViewMessage(
        amg=f"{'Slide Bevel' if state.get('bevel') else 'Clone + Slide'}: {actual:.3f}",
        pos="midCenterTop",
        fade=True,
    )


def _on_finalize(clear_selection_click=True,keep_session=False):
    global _STATE, _LAST_SHIFT_CLICK
    if clear_selection_click:
        _LAST_SHIFT_CLICK = None
    if _STATE is not None and _STATE.get("temp") is not None:
        # A native selection tool may already have selected something else
        # before this context receives its finalize callback. Only restore the
        # gesture's source selection when the preview itself is still selected.
        selection = om.MGlobal.getActiveSelectionList()
        hilite_names = om.MGlobal.getHiliteList().getSelectionStrings()
        temp_paths = cmds.ls(_STATE["temp"], long=True) or []
        preview_selected = False
        for index in range(selection.length()):
            try:
                selected_path = selection.getDagPath(index).fullPathName()
            except (RuntimeError, TypeError):
                continue
            if any(selected_path == path or selected_path.startswith(path + "|")
                   for path in temp_paths):
                preview_selected = True
                break
        try:
            _discard_preview(_STATE)
            if not preview_selected:
                hilite = om.MSelectionList()
                for node in hilite_names:
                    if cmds.objExists(node):
                        hilite.add(node)
                om.MGlobal.setHiliteList(hilite)
                om.MGlobal.setActiveSelectionList(selection)
        except Exception as exc:
            cmds.warning(f"Slide for Maya: {exc}")
    _STATE = None
    if not keep_session:_end_session()


def _end_session(*_):
    global _SESSION
    _SESSION=None
    for callback in _SESSION_CALLBACKS:om.MMessage.removeCallback(callback)
    _SESSION_CALLBACKS.clear()


def _session_selection_changed(*_):
    from PySide6 import QtCore
    QtCore.QTimer.singleShot(0,_check_session_selection)


def _check_session_selection():
    if (_SESSION is not None and _STATE is None and not _SESSION_UPDATING and
            edit_session.selection_key()!=_SESSION['result_selection']):
        previous=om.MSelectionList()
        for item in _SESSION['result_selection']:
            try:previous.add(item)
            except RuntimeError:pass
        current=om.MGlobal.getActiveSelectionList()
        if not current.length() or not previous.length():
            _end_session();return
        added=om.MSelectionList();added.merge(current);added.merge(previous,om.MSelectionList.kRemoveFromList)
        _end_session()
        if added.length():
            current.merge(previous,om.MSelectionList.kRemoveFromList)
            om.MGlobal.setActiveSelectionList(current)


def _session_history_changed(*_):
    global _DRAG_MODE,_BEVEL_DRAG_MODE,_DRAG_COUNT,_BEVEL_DRAG_COUNT,_DRAG_BIAS_PERCENT,_BEVEL_DRAG_BIAS_PERCENT
    if _SESSION is None:return
    node=_SESSION['result_node']
    if not cmds.objExists(node):_end_session();return
    info=json.loads(cmds.getAttr(node+'.slideState'))
    info['result_selection']=tuple(info['result_selection'])
    _SESSION.update(info)
    if _SESSION.get('bevel'):
        _BEVEL_DRAG_MODE=info['mode'];_BEVEL_DRAG_COUNT=info['count'];_BEVEL_DRAG_BIAS_PERCENT=info['bias_percent']
    else:
        _DRAG_MODE=info['mode'];_DRAG_COUNT=info['count'];_DRAG_BIAS_PERCENT=info['bias_percent']


def _publish_session(state):
    global _SESSION,_SESSION_UPDATING
    _SESSION_UPDATING=True
    try:
        actual=state['live'].actual
        live=state['live'];side=state['side'];guide_index=state.get('guide_index')
        if state['count']>1:edges=live.new_edges
        elif state.get('bevel'):edges=live._new_edges
        else:
            def pairs(edit):
                if edit._child_edits:return [pair for child in edit._child_edits for pair in pairs(child)]
                vertices=edit._new_vertices
                return [(vertices[i],vertices[(i+1)%len(vertices)]) for i in range(len(edit.plan.edge_ids))]
            fn=_mesh_fn(state['temp'])
            mapping={frozenset(fn.getEdgeVertices(edge)):edge for edge in range(fn.numEdges)}
            edges=[mapping[frozenset(pair)] for pair in pairs(live)]
        data=edit_session.snapshot(state['temp']);shading=edit_session.materials(state['temp'])
        _discard_preview(state)
        state.update(actual=actual,side=side,guide_index=guide_index)
        edit_session.publish(state,data,shading,edges)
        _SESSION=state
        if not _SESSION_CALLBACKS:
            _SESSION_CALLBACKS.append(om.MEventMessage.addEventCallback('SelectionChanged',_session_selection_changed))
            for event in ('Undo','Redo'):_SESSION_CALLBACKS.append(om.MEventMessage.addEventCallback(event,_session_history_changed))
        cmds.refresh(force=True,currentView=True)
        return actual
    finally:_SESSION_UPDATING=False


def _update_session_settings(bevel=False):
    global _SESSION_UPDATING
    _check_session_selection()
    if _SESSION is None or bool(_SESSION.get('bevel'))!=bevel:return
    state=edit_session.clone_state(_SESSION)
    state.update(mode=_BEVEL_DRAG_MODE if bevel else _DRAG_MODE,
                 count=_BEVEL_DRAG_COUNT if bevel else _DRAG_COUNT,
                 bias_percent=_BEVEL_DRAG_BIAS_PERCENT if bevel else _DRAG_BIAS_PERCENT)
    if all(state[key]==_SESSION[key] for key in ('mode','count','bias_percent')):return
    amount=_SESSION['actual']
    if state['mode']!=_SESSION['mode']:
        plans=state['bevel_plan'].runs if bevel else (state['plans'][_SESSION['side']],)
        width=min((distance for plan in plans for distance in _normal_distances(plan) if distance>_EPS),default=1.)
        amount=amount*width if state['mode']=='uniform' else amount/width
    _SESSION_UPDATING=True
    try:
        if state['count']>1:_update_multi_preview(state,amount,_SESSION['side'])
        else:
            _start_preview(state,_SESSION['side'])
            with _without_undo():state['live'].update(amount)
        _publish_session(state)
    except Exception as exc:
        if state.get('temp'):
            with _without_undo():_discard_preview(state)
        cmds.warning(f'Slide for Maya: {exc}')
    finally:_SESSION_UPDATING=False


@contextmanager
def _context_selection():
    """Keep the current selection through Maya's mode and tool callbacks."""
    bevel_ui = sys.modules.get("model_pack_bevel.ui")
    if bevel_ui is not None and bevel_ui._SESSION is not None:
        # Bevel changes topology on exit. Take the next tool's selection
        # snapshot after commit so it cannot restore pre-bevel edge numbers.
        bevel_ui._on_finalize()
    _on_finalize()
    selection = om.MGlobal.getActiveSelectionList()
    hilite = om.MGlobal.getHiliteList()
    component_mode = cmds.selectMode(query=True, component=True)
    try:
        yield
    finally:
        if component_mode:
            om.MGlobal.setHiliteList(hilite)
        # Mode/mask changes can recall Maya's previous component selection.
        # Use the API so restoring this snapshot adds no separate Undo step.
        om.MGlobal.setActiveSelectionList(selection)


def active():
    return cmds.currentCtx() == _CONTEXT


def active_bevel():
    return cmds.currentCtx() == _BEVEL_CONTEXT


def deactivate_bevel_drag():
    with _context_selection():
        if active_bevel():
            cmds.setToolTo("selectSuperContext")


def _on_bevel_press():
    _on_press(bevel=True)


def set_edge_count(count, *, bevel=False):
    """Update the current cut, or configure the next gesture."""
    global _DRAG_COUNT, _BEVEL_DRAG_COUNT
    if not isinstance(count, int) or not 1 <= count <= 16:
        raise SlideError("Количество рёбер должно быть от 1 до 16.")
    if bevel:
        _BEVEL_DRAG_COUNT = count
    else:
        _DRAG_COUNT = count
    _update_session_settings(bevel)


def set_edge_distribution(percent, *, bevel=False):
    """Change the spacing of the current or next set of cuts."""
    global _DRAG_BIAS_PERCENT, _BEVEL_DRAG_BIAS_PERCENT
    if not isinstance(percent, (int, float)) or not math.isfinite(percent) or not 0 <= percent <= 100:
        raise SlideError("Положение промежуточных рёбер должно быть от 0 до 100%.")
    if bevel:
        _BEVEL_DRAG_BIAS_PERCENT = percent
    else:
        _DRAG_BIAS_PERCENT = percent
    _update_session_settings(bevel)


def activate_bevel_drag(mode=None, count=None):
    """Activate the two-sided drag tool, including branching selections."""
    global _BEVEL_DRAG_MODE
    was_active=active_bevel()
    if mode is not None:
        if mode not in ("relative", "uniform"):
            raise SlideError("Режим должен быть relative или uniform.")
        _BEVEL_DRAG_MODE = mode
    if count is not None:
        set_edge_count(count, bevel=True)
    if was_active and (_STATE is None or _STATE.get('context')==_BEVEL_CONTEXT):
        if mode is not None:_update_session_settings(True)
        return _BEVEL_CONTEXT
    prefix = "import slide_for_maya.ui as slide_ui; slide_ui."
    callbacks = dict(
        pressCommand=prefix + "_on_bevel_press()",
        dragCommand=prefix + "_on_drag()",
        releaseCommand=prefix + "_on_release()",
        finalize=prefix + "_on_finalize()",
        undoMode="all",
    )
    with _context_selection():
        if not cmds.selectMode(query=True, component=True):
            cmds.selectMode(component=True)
        cmds.selectType(allComponents=False)
        cmds.selectType(edge=True)
        if not cmds.draggerContext(_BEVEL_CONTEXT, exists=True):
            cmds.draggerContext(_BEVEL_CONTEXT, cursor="crossHair", space="screen",
                                projection="viewPlane", **callbacks)
        else:
            cmds.draggerContext(_BEVEL_CONTEXT, edit=True, space="screen",
                                projection="viewPlane", **callbacks)
        cmds.setToolTo(_BEVEL_CONTEXT)
    return _BEVEL_CONTEXT


def deactivate_drag():
    with _context_selection():
        if active():
            cmds.setToolTo("selectSuperContext")


def activate_drag(mode=None, count=None):
    """Activate a lightweight screen-space drag context in Maya's viewport."""
    global _DRAG_MODE
    was_active=active()
    if mode is not None:
        if mode not in ("relative", "uniform"):
            raise SlideError("Режим должен быть relative или uniform.")
        _DRAG_MODE = mode
    if count is not None:
        set_edge_count(count)
    if was_active and (_STATE is None or _STATE.get('context')==_CONTEXT):
        if mode is not None:_update_session_settings(False)
        return _CONTEXT
    prefix = "import slide_for_maya.ui as slide_ui; slide_ui."
    callbacks = dict(
        pressCommand=prefix + "_on_press()",
        dragCommand=prefix + "_on_drag()",
        releaseCommand=prefix + "_on_release()",
        finalize=prefix + "_on_finalize()",
        undoMode="all",
    )
    with _context_selection():
        if not cmds.selectMode(query=True, component=True):
            cmds.selectMode(component=True)
        cmds.selectType(allComponents=False)
        cmds.selectType(edge=True)
        if not cmds.draggerContext(_CONTEXT, exists=True):
            cmds.draggerContext(_CONTEXT, cursor="crossHair", space="screen",
                                projection="viewPlane", **callbacks)
        else:
            cmds.draggerContext(_CONTEXT, edit=True, space="screen",
                                projection="viewPlane", **callbacks)
        cmds.setToolTo(_CONTEXT)
    return _CONTEXT
