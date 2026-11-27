"""Scene-owned Control Curve bindings, independent of preview/UI lifetimes."""
import json
from pathlib import Path
import numpy as np
from maya import cmds
from maya.api import OpenMaya as om
from recurve_controls import curve_data,parameters,dag_path,lateral_weights,DEFAULT_FALLOFF

NODE_TYPE='modelPackControlCurve'
PLUGIN=Path(__file__).with_name('model_pack_curve_nodes.py')


def topology(fn):
    return [list(values) for values in fn.getVertices()]


def definition(session):
    base=om.MFnMesh(session.control_base);points=base.getPoints()
    result=dict(version=1,topology=topology(base),rows=session.control_ids,
                parameters=[parameters([tuple(points[v])[:3] for v in row]) for row in session.control_ids],
                align=session.align_to_curve,baselines=[],region=None,offsets=None)
    for guide in session.controls.guides:
        fn=guide['fn']
        result['baselines'].append(dict(cvs=[tuple(p)[:3] for p in fn.cvPositions()],
            knots=list(fn.knots()),degree=fn.degree,closed=guide['periodic']))
    if session.face_region is not None:
        region=session.face_region
        result['region']={name:(getattr(region,name).tolist() if isinstance(getattr(region,name),np.ndarray)
                                else list(getattr(region,name))) for name in
            ('vertices','boundary','interior','diagonal','ii','ij','iw','bi','bj','bw')}
    if session.face_region is None:
        result['lateral'] = session.controls.influence_binding(session.control_base, session.control_ids)
        result['falloff'] = session.influence_falloff
        result['smooth'] = session.influence_smooth
        result['influence'] = dict(distance=session.influence_distance, edges=session.influence_edges,
                                   by_edges=session.influence_by_edges)
    return result


def prepare(text):
    state=json.loads(text)
    if state['version']!=1:raise ValueError('Unsupported saved Control Curve binding.')
    state['curves']=[curve_data(b['cvs'],b['knots'],b['degree'],b['closed']) for b in state['baselines']]
    if state['offsets'] is not None:state['offsets']=np.asarray(state['offsets'],dtype=float)
    if state['region'] is not None:
        from recurve_faces import FaceRegion
        region=FaceRegion.__new__(FaceRegion)
        for name,value in state['region'].items():
            setattr(region,name,np.asarray(value,dtype=np.int32 if name in ('interior','ii','ij','bi','bj') else float)
                    if name not in ('vertices','boundary') else value)
        region.previous=np.zeros((len(region.interior),3));state['region']=region
    return state


def upstream_aligned(node, expected_topology):
    """Find fully aligned live guides on the consecutive upstream bindings."""
    protected = {}
    source = node.findPlug('inputMesh', False).source()
    while not source.isNull:
        upstream = om.MFnDependencyNode(source.node())
        if upstream.typeName != NODE_TYPE:
            break
        state = json.loads(upstream.findPlug('binding', False).asString())
        if (state.get('topology') == expected_topology and not state.get('region')
                and upstream.findPlug('envelope', False).asDouble() >= 1.):
            curves = upstream.findPlug('controlCurve', False)
            for index, row in enumerate(state['rows']):
                settings = state.get('guides', {}).get(str(index), {})
                if settings.get('align', state['align']) and curves.elementByLogicalIndex(index).isDestination:
                    curve_data = curves.elementByLogicalIndex(index).asMObject()
                    if curve_data.isNull():
                        continue
                    curve = om.MFnNurbsCurve(curve_data)
                    lo, hi = curve.knotDomain
                    matrix = (om.MFnMatrixData(upstream.findPlug('curveMatrix', False)
                              .elementByLogicalIndex(index).asMObject()).matrix()
                              * om.MFnMatrixData(upstream.findPlug('meshInverseMatrix', False)
                                                .asMObject()).matrix())
                    for vertex, t in zip(row, state['parameters'][index]):
                        protected[vertex] = curve.getPointAtParam(lo + t * (hi - lo)) * matrix
        source = upstream.findPlug('inputMesh', False).source()
    return protected


