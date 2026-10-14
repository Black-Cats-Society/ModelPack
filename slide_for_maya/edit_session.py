"""Keep one completed cut editable until selection or context changes."""
import json
from maya import cmds
from maya.api import OpenMaya as om
from .core import _mesh_fn

PENDING={}
NODE='modelPackSlideResult'
COMMAND='modelPackSetSlideResult'


def snapshot(mesh):
    fn=_mesh_fn(mesh)
    source=om.MFnDependencyNode(fn.object()).findPlug('outMesh',False).asMObject()
    data=om.MFnMeshData().create();om.MFnMesh().copy(source,data)
    om.MFnMeshData(data).copyObjectGroups(source)
    return data


def preview(state):
    """Create a disposable mesh from the source before this operation."""
    data=state['base_mesh'];mesh=state['mesh']
    temp=cmds.createNode('transform',name='modelPackSlidePreview#')
    sel=om.MSelectionList();sel.add(temp)
    om.MFnMesh().copy(data,sel.getDependNode(0))
    cmds.xform(temp,worldSpace=True,matrix=cmds.xform(mesh,q=True,worldSpace=True,matrix=True))
    shaders,assignments=state['base_materials']
    assign_materials(temp,shaders,assignments)
    return temp


def materials(mesh):
    fn=_mesh_fn(mesh);shaders,ids=fn.getConnectedShaders(fn.dagPath().instanceNumber())
    return [om.MFnDependencyNode(shader).name() for shader in shaders],list(ids)


def assign_materials(mesh,shaders,ids):
    if not shaders:return
    cmds.sets(mesh,e=True,forceElement=shaders[0])
    for index,shader in enumerate(shaders[1:],1):
        faces=[f'{mesh}.f[{f}]' for f,assigned in enumerate(ids) if assigned==index]
        if faces:cmds.sets(faces,e=True,forceElement=shader)


def publish(state,data,shading,edges):
    from model_pack_modifiers import ensure_plugin,PLUGIN
    ensure_plugin(PLUGIN)
    mesh=state['mesh'];node=state.get('result_node')
    cmds.undoInfo(openChunk=True,chunkName='Slide Bevel' if state.get('bevel') else 'Clone Slide')
    try:
        if not node:
            identity=cmds.polyMoveVertex(mesh,translate=(0,0,0),ch=True)[0]
            destination=cmds.listConnections(identity+'.output',s=False,d=True,plugs=True)[0]
            node=cmds.createNode(NODE)
            cmds.addAttr(node,ln='slideState',dt='string')
            cmds.connectAttr(identity+'.output',node+'.inputMesh')
            cmds.connectAttr(node+'.outputMesh',destination,force=True)
        token=str(id(data));PENDING[token]=data
        try:getattr(cmds,COMMAND)(node,token)
        finally:PENDING.pop(token,None)
        assign_materials(mesh,*shading)
        cmds.select([f'{mesh}.e[{edge}]' for edge in edges],r=True)
        info={key:state.get(key) for key in ('actual','mode','count','bias_percent','side','guide_index','direction')}
        info['result_selection']=selection_key()
        cmds.setAttr(node+'.slideState',json.dumps(info),type='string')
    finally:cmds.undoInfo(closeChunk=True)
    state['result_node']=node
    state['result_selection']=selection_key()


def selection_key():
    return tuple(sorted(om.MGlobal.getActiveSelectionList().getSelectionStrings()))


def clone_state(session):
    state={key:value for key,value in session.items() if key not in ('live','temp','guides','anchor')}
    state.update(live=None,temp=None,multi_amount=None,side=None,guide_index=None)
    state['editing']=True
    state['base_amount']=session['actual']
    state['base_side']=session['side']
    return state
