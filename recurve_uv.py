"""Write UV sets to mesh data without calling node-only APIs on that data."""
import maya.api.OpenMaya as om


def assign_uv_sets(data,sets,current=None):
    """sets maps names to (U values, V values, per-face counts, corner IDs)."""
    if not sets:return data
    out=om.MFnMesh(data)
    if tuple(sets)==tuple(out.getUVSetNames()):
        for name,(us,vs,counts,ids) in sets.items():
            if len(us):
                out.setUVs(us,vs,name)
                out.assignUVs(counts,ids,name)
        return data
    # Maya's createUVSet/renameUVSet require a mesh node. Use an isolated,
    # hidden staging mesh; copy its completed geometry back to detached data.
    # The user's mesh, its UV-set attributes and its history are not edited.
    selection=om.MGlobal.getActiveSelectionList()
    modifier=om.MDagModifier()
    transform=modifier.createNode('transform')
    modifier.renameNode(transform,'_recurveUVBuild#')
    modifier.newPlugValueBool(om.MFnDependencyNode(transform).findPlug('visibility',False),False)
    modifier.doIt()
    try:
        staged=om.MFnMesh();staged.copy(data,transform)
        names=list(sets)
        existing=list(staged.getUVSetNames())
        # The source may contain map1 after a custom first set (UVMap, map1).
        # Rename the empty initial slot to the first source name even when
        # map1 is needed again later, then create the remaining slots in order.
        if len(existing)==1 and existing[0]!=names[0]:
            staged.renameUVSet(existing[0],names[0])
        for name,(us,vs,counts,ids) in sets.items():
            if name not in staged.getUVSetNames():staged.createUVSet(name)
            if len(us):
                staged.setUVs(us,vs,name)
                staged.assignUVs(counts,ids,name)
        if current in sets:staged.setCurrentUVSetName(current)
        result=om.MFnMeshData().create()
        om.MFnMesh().copy(staged.object(),result)
    finally:
        modifier.undoIt()
        om.MGlobal.setActiveSelectionList(selection)
    actual=tuple(om.MFnMesh(result).getUVSetNames())
    if actual!=tuple(sets):
        raise RuntimeError('Recurve could not preserve the UV set names: expected %r, got %r.'%(tuple(sets),actual))
    return result