def blend_overlap(source, state, curves, matrices, envelope, node):
    """Evaluate consecutive edge bindings against their shared input."""
    layers = [(state, curves, matrices, envelope)]
    base = source
    current = node
    while True:
        plug = current.findPlug('inputMesh', False).source()
        if plug.isNull:
            break
        upstream = om.MFnDependencyNode(plug.node())
        if upstream.typeName != NODE_TYPE:
            break
        binding = prepare(upstream.findPlug('binding', False).asString())
        if binding['region'] is not None or binding['topology'] != state['topology']:
            break
        upstream_input = upstream.findPlug('inputMesh', False).asMObject()
        if upstream_input.isNull() or topology(om.MFnMesh(upstream_input)) != state['topology']:
            break
        inverse = om.MFnMatrixData(upstream.findPlug('meshInverseMatrix', False).asMObject()).matrix()
        inputs = upstream.findPlug('controlCurve', False)
        transforms = upstream.findPlug('curveMatrix', False)
        guides, relative = [], []
        for index in range(len(binding['rows'])):
            guide = inputs.elementByLogicalIndex(index)
            guides.append(guide.asMObject() if guide.isDestination else None)
            matrix = (om.MFnMatrixData(transforms.elementByLogicalIndex(index).asMObject()).matrix()
                      if guide.isDestination else om.MMatrix())
            relative.append(matrix * inverse)
        layers.append((binding, guides, relative, upstream.findPlug('envelope', False).asDouble()))
        base, current = upstream_input, upstream

    original = om.MFnMesh(base).getPoints()
    points = om.MPointArray(original)
    offsets = layers[-1][0]['offsets']
    if offsets is not None:
        for vertex, offset in enumerate(offsets):
            points[vertex] += om.MVector(*offset)
    total = np.zeros((len(points), 3))
    weights = np.zeros(len(points))
    full = np.zeros(len(points), dtype=int)
    contributors = np.zeros(len(points), dtype=int)
    pinned = {}
    zero_span_vectors = {}
    bridges = []
    for binding, guides, relative, strength in reversed(layers):
        if strength <= 0:
            continue
        for index, (row, samples, (_, baseline)) in enumerate(zip(
                binding['rows'], binding['parameters'], binding['curves'])):
            guide = guides[index]
            if guide is None or guide.isNull():
                continue
            curve = om.MFnNurbsCurve(guide)
            lo, hi = curve.knotDomain
            start, end = baseline.knotDomain
            settings = binding.get('guides', {}).get(str(index), {})
            align = settings.get('align', binding['align'])
            influence = settings.get('influence', binding.get('influence', {}))
            full_span = influence.get('mode') == 2 and influence.get('percent') == 100
            vectors = {}
            ids = row[:-1] if row[0] == row[-1] else row
            for vertex, t in zip(ids, samples):
                target = curve.getPointAtParam(lo + t * (hi - lo)) * relative[index]
                origin = points[vertex] if align else baseline.getPointAtParam(start + t * (end - start))
                vectors[vertex] = np.asarray(tuple(target - origin))
                if align and strength >= 1.:
                    pinned.setdefault(vertex, target)
            if influence.get('mode') == 2 and influence.get('percent') == 0:
                for vertex, value in vectors.items():
                    zero_span_vectors.setdefault(vertex, value * strength)
            contributions = {vertex: (value, 1.) for vertex, value in vectors.items()}
            opposite = dict(influence.get('bridge', ())) if full_span and strength >= 1. else {}
            for vertex, seed, weight in lateral_weights(
                settings.get('lateral', binding.get('lateral', ())),
                settings.get('falloff', binding.get('falloff', DEFAULT_FALLOFF)),
                settings.get('smooth', binding.get('smooth', False)),
            ):
                if seed in vectors and vertex not in contributions:
                    if weight > 0:
                        contributions[vertex] = vectors[seed], weight
                    if vertex in opposite and weight < 1.:
                        bridges.append((vertex, opposite[vertex], 1. - weight))
            for vertex, (value, weight) in contributions.items():
                weight *= strength
                total[vertex] += value * weight
                weights[vertex] += weight
                contributors[vertex] += 1
                if full_span and strength >= 1.:
                    full[vertex] += 1
    for vertex, seed, weight in bridges:
        if seed in zero_span_vectors:
            total[vertex] += zero_span_vectors[seed] * weight
            weights[vertex] += weight
    denominator = np.maximum(weights, 1.)
    complete = (full >= 2) & (full == contributors)
    denominator[complete] = weights[complete]
    for vertex in np.flatnonzero(weights):
        points[int(vertex)] += om.MVector(*(total[vertex] / denominator[vertex]))
    for vertex, target in pinned.items():
        points[vertex] = target
    result = om.MFnMeshData().create()
    om.MFnMesh().copy(base, result)
    output = om.MFnMesh(result)
    output.setPoints(points)
    moved = [vertex for vertex, (before, after) in enumerate(zip(original, points))
             if (after - before).length() > 1e-8]
    if moved:
        output.unlockVertexNormals(moved)
    return result


