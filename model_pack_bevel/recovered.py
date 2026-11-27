"""Edit recognized, baked bevel bands while keeping their original mesh intact."""
from collections import defaultdict
import math
from pathlib import Path
from statistics import median

from maya import cmds
from maya.api import OpenMaya as om
from slide_for_maya.core import _mesh_fn, SlideError
from . import bevel_detection, ribbon_edit, unbevel_faces
from .graph import EdgeGraph
from .profiles import Curve, profile_value
from .session import BevelSession, without_undo
from .geometry import TemplateGeometry
from .support_edit import read_support_profile, support_steps, supported_band


def selected_components(component_type):
    """Read a single mesh's selection without mixing component types."""
    selection = om.MGlobal.getActiveSelectionList()
    iterator = om.MItSelectionList(selection)
    name = None;faces=set();objects=set()
    while not iterator.isDone():
        try:path,component=iterator.getComponent()
        except RuntimeError:return None
        if component.isNull():
            objects.add(path.fullPathName())
            iterator.next()
            continue
        if not component.hasFn(component_type):return None
        if path.node().hasFn(om.MFn.kTransform):path.extendToShape()
        if not path.node().hasFn(om.MFn.kMesh):return None
        current=path.fullPathName()
        if name is not None and name!=current:raise SlideError('Выделите одну фаску или полосы на одном меше.')
        name=current
        faces.update(om.MFnSingleIndexedComponent(component).getElements())
        iterator.next()
    if name is None or not faces or objects - {name, name.rsplit('|',1)[0]}:
        return None
    return name,tuple(sorted(faces))


def selected_faces():
    return selected_components(om.MFn.kMeshPolygonComponent)


def selected_band():
    """Recover a selected polygon band."""
    request=selected_faces()
    if request is None:return None
    return recover_band(*request)


def recover_band(name, ids):
    name = _mesh_fn(name).fullPathName()
    faces = set(ids)
    # An explicit band determines its own direction. At crossing fillets the
    # automatic search may prefer the thinner longitudinal strip and classify
    # the selected transverse faces as support surfaces (Detail04). Validate
    # the whole selection before using seed expansion, which is still useful
    # for a single picked face or an incomplete band.
    regular_band = None
    if len(faces)>1:
        details={}
        try:
            _,rows,_=unbevel_faces.prepare(name,faces,details=details)
        except ValueError:pass
        else:
            if (all(region.get('quad_network') for region in details.get('regions', ())) and
                    len({v for row in rows for v in row})==sum(map(len,rows))):
                if details.get('regions') or read_support_profile(name, faces, rows):
                    return name,tuple(sorted(faces))
                # An already complete ordinary strip keeps priority over
                # overlapping fillets in the generic detector.
                regular_band = tuple(sorted(faces))
    recognized = supported_band(name, faces)
    if recognized:
        return name, tuple(sorted(recognized))
    if regular_band is not None:
        return name, regular_band
    result=bevel_detection.detect(name,seeds=faces,include_corners=False,use_history=False)
    recognized={f for group in result.groups if group.ready for f in group.faces}
    if not recognized:
        raise SlideError('Не удалось распознать фаску. Выделите полигоны её полосы, без основных поверхностей.')
    if faces-recognized:
        raise SlideError('В выделении есть основные поверхности. Выделите только полосу фаски.')
    return name,tuple(sorted(recognized))


def snapshot(fn):
    data=om.MFnMeshData().create();om.MFnMesh().copy(fn.object(),data)
    source=om.MFnDependencyNode(fn.object()).findPlug('outMesh',False).asMObject()
    om.MFnMeshData(data).copyObjectGroups(source)
    return data


def rounded_offset(first,last,corner,t):
    """Circular fillet at the same chord parameter; zero at both borders."""
    flat=first+(last-first)*t
    if t<1e-8 or t>1-1e-8:return om.MVector()
    a,b=first-corner,last-corner
    if min(a.length(),b.length())<1e-10:return om.MVector()
    cosine=max(-.999999,min(.999999,a.normal()*b.normal()))
    center=corner+(a+b)/(1+cosine)
    radius=(first-center).length()
    direction=corner-(first+(last-first)*.5)
    if direction.length()<1e-10:return om.MVector()
    direction.normalize();d=flat-center
    along=d*direction
    step=-along+math.sqrt(max(0,along*along+radius*radius-d*d))
    return direction*max(0,step)


