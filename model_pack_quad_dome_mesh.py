"""Replace a selected planar disk; split an odd rim through adjacent quads."""
from collections import defaultdict
import math
import numpy as np
from maya import cmds
from maya.api import OpenMaya as om
from model_pack_quad_dome_geometry import Patch


def selected_region():
    selected=om.MGlobal.getActiveSelectionList();shape=None;faces=set()
    for i in range(selected.length()):
        try:path,component=selected.getComponent(i)
        except RuntimeError:raise ValueError('Quad Dome: выберите полигон или плоскую группу полигонов.')
        if component.isNull() or not component.hasFn(om.MFn.kMeshPolygonComponent):
            raise ValueError('Quad Dome: выберите полигон или плоскую группу полигонов.')
        if path.node().hasFn(om.MFn.kTransform):path.extendToShape()
        if shape is not None and shape!=path.fullPathName():raise ValueError('Quad Dome: выберите поверхность одного меша.')
        shape=path.fullPathName();fn=om.MFnMesh(path);comp=om.MFnSingleIndexedComponent(component)
        faces.update(range(fn.numPolygons) if comp.isComplete else comp.getElements())
    if shape is None or not faces:raise ValueError('Quad Dome: выберите поверхность.')
    return shape,sorted(faces)


class Mesh:
    def __init__(self, shape=None, faces=None):
        from recurve import mesh,snapshot
        if shape is None:shape,faces=selected_region()
        fn=mesh(shape);path=fn.dagPath();self.path=path.fullPathName();self.selected=set(faces)
        if path.isInstanced() or cmds.referenceQuery(self.path,isNodeReferenced=True):raise ValueError('Quad Dome: нужен локальный меш без instances.')
        dep=om.MFnDependencyNode(path.node())
        if dep.findPlug('inMesh',False).isLocked or (cmds.lockNode(self.path,q=True,lock=True) or [False])[0]:
            raise ValueError('Quad Dome: меш заблокирован.')
        if not self.selected or min(self.selected)<0 or max(self.selected)>=fn.numPolygons:raise ValueError('Quad Dome: выделение больше не существует.')
        holes=fn.getHoles()
        if any(int(item[0]) in self.selected for item in holes):raise ValueError('Quad Dome: отверстия в выбранной поверхности не поддерживаются.')
        self.matrix=path.inclusiveMatrix();self.inverse=self.matrix.inverse()
        if abs(om.MTransformationMatrix(self.matrix).asMatrix().det4x4())<1e-12:raise ValueError('Quad Dome: у объекта нулевой масштаб.')
        self.before=snapshot(fn);self.handle=om.MObjectHandle(path.node())
        self.original=[om.MPoint(p) for p in fn.getPoints(om.MSpace.kWorld)]
        self.polygons=[list(fn.getPolygonVertices(f)) for f in range(fn.numPolygons)]
        self.edge_faces=defaultdict(list);boundary={};adj=defaultdict(set)
        for face,polygon in enumerate(self.polygons):
            for j,(a,b) in enumerate(zip(polygon,polygon[1:]+polygon[:1])):
                edge=tuple(sorted((a,b)));self.edge_faces[edge].append(face)
                if face in self.selected:
                    if edge in boundary:
                        other=boundary.pop(edge)[0];adj[face].add(other);adj[other].add(face)
                    else:boundary[edge]=(face,j,a,b)
        seen={next(iter(self.selected))};todo=list(seen)
        while todo:
            for other in adj[todo.pop()]-seen:seen.add(other);todo.append(other)
        if seen!=self.selected:raise ValueError('Quad Dome: выберите одну связную поверхность.')
        outgoing={};incoming=set()
        for edge,(face,j,a,b) in boundary.items():
            if len(self.edge_faces[edge])>2 or a in outgoing or b in incoming:raise ValueError('Quad Dome: неоднозначная или неманифолдная граница.')
            outgoing[a]=(b,face,j);incoming.add(b)
        self.boundary=[];self.boundary_corners=[]
        if not outgoing:raise ValueError('Quad Dome: выбранная поверхность не имеет контура.')
        first=current=min(outgoing)
        while current not in self.boundary:
            if current not in outgoing:raise ValueError('Quad Dome: контур не замкнут.')
            self.boundary.append(current);other,face,j=outgoing[current]
            self.boundary_corners.append((face,j));current=other
        if current!=first or len(self.boundary)!=len(outgoing):raise ValueError('Quad Dome: нужен один контур без отверстий.')
        if not 3<=len(self.boundary)<=128:raise ValueError('Quad Dome: поддерживаются контуры от 3 до 128 сторон.')
        normal=sum((fn.getPolygonNormal(f,om.MSpace.kWorld) for f in self.selected),om.MVector())
        if normal.length()<1e-9:raise ValueError('Quad Dome: поверхность не плоская.')
        self.normal=normal.normal();self.shaders,self.materials=fn.getConnectedShaders(path.instanceNumber())
        selected_materials={self.materials[f] for f in self.selected}
        if len(selected_materials)>1:raise ValueError('Quad Dome: выбранная поверхность должна иметь один материал.')
        self.owner=min(self.selected)
        # Validate the footprint before allocating any Maya preview nodes.
        points=[tuple(self.original[v])[:3] for v in self.boundary]
        if len(points)%2:points.insert(1,tuple((self.original[self.boundary[0]]+ (self.original[self.boundary[1]]-self.original[self.boundary[0]])*.5))[:3])
        self.patch=Patch(points,tuple(self.normal));self.default_height=self.patch.radius*.6

    def validate(self):
        if not self.handle.isValid() or not self.handle.isAlive():raise ValueError('Quad Dome: исходный меш удалён.')
        paths=om.MDagPath.getAllPathsTo(self.handle.object())
        if len(paths)!=1:raise ValueError('Quad Dome: исходный меш стал instance.')
        path=paths[0];fn=om.MFnMesh(path);self.path=path.fullPathName()
        if path.inclusiveMatrix()!=self.matrix or [tuple(p) for p in fn.getPoints(om.MSpace.kWorld)]!=[tuple(p) for p in self.original] or [list(fn.getPolygonVertices(f)) for f in range(fn.numPolygons)]!=self.polygons:
            raise ValueError('Quad Dome: исходная геометрия изменилась. Запустите инструмент заново.')
        return fn

    def topology(self,split_sides):
        points=[om.MPoint(p) for p in self.original];splits={};divided={};boundary=list(self.boundary)
        if len(boundary)%2:
            # Longest rim edge avoids creating another unnecessarily short edge.
            start=max(range(len(boundary)),key=lambda i:(points[boundary[i]]-points[boundary[(i+1)%len(boundary)]]).length())
            a,b=boundary[start],boundary[(start+1)%len(boundary)];edge=tuple(sorted((a,b)))
            top_mid=points[a]+(points[b]-points[a])*.5
            delta=top_mid-om.MPoint(*self.patch.origin)
            xy=np.array([delta*om.MVector(*self.patch.u),delta*om.MVector(*self.patch.v)])
            rho=math.sqrt(float(xy@self.patch.metric@xy))
            correction=(om.MVector(*self.patch.u)*xy[0]+om.MVector(*self.patch.v)*xy[1])*(1/max(rho,1e-12)-1)
            top_axis=(points[b]-points[a]).normal();top_perpendicular=self.normal^top_axis
            edge_length=(points[b]-points[a]).length()
            def midpoint(pair):
                if pair not in splits:
                    p=points[pair[0]]+(points[pair[1]]-points[pair[0]])*.5
                    axis=(points[pair[1]]-points[pair[0]]).normal()
                    if axis*top_axis<0:axis=-axis
                    perpendicular=self.normal^axis
                    if abs(axis*self.normal)<1e-5:
                        ratio=(points[pair[1]]-points[pair[0]]).length()/edge_length
                        p+=(axis*(correction*top_axis)+perpendicular*(correction*top_perpendicular))*ratio
                    splits[pair]=len(points);points.append(p)
                return splits[pair]
            new=midpoint(edge);boundary.insert(start+1,new)
            previous=self.owner;visited=set()
            while split_sides:
                other=[f for f in self.edge_faces[edge] if f not in self.selected and f!=previous]
                if not other:break
                if len(other)!=1:raise ValueError('Quad Dome: нельзя разделить неманифолдную боковину.')
                face=other[0]
                if face in visited:raise ValueError('Quad Dome: замкнутая боковая полоса. Отключите Split Side Strip.')
                visited.add(face);polygon=self.polygons[face]
                if len(polygon)!=4:break
                index=next(i for i in range(4) if tuple(sorted((polygon[i],polygon[(i+1)%4])))==edge)
                v=polygon[index:]+polygon[:index];opposite=tuple(sorted((v[2],v[3])))
                if any(f in self.selected for f in self.edge_faces[opposite]):
                    raise ValueError('Quad Dome: боковая полоса возвращается к крышке. Отключите Split Side Strip.')
                m1,m2=midpoint(edge),midpoint(opposite)
                divided[face]=[[v[0],m1,m2,v[3]],[m1,v[1],v[2],m2]]
                previous=face;edge=opposite
        records=[];owners=[];faces=[]
        reverse_splits={v:edge for edge,v in splits.items()}
        def record(face,vertex):
            polygon=self.polygons[face]
            if vertex in polygon:
                i=polygon.index(vertex);return face,i,i,0.
            a,b=reverse_splits[vertex];return face,polygon.index(a),polygon.index(b),.5
        for face,polygon in enumerate(self.polygons):
            if face in self.selected:continue
            parts=divided.get(face)
            if parts is None:
                part=[]
                for a,b in zip(polygon,polygon[1:]+polygon[:1]):
                    part.append(a);edge=tuple(sorted((a,b)))
                    if edge in splits:part.append(splits[edge])
                parts=[part]
            for part in parts:
                faces.append(part);owners.append(face);records.append([record(face,v) for v in part])
        return points,boundary,faces,owners,records,splits

    def build(self,height,rings,split_sides=True):
        self.validate();world,boundary,faces,owners,records,splits=self.topology(split_sides)
        patch=Patch([tuple(world[v])[:3] for v in boundary],tuple(self.normal))
        cap_points,cap_faces,normals=patch.build(height,rings)
        cap_ids=[boundary[i] for i in patch.order]
        for p in cap_points[len(boundary):]:cap_ids.append(len(world));world.append(om.MPoint(*p))
        first_cap=len(faces);faces.extend([cap_ids[v] for v in face] for face in cap_faces)
        owners.extend([self.owner]*len(cap_faces))
        used={v for face in faces for v in face};selected_vertices={v for f in self.selected for v in self.polygons[f]}
        kept=[v for v in range(len(world)) if v in used or v not in selected_vertices]
        remap={v:i for i,v in enumerate(kept)};vertices=[world[v]*self.inverse for v in kept]
        faces=[[remap[v] for v in face] for face in faces];cap_ids=[remap[v] for v in cap_ids]
        data=om.MFnMeshData().create();out=om.MFnMesh()
        out.create(vertices,[len(f) for f in faces],[v for f in faces for v in f],parent=data)
        if out.numVertices!=len(vertices) or list(out.getVertices()[1])!=[v for f in faces for v in f]:raise ValueError('Quad Dome: Maya изменила построенную топологию.')
        source=om.MFnMesh(self.before)
        old_edges={tuple(sorted(fn)):source.isEdgeSmooth(e) for e in range(source.numEdges) for fn in [source.getEdgeVertices(e)]}
        new_edges={}
        for e,value in old_edges.items():
            if all(v in remap for v in e):new_edges[tuple(sorted(remap[v] for v in e))]=value
            if e in splits:
                middle=remap[splits[e]]
                for vertex in e:new_edges[tuple(sorted((remap[vertex],middle)))]=value
        cap_set=set(cap_ids);smooth=[]
        rim=cap_ids[:len(boundary)]
        rim_edges={tuple(sorted((a,b))) for a,b in zip(rim,rim[1:]+rim[:1])}
        for edge in range(out.numEdges):
            a,b=out.getEdgeVertices(edge);pair=tuple(sorted((a,b)))
            smooth.append(False if height<0 and pair in rim_edges else True if a in cap_set and b in cap_set else new_edges.get(pair,True))
        out.setEdgeSmoothings(list(range(out.numEdges)),smooth);out.cleanupEdgeSmoothing()
        from recurve_normals import NormalTransfer
        transfer=NormalTransfer(source,vertices)
        for face,part in enumerate(records):
            for vertex,(f,a,b,w) in zip(faces[face],part):transfer.add(face,vertex,f,a,b,w)
        transfer.apply(out)
        # Analytic ellipsoid normals remove shading pinches at grid corners.
        # Convex caps share rim normals with the side. A recess has a hard lip:
        # applying its inward normals to the outer side would invert shading.
        normal_map={v:om.MVector(*n)*self.matrix.transpose() for v,n in zip(cap_ids,normals)}
        values=[];normal_faces=[];normal_vertices=[]
        for face,polygon in enumerate(faces):
            for vertex in polygon:
                if vertex in normal_map and (height>0 or face>=first_cap):
                    values.append(normal_map[vertex].normal());normal_faces.append(face);normal_vertices.append(vertex)
        out.setFaceVertexNormals(values,normal_faces,normal_vertices)
        from recurve_uv import assign_uv_sets
        uvsets={}
        projected={v:(((world[kept[v]]-om.MPoint(*patch.origin))*om.MVector(*patch.u))/patch.scale*.5+.5,
                      ((world[kept[v]]-om.MPoint(*patch.origin))*om.MVector(*patch.v))/patch.scale*.5+.5) for v in cap_ids}
        for name in source.getUVSetNames():
            us,vs=map(list,source.getUVs(name));assigned=[];counts=[];cache={}
            for part in records:
                ids=[]
                try:
                    for f,a,b,w in part:
                        ia,ib=source.getPolygonUVid(f,a,name),source.getPolygonUVid(f,b,name)
                        if ia==ib or w==0:ids.append(ia)
                        else:
                            key=(ia,ib)
                            if key not in cache:cache[key]=len(us);us.append((us[ia]+us[ib])*.5);vs.append((vs[ia]+vs[ib])*.5)
                            ids.append(cache[key])
                except RuntimeError:ids=[]
                counts.append(len(ids));assigned.extend(ids)
            ids={}
            for v,(u,t) in projected.items():ids[v]=len(us);us.append(u);vs.append(t)
            for polygon in faces[first_cap:]:counts.append(4);assigned.extend(ids[v] for v in polygon)
            uvsets[name]=(us,vs,counts,assigned)
        data=assign_uv_sets(data,uvsets,source.currentUVSetName() if uvsets else None)
        if source.numColorSets:
            from recurve_colors import ColorTransfer
            colors=ColorTransfer(source)
            for part in records:
                for f,a,b,w in part:colors.add(f,a,b,w)
            for name,record in colors.sets.items():
                # New surface colors inherit the selected boundary's paint.
                boundary_ids=[record['source'][colors.offsets[f]+i] for f,i in self.boundary_corners]
                painted=[record['colors'][i] for i in boundary_ids if i>=0]
                color=tuple(sum(c[i] for c in painted)/len(painted) for i in range(4)) if painted else None
                index=len(record['colors']) if color is not None else -1
                if color is not None:record['colors'].append(color)
                record['ids'].extend([index]*(4*len(cap_faces)))
            data=colors.apply(data)
        return data,list(range(first_cap,len(faces))),owners,patch