def evaluate(source,state,curves,matrices,envelope=1.,node=None):
    """The preview's delta binding with stored samples and harmonic face weights."""
    fn=om.MFnMesh(source)
    # Changed upstream topology must not address unrelated/new vertex IDs.
    if topology(fn)!=state['topology']:return source
    if node is not None and state.get('blend_overlap') and state['region'] is None:
        return blend_overlap(source, state, curves, matrices, envelope, node)
    points=fn.getPoints();delta=np.zeros((len(points),3));counts=np.zeros(len(points))
    offsets=state['offsets']
    if offsets is not None:
        for v,p in enumerate(offsets):points[v]+=om.MVector(*p)
    face_mode=state['region'] is not None
    guide_deltas = {}
    aligned = {}
    for index,(row,ts,(_,baseline)) in enumerate(zip(state['rows'],state['parameters'],state['curves'])):
        current=curves[index]
        if current is None:continue
        curve=om.MFnNurbsCurve(current);lo,hi=curve.knotDomain;start,end=baseline.knotDomain
        ids=row[:-1] if row[0]==row[-1] else row
        settings = state.get('guides', {}).get(str(index))
        local = {}
        for vertex,t in zip(ids,ts):
            point=curve.getPointAtParam(lo+t*(hi-lo))*matrices[index]
            align = not face_mode and (settings or {}).get('align', state['align'])
            original = points[vertex] if align else baseline.getPointAtParam(start+t*(end-start))
            if align:
                aligned.setdefault(vertex, point)
            value=point-original
            if face_mode:
                delta[vertex]+=tuple(value);counts[vertex]+=1
            else:
                local[vertex] = tuple(value)
        if not face_mode:
            guide_deltas[index] = local
            for vertex, value in local.items():
                delta[vertex] += value
                counts[vertex] += 1
    if face_mode:
        border=state['region'].boundary
        delta[border]/=np.maximum(counts[border,None],1.)
        delta=state['region'].spread(delta)
    else:
        selected = {vertex for row in state['rows'] for vertex in row}
        side_weights = np.zeros(len(points))
        if state.get('guides'):
            for index, local in guide_deltas.items():
                settings = state['guides'].get(str(index), {})
                for vertex, seed, weight in lateral_weights(
                    settings.get('lateral', state.get('lateral', ())),
                    settings.get('falloff', state.get('falloff', DEFAULT_FALLOFF)),
                    settings.get('smooth', state.get('smooth', False)),
                ):
                    if vertex not in selected and seed in local:
                        delta[vertex] += np.asarray(local[seed]) * weight
                        side_weights[vertex] += weight
            delta /= np.maximum(counts + side_weights, 1.)[:, None]
        else:
            delta /= np.maximum(counts, 1.)[:, None]
            for vertex, seed, weight in lateral_weights(state.get('lateral', ()), state.get('falloff', DEFAULT_FALLOFF), state.get('smooth', False)):
                delta[vertex] = delta[seed] * weight
    if not face_mode:
        protected = upstream_aligned(node, state['topology']) if node is not None else {}
        if protected:
            iterator = om.MItMeshVertex(source)
            neighbors = set()
            for vertex in protected:
                iterator.setIndex(vertex)
                neighbors.update(iterator.getConnectedVertices())
            for vertex in neighbors.difference(protected):
                delta[vertex] *= .5
            for vertex, target in protected.items():
                points[vertex] = fn.getPoint(vertex)
                delta[vertex] = tuple(target - points[vertex])
        for vertex, target in aligned.items():
            if vertex not in protected:
                delta[vertex] = tuple(target - points[vertex])
    delta*=envelope
    moved=np.flatnonzero(np.any(np.abs(delta)>1e-8,axis=1))
    if not len(moved) and (offsets is None or not np.any(offsets)):return source
    for vertex in moved:points[int(vertex)]+=om.MVector(*delta[vertex])
    from recurve import copy_mesh_data
    output=copy_mesh_data(source)
    out=om.MFnMesh(output);out.setPoints(points)
    # Preserve authored locks elsewhere, matching the temporary preview.
    iterator=om.MItMeshVertex(output);unlock=[]
    for vertex in moved:
        iterator.setIndex(int(vertex))
        for face in iterator.getConnectedFaces():
            corner=list(out.getPolygonVertices(face)).index(vertex)
            if out.isNormalLocked(out.getFaceNormalIds(face)[corner]):
                unlock.append(int(vertex));break
    if unlock:out.unlockVertexNormals(unlock)
    return output


