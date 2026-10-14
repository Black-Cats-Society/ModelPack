"""Editable history bindings for Mirror Cut, Mirror Dup and Reset Tube."""
import gc,importlib,json
from pathlib import Path
from maya import cmds
from maya.api import OpenMaya as om
from slide_for_maya.core import _mesh_fn
from model_pack_paths import plugin_path

PLUGIN=plugin_path(Path(__file__).with_name('model_pack_modifier_nodes.py'))
MIRROR_PLUGIN=plugin_path(Path(__file__).with_name('model_pack_fast_mirror_node.py'))


def ensure_plugin(required=None):
    for plugin in ((required,) if required else (PLUGIN,MIRROR_PLUGIN)):
        if not cmds.pluginInfo(plugin,q=True,loaded=True):cmds.loadPlugin(plugin,quiet=True)
        if plugin==PLUGIN and 'modelPackSlideResult' not in (cmds.pluginInfo(plugin,q=True,dependNode=True) or []):
            module=importlib.import_module('model_pack_modifier_nodes')
            fn=om.MFnPlugin(om.MFnPlugin.findPlugin(Path(plugin).stem))
            cls=module.SlideResultNode
            fn.registerNode(cls.name,cls.id,cls.creator,cls.initialize)
            fn.registerCommand('modelPackSetSlideResult',module.SetSlideResult.creator)
        if plugin==PLUGIN and 'modelPackMirrorEdit' not in (cmds.pluginInfo(plugin,q=True,command=True) or []):
            from model_pack_mirror_session import MirrorEditCommand
            fn=om.MFnPlugin(om.MFnPlugin.findPlugin(Path(plugin).stem))
            fn.registerCommand('modelPackMirrorEdit',MirrorEditCommand.creator)
        if plugin==PLUGIN and 'modelPackApplyMirror' not in (cmds.pluginInfo(plugin,q=True,command=True) or []):
            from model_pack_mirror_session import MirrorApplyCommand
            fn=om.MFnPlugin(om.MFnPlugin.findPlugin(Path(plugin).stem))
            fn.registerCommand('modelPackApplyMirror',MirrorApplyCommand.creator)
        cmds.pluginInfo(plugin,e=True,autoload=True)
    if not cmds.about(batch=True):cmds.pluginInfo(savePluginPrefs=True)


def refresh_plugin():
    if cmds.pluginInfo(PLUGIN,q=True,loaded=True):
        module=importlib.import_module('model_pack_modifier_nodes');importlib.reload(module)
        for kind,implementation in (('modelPackResetTube',module.ResetTubeNode),('modelPackSlideResult',module.SlideResultNode)):
            if kind not in (cmds.pluginInfo(PLUGIN,q=True,dependNode=True) or []):continue
            for node in cmds.ls(type=kind) or []:
                sel=om.MSelectionList();sel.add(node)
                cls=type(om.MFnDependencyNode(sel.getDependNode(0)).userNode())
                cls.compute=implementation.compute;cmds.dgdirty(node)
    if cmds.pluginInfo(MIRROR_PLUGIN,q=True,loaded=True):
        from maya import OpenMayaMPx as legacy_mpx
        mirror=importlib.import_module('model_pack_fast_mirror_node');importlib.reload(mirror)
        backend=importlib.import_module('model_pack_legacy_mesh')
        # Maya can return an API 1 base wrapper from userNode(), rather than
        # the Python subclass held by the plugin's registered creator.
        # Recover those classes too, including plugins predating the registry.
        nodes=cmds.ls(type='modelPackMirrorCut') or []
        backend.REGISTERED_NODE_CLASSES.discard(legacy_mpx.MPxNode)
        for candidate in gc.get_objects():
            if type(candidate) is type and candidate.__name__=='MirrorNode' and issubclass(candidate,legacy_mpx.MPxNode):
                if hasattr(candidate,'outputMesh'):backend.REGISTERED_NODE_CLASSES.add(candidate)
        for cls in backend.REGISTERED_NODE_CLASSES:
            cls.compute=mirror.MirrorNode.compute;cls.setDependentsDirty=mirror.MirrorNode.setDependentsDirty
        for node in nodes:cmds.dgdirty(node)


def targets():
    result=[]
    for item in cmds.ls(sl=True,objectsOnly=True,long=True) or []:
        try:
            path=_mesh_fn(item).dagPath();path.pop();parent=path.fullPathName()
        except (ValueError,RuntimeError):continue
        if parent not in result:result.append(parent)
    if not result:raise ValueError('Выделите polygon mesh или его компоненты.')
    return result