def collapse_sections(source, rows, centers):
    """Weld explicit zero-width sections, never other nearby mesh vertices."""
    from recurve_normals import NormalTransfer
    from recurve_uv import assign_uv_sets
    fn=om.MFnMesh(source)
    original=fn.getPoints();roots={};targets={}
    for row,center in zip(rows,centers):
        root=row[0];targets[root]=om.MPoint(center)
        for vertex in row:
            if vertex in roots:raise SlideError('Сечения фаски пересекаются.')
            roots[vertex]=root
    mapping={};points=[]
    for vertex,point in enumerate(original):
        if vertex in roots and roots[vertex]!=vertex:continue
        mapping[vertex]=len(points);points.append(targets.get(vertex,point))
    mapping.update({vertex:mapping[root] for vertex,root in roots.items()})
    faces=[];owners=[];corners=[];hard=set()
    for face in range(fn.numPolygons):
        clean=[]
        for corner,vertex in enumerate(fn.getPolygonVertices(face)):
            mapped=mapping[vertex]
            if not clean or clean[-1][0]!=mapped:clean.append((mapped,corner,vertex))
        if len(clean)>1 and clean[0][0]==clean[-1][0]:clean.pop()
        unique={v for v,_,_ in clean}
        if len(unique)<3:
            if len(unique)==2:hard.add(frozenset(unique))
            continue
        if len(unique)!=len(clean):raise SlideError('Схлопывание фаски создаёт защемлённый полигон.')
        faces.append([v for v,_,_ in clean]);owners.append(face);corners.append(clean)
    result=om.MFnMeshData().create();out=om.MFnMesh()
    out.create(points,[len(face) for face in faces],[v for face in faces for v in face],parent=result)
    if out.numVertices!=len(points) or any(list(out.getPolygonVertices(f))!=face for f,face in enumerate(faces)):
        raise SlideError('Maya изменила топологию за пределами удаляемой фаски.')
    smooth={}
    for edge in range(fn.numEdges):
        pair=frozenset(mapping[v] for v in fn.getEdgeVertices(edge))
        smooth[pair]=smooth.get(pair,True) and fn.isEdgeSmooth(edge)
    hard_edges=[]
    flags=[]
    for edge in range(out.numEdges):
        pair=frozenset(out.getEdgeVertices(edge))
        if pair in hard:hard_edges.append(edge)
        flags.append(pair not in hard and smooth.get(pair,True))
    out.setEdgeSmoothings(list(range(out.numEdges)),flags);out.cleanupEdgeSmoothing()
    fn.getNormals();transfer=NormalTransfer(fn,points)
    for face,(owner,clean) in enumerate(zip(owners,corners)):
        for vertex,corner,old in clean:
            if old not in roots:transfer.add(face,vertex,owner,corner,corner,0.)
    transfer.apply(out)
    # Authoring corner normals can implicitly harden edges. Restore the
    # explicit flags after transferring the unaffected locked normals.
    out.setEdgeSmoothings(list(range(out.numEdges)),flags);out.cleanupEdgeSmoothing()
    uvsets={}
    for name in fn.getUVSetNames():
        counts,ids=fn.getAssignedUVs(name);offsets=[0]
        for count in counts:offsets.append(offsets[-1]+count)
        assigned=[];lengths=[]
        for owner,clean in zip(owners,corners):
            values=[ids[offsets[owner]+corner] for _,corner,_ in clean] if counts[owner] else []
            assigned.extend(values);lengths.append(len(values))
        uvsets[name]=(*fn.getUVs(name),lengths,assigned)
    result=assign_uv_sets(result,uvsets,fn.currentUVSetName() if uvsets else None)
    groups=om.MFnMeshData(source);output_groups=om.MFnMeshData(result)
    output_groups.copyObjectGroups(source)
    for index in range(groups.objectGroupCount):
        group=groups.objectGroup(index);component=groups.objectGroupComponent(group)
        if component.isNull() or not component.hasFn(om.MFn.kMeshPolygonComponent):continue
        old=om.MFnSingleIndexedComponent(component)
        members=None if old.isComplete else set(old.getElements())
        new=om.MFnSingleIndexedComponent();component=new.create(om.MFn.kMeshPolygonComponent)
        new.addElements([f for f,owner in enumerate(owners) if members is None or owner in members])
        output_groups.setObjectGroupComponent(group,component)
    return result,owners,hard_edges