def ensure_plugin():
    if not cmds.pluginInfo(PLUGIN.stem, query=True, loaded=True):
        cmds.loadPlugin(str(PLUGIN), quiet=True)
    if not cmds.about(batch=True) and not cmds.pluginInfo(PLUGIN.stem, query=True, autoload=True):
        cmds.pluginInfo(PLUGIN.stem, edit=True, autoload=True)
        cmds.pluginInfo(savePluginPrefs=True)


def refresh_registered_nodes():
    """Replace compute on the registered Python class without unloading history."""
    import gc
    import runpy

    fresh = runpy.run_path(str(PLUGIN), run_name='_model_pack_curve_refresh')['ControlCurveNode']
    for candidate in gc.get_objects():
        if (type(candidate) is type and candidate.__name__ == 'ControlCurveNode'
                and issubclass(candidate, om.MPxNode) and hasattr(candidate, 'outputMesh')):
            candidate.compute = fresh.compute
    for name in cmds.ls(type=NODE_TYPE) or []:
        fn = om.MFnDependencyNode(om.MSelectionList().add(name).getDependNode(0))
        instance = fn.userNode()
        if instance is not None:
            instance.binding_text = None
        cmds.dgdirty(name)


class SavedBinding:
    def __init__(self,session):
        ensure_plugin()
        controls=session.controls;paths=controls.paths()
        if paths is None:raise ValueError('Control curves are missing. Undo their deletion before saving.')
        self.base=session.control_base
        state=definition(session);self.modifier=om.MDGModifier();self.connected=False
        self.node=self.modifier.createNode(NODE_TYPE);self.modifier.renameNode(self.node,'modelPackControlCurve#')
        node=om.MFnDependencyNode(self.node)
        destination=om.MFnDependencyNode(dag_path(session.path).node()).findPlug('inMesh',False)
        # Keep older saved curves and ordinary modeling history live when the
        # vertex topology is unchanged. Bake only the preview's local offsets.
        source=None
        from recurve_colors import same_colors
        for candidate in session.source.upstream:
            if candidate==session.source.output:continue
            data=candidate.asMObject()
            if not data.isNull() and topology(om.MFnMesh(data))==state['topology']:
                if not same_colors(om.MFnMesh(self.base),om.MFnMesh(data)):continue
                source=candidate
                a=om.MFnMesh(self.base).getPoints();b=om.MFnMesh(data).getPoints()
                state['offsets']=[tuple(x-y) for x,y in zip(a,b)];break
        if source is None:source=session.source.output
        self.modifier.newPlugValueString(node.findPlug('binding',False),json.dumps(state,separators=(',',':')))
        self.modifier.connect(source,node.findPlug('inputMesh',False))
        for old in destination.connectedTo(True,False):self.modifier.disconnect(old,destination)
        self.modifier.connect(node.findPlug('outputMesh',False),destination)
        mesh_transform=om.MFnDependencyNode(dag_path(session.path).transform())
        self.modifier.connect(mesh_transform.findPlug('worldInverseMatrix',False).elementByLogicalIndex(0),
                              node.findPlug('meshInverseMatrix',False))
        for index,path in enumerate(paths):
            shape=om.MFnDependencyNode(path.node())
            self.modifier.connect(shape.findPlug('local',False),node.findPlug('controlCurve',False).elementByLogicalIndex(index))
            self.modifier.connect(shape.findPlug('worldMatrix',False).elementByLogicalIndex(0),
                                  node.findPlug('curveMatrix',False).elementByLogicalIndex(index))

    def redo(self):
        if not self.connected:
            try:self.modifier.doIt()
            except Exception:
                self.modifier.undoIt();raise
            self.connected=True

    def undo(self):
        if self.connected:self.modifier.undoIt();self.connected=False

    def validate(self,path,expected):
        from recurve import mesh_plug
        output=mesh_plug(path,'outMesh').asMObject()
        if output.isNull():raise RuntimeError('Could not evaluate the saved control curves.')
        actual=om.MFnMesh(output);before=om.MFnMesh(expected)
        if topology(actual)!=topology(before):raise RuntimeError('Saved curves changed the mesh topology.')
        a=actual.getPoints();b=before.getPoints()
        span=max((p-b[0]).length() for p in b)
        if max((p-q).length() for p,q in zip(a,b))>max(1e-5,span*1e-6):
            raise RuntimeError('Saved curves did not match the current preview. The live editor was kept open.')