def tag(node,kind,source,target):
    cmds.addAttr(node,ln='modelPackTool',dt='string')
    cmds.setAttr(node+'.modelPackTool',kind,type='string')
    for name,obj in (('modelPackSource',source),('modelPackTarget',target)):
        cmds.addAttr(node,ln=name,at='message');cmds.connectAttr(obj+'.message',node+'.'+name)


def related(node,name='modelPackTarget'):
    connections=cmds.listConnections(node+'.'+name,s=True,d=False) or []
    return (cmds.ls(connections,long=True) or [None])[0]


def find(kind,mesh):
    candidates=(cmds.listHistory(mesh) or [])+(cmds.listConnections(mesh+'.message',s=False,d=True) or [])
    return next((node for node in candidates if cmds.attributeQuery('modelPackTool',node=node,exists=True)
                 and cmds.getAttr(node+'.modelPackTool')==kind),None)


def mirror_cut(mesh,axis,space,direction,threshold,pivot):
    ensure_plugin(MIRROR_PLUGIN)
    node=find('mirror_cut',mesh)
    if node is None:
        # A native identity modifier moves pre-existing tweaks into history
        # and creates the original shape without baking earlier operations.
        identity=cmds.polyMoveVertex(mesh,translate=(0,0,0),ch=True)[0]
        destination=cmds.listConnections(identity+'.output',s=False,d=True,plugs=True)[0]
        node=cmds.createNode('modelPackMirrorCut')
        cmds.connectAttr(identity+'.output',node+'.inputMesh')
        cmds.connectAttr(node+'.outputMesh',destination,force=True)
        tag(node,'mirror_cut',mesh,mesh)
    if cmds.nodeType(node)=='polyMirror':
        # Previously saved native histories remain editable.
        for attr,value in (('axis',axis),('axisDirection',direction),('mirrorAxis',2 if space==0 else 1),('mergeThreshold',threshold)):
            if cmds.getAttr(node+'.'+attr)!=value:cmds.setAttr(node+'.'+attr,value)
    else:
        if cmds.objExists(node+'.modelPackPlaneEnabled') and cmds.getAttr(node+'.space')!=space:
            cmds.setAttr(node+'.modelPackPlaneEnabled',False)
        for attr,value in (('axis',axis),('direction',direction),('space',space),('threshold',threshold)):
            if cmds.getAttr(node+'.'+attr)!=value:cmds.setAttr(node+'.'+attr,value)
        destination=node+'.meshMatrix';incoming=cmds.listConnections(destination,s=True,d=False,plugs=True) or []
        if space==0:
            source=mesh+'.worldMatrix[0]'
            if not cmds.isConnected(source,destination):cmds.connectAttr(source,destination,force=True)
        else:
            # rotateAxis stores the orientation baked by Maya's pivot editor.
            # Coordinates in this frame follow the object's rotation axes.
            frames=cmds.listConnections(node+'.localFrame',s=True,d=False) if cmds.attributeQuery('localFrame',node=node,exists=True) else []
            if frames:frame=frames[0]
            else:
                frame=cmds.createNode('composeMatrix',name='modelPackMirrorFrame#')
                cmds.addAttr(node,ln='localFrame',at='message');cmds.connectAttr(frame+'.message',node+'.localFrame')
            if not cmds.isConnected(mesh+'.rotateAxis',frame+'.inputRotate'):
                cmds.connectAttr(mesh+'.rotateAxis',frame+'.inputRotate',force=True)
            for source,target in (('scale','inputScale'),('shear','inputShear')):
                if not cmds.isConnected(mesh+'.'+source,frame+'.'+target):cmds.connectAttr(mesh+'.'+source,frame+'.'+target,force=True)
            if not cmds.attributeQuery('pivotFrameMatrix',node=node,exists=True):
                cmds.addAttr(node,ln='pivotFrameMatrix',dt='matrix',hidden=True)
            rotations=cmds.listConnections(node+'.pivotLocalFrame',s=True,d=False) if cmds.attributeQuery('pivotLocalFrame',node=node,exists=True) else []
            if not rotations:rotations=cmds.listConnections(node+'.pivotFrameMatrix',s=True,d=False,type='composeMatrix') or []
            if rotations:rotation=rotations[0]
            else:
                rotation=cmds.createNode('composeMatrix',name='modelPackMirrorPivotFrame#')
            if not cmds.attributeQuery('pivotLocalFrame',node=node,exists=True):
                cmds.addAttr(node,ln='pivotLocalFrame',at='message')
            if not cmds.isConnected(rotation+'.message',node+'.pivotLocalFrame'):
                cmds.connectAttr(rotation+'.message',node+'.pivotLocalFrame',force=True)
            if not cmds.isConnected(mesh+'.rotateAxis',rotation+'.inputRotate'):
                cmds.connectAttr(mesh+'.rotateAxis',rotation+'.inputRotate')
            if not cmds.isConnected(rotation+'.outputMatrix',node+'.pivotFrameMatrix'):
                cmds.connectAttr(rotation+'.outputMatrix',node+'.pivotFrameMatrix',force=True)
            for name in ('scalePivot','scalePivotTranslate'):
                if not cmds.attributeQuery(name,node=node,exists=True):
                    cmds.addAttr(node,ln=name,at='double3',hidden=True)
                    for axis_name in 'XYZ':cmds.addAttr(node,ln=name+axis_name,at='double',parent=name,hidden=True)
                for axis_name in 'XYZ':
                    source=mesh+'.'+name+axis_name;target=node+'.'+name+axis_name
                    if not cmds.isConnected(source,target,ignoreUnitConversion=True):cmds.connectAttr(source,target,force=True)
            if not cmds.isConnected(frame+'.outputMatrix',destination):
                cmds.connectAttr(frame+'.outputMatrix',destination,force=True)
    destination=node+'.pivot'
    if space and cmds.nodeType(node)=='modelPackMirrorCut':
        if cmds.isConnected(mesh+'.rotatePivot',destination):cmds.disconnectAttr(mesh+'.rotatePivot',destination)
        for axis_name in 'XYZ':
            source=mesh+'.rotatePivot'+axis_name;target=destination+axis_name
            if not cmds.isConnected(source,target,ignoreUnitConversion=True):cmds.connectAttr(source,target,force=True)
    else:
        for target in [destination]+[destination+axis_name for axis_name in 'XYZ']:
            source=cmds.connectionInfo(target,sourceFromDestination=True)
            if source:cmds.disconnectAttr(source,target)
        if tuple(cmds.getAttr(destination)[0])!=tuple(pivot):cmds.setAttr(destination,*pivot,type='double3')
    return node


