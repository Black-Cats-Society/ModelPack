"""Undoable cleanup of sketches and Model Pack construction history."""
import re
from maya import cmds
from maya.api import OpenMaya as om
from . import core, patterns


def owned(node):
    if cmds.objExists(node+'.'+patterns.OWNED):
        return bool(cmds.getAttr(node+'.'+patterns.OWNED))
    return cmds.objExists(node+'.'+patterns.TAG) or cmds.objExists(node+'.fdCutSpec')


def clear_curves():
    curves=[]
    for uuid,node in patterns.all_curves():
        # Earlier Line Sketch builds predate the ownership tag.
        legacy=(re.fullmatch(r'fdSketch\d+',node.rsplit('|',1)[-1]) and
                cmds.objExists(node+'.'+patterns.TARGET))
        if owned(node) or legacy:curves.append(node)
    dependents=set()
    for node in curves:
        shapes=cmds.listRelatives(node,shapes=True,type='nurbsCurve',fullPath=True) or [node]
        for shape in shapes:
            for downstream in cmds.listHistory(shape,future=True,allFuture=True) or []:
                if cmds.nodeType(downstream)!='mesh' or cmds.getAttr(downstream+'.intermediateObject'):continue
                dependents.update(cmds.ls(downstream,long=True) or [])
    # Deleting a curve used by a saved Floater/Bevel chain used to leave a
    # dangling evaluator which could crash native polygon nodes on scene open.
    for mesh in dependents:
        if cmds.polyEvaluate(mesh,face=True)<1:
            raise ValueError('Fast Draft: зависимая геометрия повреждена. Сначала восстановите её исходную кривую.')
    with core.transaction('Clear Sketches'):
        for mesh in sorted(dependents):
            if cmds.objExists(mesh):cmds.delete(mesh,constructionHistory=True)
        for node in curves:
            shapes=cmds.listRelatives(node,shapes=True,fullPath=True) or []
            children=cmds.listRelatives(node,children=True,type='transform',fullPath=True) or []
            # Keep any user-added siblings or children under the curve transform.
            if cmds.nodeType(node)=='transform' and (children or any(cmds.nodeType(s)!='nurbsCurve' for s in shapes)):
                cmds.delete([s for s in shapes if cmds.nodeType(s)=='nurbsCurve'])
            else:cmds.delete(node)
    patterns._LAST=None
    return len(curves)


def _history_nodes():
    types = set()
    for plugin in cmds.pluginInfo(query=True, listPlugins=True) or []:
        if cmds.pluginInfo(plugin, query=True, vendor=True) == 'Model Pack':
            types.update(cmds.pluginInfo(plugin, query=True, dependNode=True) or [])
    return set(cmds.ls(type=sorted(types)) or []) if types else set()


def _orphaned_history(root, model_pack_nodes):
    """Find discarded polygon history without following ownership messages."""
    pending = [root]
    visited = set()
    while pending:
        node = pending.pop()
        if node in visited:
            continue
        if cmds.referenceQuery(node, isNodeReferenced=True):
            return set()
        if node not in model_pack_nodes:
            inherited = cmds.nodeType(node, inherited=True) or []
            if not any(kind in inherited for kind in ('polyBase', 'polyModifier', 'deleteComponent')):
                return set()
        visited.add(node)
        dep = om.MFnDependencyNode(om.MSelectionList().add(node).getDependNode(0))
        for plug in dep.getConnections():
            for destination in plug.destinations():
                if not destination.attribute().hasFn(om.MFn.kMessageAttribute):
                    target = destination.node()
                    if target.hasFn(om.MFn.kDagNode):
                        return set()
                    pending.append(om.MFnDependencyNode(target).name())
    return visited


def clean_history():
    model_pack_nodes = _history_nodes()
    meshes = {}
    cleaned_transforms = set()
    proxies = []
    helpers = set()
    for node in cmds.ls(type='transform',long=True) or []:
        if cmds.referenceQuery(node, isNodeReferenced=True):
            continue
        shapes = cmds.listRelatives(node, shapes=True, type='mesh', noIntermediate=True, fullPath=True) or []
        if not shapes:
            continue
        draft_owned = owned(node)
        for attr in ('radius', 'height', 'sides', 'capType'):
            plug = node + '.' + attr
            if not cmds.objExists(plug) or not cmds.addAttr(plug, q=True, usedAsProxy=True):
                continue
            source = cmds.connectionInfo(plug, sourceFromDestination=True).split('.', 1)[0]
            if draft_owned or source in model_pack_nodes:
                proxies.append(plug)
        for shape in shapes:
            history = set(cmds.listHistory(shape) or [])
            if not draft_owned and not history.intersection(model_pack_nodes):
                continue
            mesh = om.MFnMesh(core.path_for(shape))
            meshes[shape] = mesh.numVertices, mesh.numPolygons
            cleaned_transforms.add(node)
            for upstream in history:
                if cmds.nodeType(upstream)!='mesh':continue
                for parent in cmds.listRelatives(upstream,parent=True,fullPath=True) or []:
                    if re.fullmatch(r'fd(Source|Box|Cylinder|Profile|Surface)\d+',parent.rsplit('|',1)[-1]):helpers.add(parent)
    with core.transaction('Clean History'):
        for plug in proxies:
            cmds.deleteAttr(plug)
        if meshes:
            cmds.delete(list(meshes), constructionHistory=True)
        for shape, counts in meshes.items():
            mesh = om.MFnMesh(core.path_for(shape))
            if (mesh.numVertices, mesh.numPolygons) != counts:
                raise ValueError('Очистка истории изменила геометрию ' + shape)
        remaining = _history_nodes()
        discarded = set()
        for node in sorted(remaining):
            if node not in discarded:
                discarded.update(_orphaned_history(node, remaining))
        if discarded:
            cmds.delete(sorted(discarded))
        for node in sorted(helpers,key=len,reverse=True):
            if node not in cleaned_transforms and cmds.objExists(node) and not cmds.listRelatives(node,children=True):cmds.delete(node)
    return len(cleaned_transforms)