class RecoveredBevelSession(BevelSession):
    recovered=True

    def __init__(self,shape,faces, *, network=None, manual=False):
        fn=_mesh_fn(shape)
        if fn.isInstanced() or cmds.referenceQuery(shape,isNodeReferenced=True):
            raise SlideError('Сделайте меш локальным и уникальным перед редактированием фаски.')
        if fn.numColorSets:raise SlideError('Редактирование фасок на мешах с vertex colors пока не поддерживается.')
        if om.MFnDependencyNode(fn.object()).findPlug('inMesh',False).isLocked:
            raise SlideError('Вход меша заблокирован.')
        if not cmds.getAttr(shape+'.visibility',settable=True):raise SlideError('Видимость меша заблокирована.')
        details={}
        self.manual_faces = tuple(faces) if manual else ()
        self._ribbon_junctions = None
        if manual and network is None:
            strip, candidate = ribbon_edit.manual_sections(shape, faces)
            if candidate[1]:
                faces, network = strip, candidate
            else:
                try:
                    unbevel_faces.prepare(shape, faces, details=details)
                except ValueError:
                    faces, network = strip, candidate
                    details = {}
        if network:
            centers, rows, graph, projectors, source_points = ribbon_edit.sections(shape, faces, *network)
            self._ribbon_junctions = graph, projectors, source_points
        else:
            centers,rows,_=unbevel_faces.prepare(shape,faces,details=details)
        regions=details.get('regions', ())
        self.width_only=bool(regions or network)
        if not all(region.get('quad_network') for region in regions):
            raise SlideError('Выделите полосу фаски между угловыми стыками.')
        if not network and len({v for row in rows for v in row})!=sum(map(len,rows)):
            raise SlideError('Выделите полосу фаски между угловыми стыками.')
        self.mesh=cmds.listRelatives(shape,parent=True,fullPath=True)[0]
        self.original_shape=shape
        self.selection=om.MGlobal.getActiveSelectionList();self.hilite=om.MGlobal.getHiliteList()
        self.selection_mode=om.MGlobal.selectionMode();self.component_mask=om.MGlobal.componentSelectionMask()
        self.visible=cmds.getAttr(shape+'.visibility')
        self.before=snapshot(fn)
        self.matrix=fn.dagPath().inclusiveMatrix();self.inverse=self.matrix.inverse()
        self.centers=[om.MPoint(p) for p in centers];self.rows=[list(row) for row in rows]
        self.faces=tuple(faces)
        points=[om.MPoint(p) for p in fn.getPoints(om.MSpace.kWorld)]
        self._surface_projectors = (self._ribbon_junctions[1] if self._ribbon_junctions is not None else
                                    ribbon_edit.surface_projectors(fn, points, set(faces)))
        self._surface_planes = ribbon_edit.surface_planes(fn, points, set(faces)) if network is None else ()
        self._resampled_projectors = {}
        self._normal_motion = (ribbon_edit.normal_motion(fn, set(faces) | set(network[1]), points)
                               if manual and network else None)
        profile = None if self.width_only else read_support_profile(shape, faces, self.rows)
        self.can_edit_supports = profile is not None or (not self.width_only and not fn.numColorSets
            and len(fn.getConnectedShaders(fn.dagPath().instanceNumber())[0]) <= 1)
        self._original_supports = 0
        self.sharp = False
        core_rows = self.rows
        if profile is not None:
            count, self.sharp = profile
            self.support_edges = self._original_supports = count
            self._support_gaps, self.support_spacing = support_steps(points, self.rows, count)
            self._original_spacing = self.support_spacing
            core_rows = [row[count:-count] for row in self.rows]
            if self.sharp:
                self.centers = [points[row[1]] for row in core_rows]
        self._core_points = [[points[v] for v in row] for row in core_rows]
        self.row_widths=[
            max((points[v]-center).length() for v in row) if self.width_only
            else ((points[row[0]]-center).length()+(points[row[-1]]-center).length())*.5
            for center,row in zip(self.centers,core_rows)
        ]
        if min(self.row_widths)<1e-9:raise SlideError('У распознанной фаски нулевая ширина.')
        self.width=self.original_width=median(self.row_widths)
        self.weights={i:w/self.width for i,w in enumerate(self.row_widths)}
        estimates=[]
        for center,row in (() if self.width_only or self.sharp else zip(self.centers,core_rows)):
            first,last=points[row[0]],points[row[-1]];axis=last-first
            for vertex in row[1:-1]:
                t=max(0,min(1,((points[vertex]-first)*axis)/(axis*axis)))
                offset=rounded_offset(first,last,center,t)
                if offset.length()>1e-10:estimates.append(((points[vertex]-(first+axis*t))*offset)/(offset*offset))
        self.shape=self.original_shape_value=max(-1,min(1,median(estimates))) if estimates else 0.
        self.segments=self.original_segments=1 if self.sharp else max(1,round(median(len(row)-1 for row in core_rows)))
        self.transverse_cuts=0;self.keep_equal=True;self.all_sections=False
        self.profile_kind=self.falloff_kind='round'
        self.profile_curve=Curve(((0,0,None),(.5,.5,None),(1,0,None)),False);self.falloff_curve=Curve()
        owner=defaultdict(set)
        for i,row in enumerate(rows):
            for vertex in row:
                owner[vertex].add(i)
        links=set()
        for face in faces:
            ring=fn.getPolygonVertices(face)
            owners={i for v in ring for i in owner[v]}
            if len(owners)==2:links.add(tuple(sorted(owners)))
            elif network:
                own_rows = sorted(i for i in owners if sum(i in owner[v] for v in ring) >= 2)
                if len(own_rows)==2:links.add(tuple(own_rows))
        if not links:raise SlideError('Не удалось восстановить продольное направление фаски.')
        self.graph=EdgeGraph(self.centers,enumerate(sorted(links)));self.edges=tuple(self.graph.edges)
        self.edit_node=self._base_input=None
        self.motion_scale=min((math.dist(self.graph.points[a],self.graph.points[b]) for a,b in links),default=self.width)*.005
        self.preview=self.node=None;self.history_nodes=[];self.isolate_sets=[];self._contact_marks=[]
        self._display_mesh=None;self._written={};self._undo=[];self._redo=[];self._action_before=None
        self.closed=False;self.contact_faces=();self.max_width=math.inf
        self._cache={self._total_segments():(self.before,self.rows)}
        self.after=self.before;self.after_rows=self.rows
        self.shaders,self.materials=fn.getConnectedShaders(fn.dagPath().instanceNumber())
        self._initial=self.snapshot()
        try:
            with without_undo():
                self.preview=cmds.createNode('transform',name='bevelPreview#')
                cmds.xform(self.preview,worldSpace=True,matrix=list(self.matrix))
                selection=om.MSelectionList();selection.add(self.preview)
                parent=selection.getDependNode(0)
                self._source=om.MFnMesh().copy(self.before,parent)
                source_fn=om.MFnDependencyNode(self._source)
                source_fn.setName('bevelEditSource#');source_fn.findPlug('intermediateObject',False).setBool(True)
                shape_name=cmds.createNode('mesh',parent=self.preview,name='bevelEditPreviewShape#')
                cmds.connectAttr(source_fn.name()+'.outMesh',shape_name+'.inMesh')
                self._preview_shape=shape_name
                for index,shader in enumerate(self.shaders):
                    name=om.MFnDependencyNode(shader).name()
                    if len(self.shaders)==1:cmds.sets(self.preview,edit=True,forceElement=name)
                    else:
                        members=[shape_name+'.f[%d]'%f for f,value in enumerate(self.materials) if value==index]
                        if members:cmds.sets(members,edit=True,forceElement=name)
                for panel in cmds.getPanel(type='modelPanel') or []:
                    if cmds.isolateSelect(panel,q=True,state=True):
                        group=cmds.isolateSelect(panel,q=True,viewObjects=True)
                        if group:cmds.sets(self.preview,add=group);self.isolate_sets.append(group)
                cmds.setAttr(shape+'.visibility',False)
                self._display_mesh=self.preview
                om.MGlobal.setActiveSelectionList(self.selection)
        except Exception:
            self.cancel();raise

    def _total_segments(self):
        return (2 if self.sharp else self.segments) + 2 * self._support_count()

    def _geometry(self):
        total = self._total_segments()
        if total not in self._cache:
            fn=om.MFnMesh(self.before)
            if fn.numColorSets or len(self.shaders)>1:
                raise SlideError('Изменение сегментов требует одного материала и отсутствия vertex colors. Ширина и форма доступны.')
            import recurve
            self._cache[total]=recurve.rebuild_chains(self.before,self.rows,total,False)
        return self._cache[total]

    def _rebuild_preview(self):
        # This preview reuses the same mesh node. _geometry resamples it
        # on demand when the number of rows changes.
        pass

    def change_supports(self, *, sharp=None, **settings):
        if not self.can_edit_supports or sharp not in (None, self.sharp):
            raise SlideError('Для этой восстановленной фаски недоступно такое изменение опорных рёбер.')
        return super().change_supports(**settings)

    def _supported_section(self, index):
        center = self.centers[index]
        original = self._core_points[index]
        first, last = original[0], original[-1]
        count = 2 if self.sharp else self.segments
        core = []
        for i in range(count + 1):
            t = i / count
            j = min(math.floor(t * (len(original) - 1)), len(original) - 2)
            position = original[j] + (original[j + 1] - original[j]) * (t * (len(original) - 1) - j)
            if not self.sharp and 0 < i < count:
                offset = rounded_offset(first, last, center, t)
                delta = (profile_value(self.profile_kind, t, self.shape, self.profile_curve) / math.sin(math.pi * t)
                         - self.original_shape_value)
                position += offset * delta
            core.append((center, position - center))
        count = self._support_count()
        if not count:
            return core
        if not self._original_supports:
            def extend_new(end):
                direction = end - center
                step = direction.normal() * self.support_spacing
                return [(center + step * k, direction) for k in range(1, count + 1)]

            return extend_new(first)[::-1] + core + extend_new(last)
        scale = self.support_spacing / self._original_spacing
        left, right = self._support_gaps[index]

        def extend(gaps, end):
            return [(center + (gaps[k - 1] if k <= self._original_supports else
                               gaps[-1] * (k / self._original_supports)) * scale, end - center)
                    for k in range(1, count + 1)]

        return extend(left, first)[::-1] + core + extend(right, last)

    def update(self,*,width=None,shape=None,segments=None):
        profile_edit = shape not in (None, self.shape) or segments not in (None, self.segments)
        if self.sharp and profile_edit:
            raise SlideError('У фаски без скругления меняются ширина и опорные рёбра.')
        if self.width_only and profile_edit:
            raise SlideError('Для сети фасок со стыками доступно изменение ширины.')
        previous=self.snapshot()
        if width is not None:
            if not math.isfinite(width):raise SlideError('Ширина должна быть конечным числом.')
            self.width=max(0,width)
        if shape is not None:
            self.shape=max(-1,min(1,shape))
        if segments is not None:self.segments=max(1,min(64,int(segments)))
        try:
            data,rows=self._geometry()
            if self.snapshot()==self._initial:
                result=data
            else:
                fn=om.MFnMesh(data);points=fn.getPoints()
                world=[om.MPoint(p)*self.matrix for p in points]
                origins=[om.MPoint(p) for p in world];vectors=[om.MVector() for _ in world]
                collapsed = list(world) if self._support_count() else None
                moved=[]
                ribbon_counts = defaultdict(int) if self._ribbon_junctions is not None else None
                for index,(center,row,base_width) in enumerate(
                        () if self._normal_motion is not None else zip(self.centers,rows,self.row_widths)):
                    if self._original_supports or collapsed is not None:
                        scale = self.weights.get(index, 1) / base_width
                        for vertex, (origin, direction) in zip(row, self._supported_section(index)):
                            origins[vertex] = origin
                            vectors[vertex] = direction * scale
                            moved.append(vertex)
                            if collapsed is not None:
                                collapsed[vertex] = center
                        continue
                    first,last=world[row[0]],world[row[-1]];axis=last-first;squared=axis*axis
                    for vertex in row:
                        t=max(0,min(1,((world[vertex]-first)*axis)/max(squared,1e-20)))
                        offset=om.MVector() if self.width_only else rounded_offset(first,last,center,t)
                        sine=math.sin(math.pi*t)
                        delta=((profile_value(self.profile_kind,t,self.shape,self.profile_curve)/sine)-self.original_shape_value) if sine>1e-7 else 0.
                        origins[vertex]=om.MPoint(center)
                        direction = (world[vertex]-center+offset*delta)*(self.weights.get(index,1)/base_width)
                        if ribbon_counts is None:
                            vectors[vertex] = direction
                        else:
                            vectors[vertex] += direction
                            ribbon_counts[vertex] += 1
                        moved.append(vertex)
                if self._normal_motion is not None:
                    vectors, moved, self._support_motion_groups = self._normal_motion
                    vectors = [om.MVector(v) for v in vectors]
                    moved = list(moved)
                    for vertex in moved:
                        origins[vertex] = world[vertex] - vectors[vertex] * self.original_width
                    # Local width grips still act on recovered cross sections.
                    # Share their scale across each support row so the grip
                    # cannot stretch the original land spacing.
                    scales = defaultdict(list)
                    for index, row in enumerate(self.rows):
                        ratio = self.weights.get(index, 1.) * self.original_width / self.row_widths[index]
                        for vertex in row:
                            scales[vertex].append(ratio)
                    grouped = {v for group in self._support_motion_groups for v in group}
                    for group in (*self._support_motion_groups, *((v,) for v in moved if v not in grouped)):
                        values = [value for v in group for value in scales[v]]
                        ratio = sum(values) / len(values) if values else 1.
                        for vertex in group:
                            vectors[vertex] *= ratio
                elif self._ribbon_junctions is not None:
                    graph, projectors, source_points = self._ribbon_junctions
                    # Anchor every displacement at the original width, including
                    # the interpolated corner vertices.
                    moved = list(ribbon_counts)
                    for vertex in moved:
                        vectors[vertex] = ribbon_edit.constrained_motion(
                            projectors, vertex, vectors[vertex] / ribbon_counts[vertex])
                    ribbon_edit.align_strip_motion(fn, self.faces, rows, vectors, projectors, source_points)
                    for vertex in moved:
                        origins[vertex] = world[vertex] - vectors[vertex] * self.original_width
                    junction = ribbon_edit.junction_motion(graph, set(moved), vectors, projectors, source_points)
                    junction = set(junction) | ribbon_edit.preserve_collapsed_caps(fn, set(moved), vectors, source_points)
                    for vertex in junction:
                        origins[vertex] = world[vertex] - vectors[vertex] * self.original_width
                    moved.extend(junction)
                projectors = self._surface_projectors
                if rows != self.rows:
                    count = self._total_segments()
                    if count not in self._resampled_projectors:
                        self._resampled_projectors[count] = ribbon_edit.resampled_projectors(world, self._surface_planes)
                    projectors = self._resampled_projectors[count]
                for vertex in (() if self._normal_motion is not None else moved):
                    origins[vertex] = world[vertex] + ribbon_edit.constrained_motion(
                        projectors, vertex, origins[vertex] - world[vertex])
                    vectors[vertex] = ribbon_edit.constrained_motion(projectors, vertex, vectors[vertex])
                # Original support faces define a physical width limit.
                limit=math.inf
                lower=0.
                if self._ribbon_junctions is not None:
                    # A polygon's total area can stay positive after one of its
                    # corners folds over. Protect the original triangulation,
                    # rather than accepting an inverted triangle inside a quad.
                    _, triangles = fn.getTriangles()
                    for offset in range(0, len(triangles), 3):
                        ids = triangles[offset:offset + 3]
                        if not any(vectors[v].length() > 1e-12 for v in ids):
                            continue
                        a, b, c = ids
                        normal = (world[b] - world[a]) ^ (world[c] - world[a])
                        span = max((world[b] - world[a]).length(), (world[c] - world[a]).length())
                        if normal.length() <= max(span * span * 1e-8, 1e-16):
                            continue
                        normal.normalize()
                        def triangle_area(width):
                            q = [origins[v] + vectors[v] * width for v in ids]
                            return ((q[1] - q[0]) ^ (q[2] - q[0])) * normal
                        start = self.original_width
                        zero, plus, minus = triangle_area(start), triangle_area(start * 2), triangle_area(0.)
                        linear, quadratic = (plus - minus) * .5, (plus + minus) * .5 - zero
                        up = TemplateGeometry._positive_root(zero, linear, quadratic)
                        down = TemplateGeometry._positive_root(zero, -linear, quadratic)
                        limit = min(limit, start * (1 + up * .99))
                        lower = max(lower, start * (1 - down * .99))
                for face in range(fn.numPolygons):
                    ids=list(fn.getPolygonVertices(face));normal=om.MVector()
                    for a,b in zip(ids[1:-1],ids[2:]):normal+=(world[a]-world[ids[0]])^(world[b]-world[ids[0]])
                    if normal.length()<1e-12:continue
                    normal.normalize()
                    def area(w):
                        q=[origins[v]+vectors[v]*w for v in ids]
                        return sum(((q[i]-q[0])^(q[i+1]-q[0]))*normal for i in range(1,len(q)-1))
                    if self._ribbon_junctions is not None:
                        if not any(vectors[v].length() > 1e-12 for v in ids):
                            continue
                        start = self.original_width
                        zero, plus, minus = area(start), area(start * 2), area(0.)
                        linear, quadratic = (plus-minus)*.5, (plus+minus)*.5-zero
                        up = TemplateGeometry._positive_root(zero, linear, quadratic)
                        down = TemplateGeometry._positive_root(zero, -linear, quadratic)
                        limit = min(limit, start * (1 + up * (1-1e-5)))
                        lower = max(lower, start * (1 - down * (1-1e-5)))
                        continue
                    zero=area(0)
                    if zero<=1e-12:
                        if collapsed is not None:
                            root = collapsed[ids[0]]
                            unoffset = sum(((collapsed[a] - root) ^ (collapsed[b] - root)) * normal
                                           for a, b in zip(ids[1:-1], ids[2:]))
                            if unoffset > 1e-12:
                                raise SlideError('Шаг опорных рёбер превышает свободное место вокруг фаски.')
                        continue
                    plus,minus=area(1),area(-1)
                    limit=min(limit,TemplateGeometry._positive_root(zero,(plus-minus)*.5,(plus+minus)*.5-zero))
                self.max_width=limit if self._ribbon_junctions is not None else limit*(1-1e-5)
                if self._ribbon_junctions is not None:
                    lower = max(lower, self.original_width * 1e-5)
                self.width=max(lower,min(self.width,self.max_width))
                self.contact_faces=()
                # Retain a valid, reversible strip while the tool is active.
                # Exact zero is remembered and welded only by commit().
                preview_width=max(self.width,self.original_width*1e-5)
                for vertex in moved:points[vertex]=(origins[vertex]+vectors[vertex]*preview_width)*self.inverse
                result=om.MFnMeshData().create();om.MFnMesh().copy(data,result)
                om.MFnMeshData(result).copyObjectGroups(data)
                out=om.MFnMesh(result);out.setPoints(points)
                # Initialize normals before querying/unlocking authored corners.
                out.getNormals();unlock=[];it=om.MItMeshVertex(result)
                for vertex in moved:
                    it.setIndex(vertex)
                    if any(out.isNormalLocked(out.getFaceNormalIds(f)[list(out.getPolygonVertices(f)).index(vertex)]) for f in it.getConnectedFaces()):unlock.append(vertex)
                if unlock:
                    flags = [out.isEdgeSmooth(edge) for edge in range(out.numEdges)]
                    out.unlockVertexNormals(unlock)
                    out.setEdgeSmoothings(list(range(out.numEdges)), flags)
                    out.cleanupEdgeSmoothing()
            with without_undo():
                modifier=om.MDGModifier()
                modifier.newPlugValue(om.MFnDependencyNode(self._source).findPlug('cachedInMesh',False),result);modifier.doIt()
                _mesh_fn(self.preview).getPoints()
            self.after,self.after_rows=result,rows
        except Exception:
            for name,value in previous.items():setattr(self,name,value)
            raise
        return self.width

    def change_transverse_cuts(self,delta):
        raise SlideError('Поперечные разрезы доступны при создании новой фаски.')

    def _remove_preview(self,restore_selection=True):
        with without_undo():
            if self.preview and cmds.objExists(self.preview):cmds.delete(self.preview)
            if cmds.objExists(self.original_shape):cmds.setAttr(self.original_shape+'.visibility',self.visible)
            if restore_selection:
                om.MGlobal.setSelectionMode(self.selection_mode)
                om.MGlobal.setComponentSelectionMask(self.component_mask)
                om.MGlobal.setHiliteList(self.hilite)
                om.MGlobal.setActiveSelectionList(self.selection)
        self.preview=None;self.history_nodes=[];self._display_mesh=None

    def commit(self):
        if self.closed:return None
        if self.snapshot()==self._initial:
            self._remove_preview()
            self.closed=True;return None
        import recurve
        if recurve.SESSION is not None:raise SlideError('Завершите текущий Control Curve перед применением фаски.')
        if not cmds.pluginInfo('recurve',q=True,loaded=True):cmds.loadPlugin(str(Path(__file__).resolve().parents[1]/'recurve.py'))
        after=self.after;owners=None
        if self.width==0:
            after,owners,edges=collapse_sections(after,self.after_rows,[p*self.inverse for p in self.centers])
            selections=recurve.chain_selection(self.original_shape,after,
                                                [list(om.MFnMesh(after).getEdgeVertices(e)) for e in edges])
        else:selections=recurve.chain_selection(self.original_shape,after,self.after_rows)
        self._remove_preview()
        recurve.PENDING=(self.original_shape,self.before,after,self.selection,selections)
        cmds.undoInfo(openChunk=True,chunkName='Edit Existing Bevel')
        try:
            cmds.recurveApply()
            # cachedInMesh retains UVs in memory, but Maya's mesh serializer
            # writes coordinates from the node's uvSetPoints attributes. Author
            # those arrays with native undoable commands for save/reopen too.
            source=cmds.listConnections(self.original_shape+'.inMesh',source=True,destination=False,plugs=True)[0].split('.')[0]
            fn=om.MFnMesh(after)
            for index,name in enumerate(fn.getUVSetNames()):
                cmds.setAttr(source+'.uvSet[%d].uvSetName'%index,name,type='string')
                us,vs=fn.getUVs(name)
                if len(us):cmds.setAttr(source+'.uvSet[%d].uvSetPoints[0:%d]'%(index,len(us)-1),*[v for pair in zip(us,vs) for v in pair],type='float2')
            cmds.setAttr(source+'.currentUVSet',fn.currentUVSetName(),type='string')
            if len(self.shaders)>1:
                # Native groupParts serialize face membership in saved scenes;
                # mesh-data groups alone are only an in-memory result.
                from slide_for_maya.edit_session import assign_materials
                assign_materials(self.original_shape,[om.MFnDependencyNode(s).name() for s in self.shaders],
                                 [self.materials[f] for f in owners] if owners is not None else self.materials)
        except Exception:
            cmds.undoInfo(closeChunk=True);cmds.undo()
            recurve.PENDING=None;self.closed=True
            raise
        else:
            recurve.PENDING=None;cmds.undoInfo(closeChunk=True);self.closed=True
        return self.original_shape