def freeze_mirror_frame(nodes):
    """Commit the current plane so pivot/object edits cannot keep moving it."""
    for node in nodes:
        if not cmds.objExists(node) or cmds.nodeType(node)!='modelPackMirrorCut':continue
        for attr in ('pivot','scalePivot','scalePivotTranslate'):
            target=node+'.'+attr
            if not cmds.objExists(target) or not cmds.connectionInfo(target,isExactDestination=True):continue
            source=cmds.connectionInfo(target,sourceFromDestination=True);value=cmds.getAttr(target)[0]
            cmds.disconnectAttr(source,target);cmds.setAttr(target,*value,type='double3')
        for attr in ('meshMatrix','pivotFrameMatrix','pivotX','pivotY','pivotZ',
                     'scalePivotX','scalePivotY','scalePivotZ',
                     'scalePivotTranslateX','scalePivotTranslateY','scalePivotTranslateZ'):
            target=node+'.'+attr
            if not cmds.objExists(target):continue
            source=cmds.connectionInfo(target,sourceFromDestination=True)
            if not source:continue
            value=cmds.getAttr(target)
            cmds.disconnectAttr(source,target)
            if attr.endswith('Matrix'):cmds.setAttr(target,*value,type='matrix')
            else:cmds.setAttr(target,value)


def apply_mirrors(nodes):
    """Bake reflected results and remove their live construction links."""
    results=[];helpers=set()
    for node in nodes:
        if not cmds.objExists(node):continue
        target=related(node)
        if target is None:continue
        kind=cmds.getAttr(node+'.modelPackTool')
        if kind=='mirror_cut':
            for attr in ('localFrame','pivotLocalFrame'):
                if cmds.objExists(node+'.'+attr):
                    helpers.update(cmds.listConnections(node+'.'+attr,s=True,d=False) or [])
            helpers.update(cmds.listConnections(node,s=True,d=False,type='unitConversion') or [])
        elif kind=='mirror_dup':
            # Keep the exact effective transform, including the copy's edits.
            plug=target+'.offsetParentMatrix';matrix=cmds.getAttr(plug)
            source=cmds.connectionInfo(plug,sourceFromDestination=True)
            if source:cmds.disconnectAttr(source,plug)
            cmds.setAttr(plug,*matrix,type='matrix');helpers.add(node)
        else:continue
        _mesh_fn(target).getPoints()
        if target not in results:results.append(target)
    if results:cmds.delete(results,constructionHistory=True)
    for helper in helpers:
        if cmds.objExists(helper) and not cmds.listConnections(helper,s=False,d=True):
            cmds.delete(helper)
    return results


