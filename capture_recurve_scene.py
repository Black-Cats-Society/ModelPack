"""Read-only capture of the selected Maya mesh and Recurve preview state."""
import json
import os
import sys
import maya.cmds as cmds
import maya.api.OpenMaya as om


def geometry(fn):
    counts,vertices=fn.getVertices()
    return {'points':[list(p)[:3] for p in fn.getPoints(om.MSpace.kObject)],
            'face_counts':list(counts),'face_vertices':list(vertices)}


def capture():
    selection=om.MGlobal.getActiveSelectionList()
    if not selection.length():
        raise RuntimeError('Select the affected mesh or its edges, then run this script again.')
    path=selection.getDagPath(0)
    if path.node().hasFn(om.MFn.kTransform):path.extendToShape()
    fn=om.MFnMesh(path)
    name=path.fullPathName()
    selected_edges=[]
    for item in range(selection.length()):
        try:
            selected_path,component=selection.getComponent(item)
            if selected_path.fullPathName()==name and component.apiType()==om.MFn.kMeshEdgeComponent:
                component_fn=om.MFnSingleIndexedComponent(component)
                selected_edges.extend(range(fn.numEdges) if component_fn.isComplete else component_fn.getElements())
        except RuntimeError:
            pass
    report={'maya_version':cmds.about(version=True),'mesh':name,
            'selected_edges':sorted(set(selected_edges)),
            'geometry':geometry(fn),'world_matrix':list(path.inclusiveMatrix()),
            'input_plugs':cmds.listConnections(name+'.inMesh',source=True,destination=False,plugs=True) or [],
            'vertex_tweaks':[],'modules':{},'preview':None}
    tweaks=om.MFnDependencyNode(path.node()).findPlug('pnts',False)
    for index in tweaks.getExistingArrayAttributeIndices():
        element=tweaks.elementByLogicalIndex(index)
        value=[element.child(axis).asDouble() for axis in range(3)]
        if any(value):report['vertex_tweaks'].append([index,value])
    for module_name in ('recurve','recurve_strip','recurve_bulk','recurve_uv','recurve_normals','recurve_controls','recurve_panel'):
        module=sys.modules.get(module_name)
        if module is not None:
            report['modules'][module_name]={'file':getattr(module,'__file__',None),
                                           'version':getattr(module,'VERSION',None)}
    module=sys.modules.get('recurve')
    if module is not None:
        report['connect_default']=getattr(module,'CONNECT_NEW',None)
        menu=getattr(module,'MENU',None)
        if menu is not None:
            try:
                report['menu']={'title':menu.windowTitle(),'connect':menu.connect.isChecked(),
                                'count':menu.count.value(),'feedback':menu.feedback.text()}
            except RuntimeError:
                pass
        session=getattr(module,'SESSION',None)
        if session is not None:
            report['preview']={'path':session.path,'count':session.count,'connect':session.connect,
                               'changed':session.changed,
                               'chains':getattr(session,'chains',[session.chain]),
                               'before':geometry(om.MFnMesh(session.before)),
                               'after':geometry(om.MFnMesh(session.after))}
            controls=getattr(session,'controls',None)
            if controls is not None:
                report['preview']['control_curves']={'points_per_curve':controls.number,
                                                    'deformed':controls.deformed,
                                                    'curve_uuids':[g['uuid'] for g in controls.guides]}
    target=os.path.join(os.path.dirname(os.path.abspath(__file__)),'Recurve_debug_scene.json')
    with open(target,'w',encoding='utf-8') as stream:
        json.dump(report,stream,ensure_ascii=False,indent=2)
    print('RECURVE DIAGNOSTICS SAVED: '+target)
    print('Mesh: %s; vertices: %d; faces: %d; selected edges: %d' %
          (name,fn.numVertices,fn.numPolygons,len(report['selected_edges'])))
    return target


if __name__=='__main__':
    capture()
