"""Hard-edge creases and Catmull-Clark Smooth, using native Maya history."""
from contextlib import contextmanager
import math

from maya import cmds
from maya.api import OpenMaya as om

WEIGHT_OPTION = 'ModelPack_CreaseWeight'
LEVELS_OPTION = 'ModelPack_CreaseLevels'


def settings():
    weight = cmds.optionVar(query=WEIGHT_OPTION) if cmds.optionVar(exists=WEIGHT_OPTION) else 10.
    levels = cmds.optionVar(query=LEVELS_OPTION) if cmds.optionVar(exists=LEVELS_OPTION) else 2
    return max(0., min(10., float(weight))), max(1, min(4, int(levels)))


def _targets():
    """Explicit edges override automatic hard edges on their own mesh."""
    selection = om.MGlobal.getActiveSelectionList()
    result = {}
    identities = {}
    for index in range(selection.length()):
        try:
            path, component = selection.getComponent(index)
        except RuntimeError:
            continue
        paths = []
        if path.node().hasFn(om.MFn.kTransform):
            for shape in cmds.listRelatives(path.fullPathName(), shapes=True,
                                           noIntermediate=True, type='mesh', fullPath=True) or []:
                paths.append(om.MSelectionList().add(shape).getDagPath(0))
        elif path.node().hasFn(om.MFn.kMesh):
            paths.append(path)
        for candidate in paths:
            if om.MFnDagNode(candidate).isIntermediateObject:
                continue
            identity = om.MObjectHandle(candidate.node()).hashCode()
            shape = identities.setdefault(identity, candidate.fullPathName())
            result.setdefault(shape, None)
            if not component.isNull() and component.hasFn(om.MFn.kMeshEdgeComponent):
                edges = om.MFnSingleIndexedComponent(component)
                ids = (range(om.MFnMesh(candidate).numEdges) if edges.isComplete else edges.getElements())
                if result[shape] is None:
                    result[shape] = set()
                result[shape].update(ids)
    if not result:
        raise ValueError('Выделите меш или рёбра для Crease.')
    return result


def _hard(shape):
    fn = om.MFnMesh(om.MSelectionList().add(shape).getDagPath(0))
    return {edge for edge in range(fn.numEdges) if not fn.isEdgeSmooth(edge)}


def _components(targets):
    from model_pack_hard_edges import _components as components
    return components({shape: _hard(shape) if ids is None else ids
                       for shape, ids in targets.items()})


def _assign(targets, weight):
    count = 0
    # polyCrease accepts several component arguments but only modifies the
    # first mesh. Issue one native command per shape inside the shared undo.
    for shape, ids in targets.items():
        ids = _hard(shape) if ids is None else ids
        if ids:
            cmds.polyCrease(_components({shape: ids}), value=weight, createHistory=True)
            count += len(ids)
    return count


@contextmanager
def _operation(label):
    cmds.undoInfo(openChunk=True, chunkName=label)
    try:
        yield
    finally:
        cmds.undoInfo(closeChunk=True)


def _weight(value):
    value = settings()[0] if value is None else float(value)
    if not math.isfinite(value) or not 0. <= value <= 10.:
        raise ValueError('Вес Crease должен быть от 0 до 10.')
    return value


def _levels(value):
    value = settings()[1] if value is None else value
    if int(value) != value or not 1 <= value <= 4:
        raise ValueError('Число уровней Smooth должно быть от 1 до 4.')
    return int(value)


def _smooth(targets, levels):
    nodes = []
    for shape in targets:
        # Geometry uses authored crease weights, independently of normal splits.
        nodes.extend(cmds.polySmooth(shape, method=0, subdivisionType=2,
                                     divisions=levels, continuity=1., keepBorder=True,
                                     keepHardEdge=False, propagateEdgeHardness=True,
                                     constructionHistory=True) or [])
    return nodes


def select_hard():
    targets = _targets()
    components = _components(dict.fromkeys(targets))
    if not components:
        raise ValueError('На выделенных мешах нет хард-рёбер.')
    cmds.select(components, replace=True)
    return components


def assign(weight=None):
    targets = _targets()
    weight = _weight(weight)
    components = _components(targets)
    if not components:
        raise ValueError('Нет выбранных рёбер или хард-рёбер для Crease.')
    with _operation('Model Pack Crease'):
        return _assign(targets, weight)


def remove():
    targets = _targets()
    # Object selection clears all existing creases, including softened edges.
    targets = {shape: range(om.MFnMesh(om.MSelectionList().add(shape).getDagPath(0)).numEdges)
               if ids is None else ids for shape, ids in targets.items()}
    with _operation('Model Pack Remove Crease'):
        return _assign(targets, 0.)


def smooth(levels=None):
    targets = _targets()
    levels = _levels(levels)
    with _operation('Model Pack Smooth'):
        nodes = _smooth(targets, levels)
        cmds.select(list(targets), replace=True)
    return nodes


def apply(weight=None, levels=None):
    targets = _targets()
    weight, levels = _weight(weight), _levels(levels)
    components = _components(targets)
    with _operation('Model Pack Crease + Smooth'):
        if components:
            _assign(targets, weight)
        nodes = _smooth(targets, levels)
        cmds.select(list(targets), replace=True)
    return nodes


def run(toolbox=None, action=apply):
    if toolbox is not None:
        toolbox._prepare_tool(None)
    try:
        return action()
    except (RuntimeError, ValueError) as error:
        cmds.warning('Crease: ' + str(error))


def menu(toolbox):
    from PySide6 import QtWidgets
    result = QtWidgets.QMenu(toolbox)
    controls = QtWidgets.QWidget(result)
    layout = QtWidgets.QFormLayout(controls)
    layout.setContentsMargins(10, 8, 10, 8)
    weight, levels = settings()
    crease = QtWidgets.QDoubleSpinBox(controls)
    crease.setObjectName('modelPackCreaseWeight')
    crease.setRange(0., 10.); crease.setDecimals(2); crease.setSingleStep(.25); crease.setValue(weight)
    crease.setToolTip('0 — без удержания; 10 — жёсткая грань. Промежуточные веса дают мягкий переход.')
    crease.valueChanged.connect(lambda value: cmds.optionVar(floatValue=(WEIGHT_OPTION, value)))
    divisions = QtWidgets.QSpinBox(controls)
    divisions.setObjectName('modelPackCreaseLevels')
    divisions.setRange(1, 4); divisions.setValue(levels)
    divisions.valueChanged.connect(lambda value: cmds.optionVar(intValue=(LEVELS_OPTION, value)))
    layout.addRow('Crease', crease); layout.addRow('Smooth levels', divisions)
    widget_action = QtWidgets.QWidgetAction(result)
    widget_action.setDefaultWidget(controls); result.addAction(widget_action)
    for label, command in (('Crease + Smooth', apply), ('Assign Crease', assign),
                           ('Smooth Only', smooth), ('Select Hard Edges', select_hard),
                           ('Remove Crease', remove)):
        action = result.addAction(label)
        action.triggered.connect(lambda checked=False, fn=command: run(toolbox, fn))
    return result