def mirror_duplicate(mesh,axis,space):
    node=find('mirror_dup',mesh)
    if node is None:
        parent=cmds.listRelatives(mesh,parent=True,fullPath=True) or []
        dup=cmds.createNode('transform',name=mesh.split('|')[-1]+'_Mirrored#',**({'parent':parent[0]} if parent else {}))
        source=_mesh_fn(mesh);shape=cmds.createNode('mesh',parent=dup)
        cmds.connectAttr(source.fullPathName()+'.outMesh',shape+'.inMesh')
        node=cmds.createNode('multMatrix',name='modelPackMirrorDup#')
        cmds.connectAttr(node+'.matrixSum',dup+'.offsetParentMatrix')
        if parent:cmds.connectAttr(parent[0]+'.worldInverseMatrix[0]',node+'.matrixIn[2]')
        tag(node,'mirror_dup',mesh,dup)
        for name,value in (('axis',axis),('space',space)):
            cmds.addAttr(node,ln='modelPack'+name.title(),at='long');cmds.setAttr(node+'.modelPack'+name.title(),value)
        shading=cmds.listConnections(source.fullPathName(),type='shadingEngine') or ['initialShadingGroup']
        cmds.sets(dup,e=True,forceElement=shading[0])
        shaders,assignments=source.getConnectedShaders(source.dagPath().instanceNumber())
        for shader_id,shader in enumerate(shaders):
            faces=[f for f,index in enumerate(assignments) if index==shader_id]
            if faces:cmds.sets([dup+f'.f[{f}]' for f in faces],e=True,forceElement=om.MFnDependencyNode(shader).name())
    else:mesh=related(node,'modelPackSource')
    from model_pack_mirror_plane import active,dup_connections,frame
    if cmds.getAttr(node+'.modelPackSpace')!=space and cmds.objExists(node+'.modelPackPlaneEnabled'):
        cmds.setAttr(node+'.modelPackPlaneEnabled',False)
    if active(node):
        cmds.setAttr(node+'.modelPackAxis',axis);cmds.setAttr(node+'.modelPackSpace',space)
        dup_connections(node,frame(node));return node
    source=mesh+'.worldMatrix[0]'
    # Disconnect the former order before changing World / Local.
    for index in (0,1):
        destination=node+f'.matrixIn[{index}]'
        incoming=cmds.listConnections(destination,s=True,d=False,plugs=True) or []
        for plug in incoming:cmds.disconnectAttr(plug,destination)
    matrix=list(om.MMatrix());matrix[axis*4+axis]=-1
    if space:
        pivot=cmds.xform(mesh,q=True,objectSpace=True,rotatePivot=True)
        matrix[12+axis]=2*pivot[axis]
    cmds.setAttr(node+f'.matrixIn[{0 if space else 1}]',*matrix,type='matrix')
    cmds.connectAttr(source,node+f'.matrixIn[{1 if space else 0}]')
    cmds.setAttr(node+'.modelPackAxis',axis);cmds.setAttr(node+'.modelPackSpace',space)
    return node


def validate_reset_tube(mesh,seam):
    from tube_resection import _MeshData,_order_seam,_extract_rings
    md=_MeshData(_mesh_fn(mesh).dagPath())
    rail,closed=_order_seam(md,seam);_extract_rings(md,rail,closed)


def reset_tube(mesh,seam,sides):
    ensure_plugin(PLUGIN);source=_mesh_fn(mesh);node=find('reset_tube',mesh)
    if node is None:
        validate_reset_tube(mesh,seam)
        result=cmds.createNode('transform',name=mesh.split('|')[-1]+'_resect#')
        shape=cmds.createNode('mesh',parent=result)
        node=cmds.createNode('modelPackResetTube')
        cmds.setAttr(node+'.seamEdges',json.dumps(list(seam)),type='string')
        cmds.setAttr(node+'.sides',sides)
        cmds.connectAttr(source.fullPathName()+'.outMesh',node+'.inputMesh')
        cmds.connectAttr(mesh+'.worldMatrix[0]',node+'.meshMatrix')
        cmds.connectAttr(node+'.outputMesh',shape+'.inMesh')
        tag(node,'reset_tube',mesh,result)
        shading=cmds.listConnections(source.fullPathName(),type='shadingEngine') or ['initialShadingGroup']
        cmds.sets(result,e=True,forceElement=shading[0])
    else:cmds.setAttr(node+'.sides',sides)
    # Evaluate now so invalid source topology is reported inside the action.
    _mesh_fn(related(node)).getPoints()
    return node
