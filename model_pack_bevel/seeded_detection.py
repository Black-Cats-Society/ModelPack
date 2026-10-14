"""Follow a selected fillet using cached local topology and surface samples."""
import math
from maya.api import OpenMaya as om
from slide_for_maya.core import _mesh_fn
from . import bevel_detection as search


class _Lazy(dict):
    def __init__(self,loader):super().__init__();self.loader=loader
    def __missing__(self,key):
        self.loader(key)
        return self[key]


class _DataMesh:
    """World coordinates in a non-DAG mesh owned by MFnMeshData."""
    def __init__(self,fn,name,owner):self.fn=fn;self.name=name;self.owner=owner
    def fullPathName(self):return self.name
    def dagPath(self):return self.owner
    def getPoints(self,space=None):return self.fn.getPoints(om.MSpace.kObject)
    def getPolygonNormal(self,face,space=None):return self.fn.getPolygonNormal(face,om.MSpace.kObject)
    def __getattr__(self,key):return getattr(self.fn,key)


class LocalMesh:
    def __init__(self,mesh):
        self.fn=_mesh_fn(mesh);self.mesh=self.fn.fullPathName()
        self.points=self.fn.getPoints(om.MSpace.kWorld)
        self.polygon=om.MItMeshPolygon(self.fn.dagPath());self.edge=om.MItMeshEdge(self.fn.dagPath())
        self.faces=_Lazy(self._face);self.normals=_Lazy(self._face);self.areas=_Lazy(self._face)
        self.face_edges=_Lazy(self._face);self.neighbors=_Lazy(self._neighbors)
        self.edge_faces=_Lazy(self._edge);self.lengths=_Lazy(self._edge)
        box=self.fn.boundingBox;matrix=self.fn.dagPath().inclusiveMatrix()
        corners=[om.MPoint(x,y,z)*matrix for x in (box.min.x,box.max.x)
                 for y in (box.min.y,box.max.y) for z in (box.min.z,box.max.z)]
        self.scale=max(max(p[i] for p in corners)-min(p[i] for p in corners) for i in range(3))
        self.tolerance=max(self.scale*1e-7,1e-10);self.width_tolerance=max(self.scale*1e-9,1e-12)
        self.regions=[];self.region_for=_Lazy(self._region);self.widths=[]

    def release(self):
        # Lazy dictionaries hold bound loaders back to this snapshot. Break
        # that cycle before returning to Maya so large native mesh data does
        # not accumulate until an unrelated idle-time garbage collection.
        for value in vars(self).values():
            if isinstance(value,_Lazy):value.loader=None

    def _face(self,face):
        self.polygon.setIndex(face);ids=list(self.polygon.getVertices())
        self.faces[face]=ids;self.face_edges[face]=list(self.polygon.getEdges())
        self.normals[face]=search._normal(self.points,ids)
        origin=self.points[ids[0]];area=om.MVector()
        for a,b in zip(ids[1:-1],ids[2:]):area+=(self.points[a]-origin)^(self.points[b]-origin)
        self.areas[face]=area.length()*.5

    def _edge(self,edge):
        self.edge.setIndex(edge);faces=list(self.edge.getConnectedFaces())
        if len(faces)>2:raise ValueError('Поиск фасок требует локальную область без неманифолдных рёбер.')
        self.edge_faces[edge]=faces;a,b=self.fn.getEdgeVertices(edge)
        self.lengths[edge]=(self.points[a]-self.points[b]).length()

    def _neighbors(self,face):
        self.neighbors[face]={f for edge in self.face_edges[face] for f in self.edge_faces[edge] if f!=face}

    def _region(self,first):
        # A supporting surface only needs a local width estimate. Cap the
        # sample so a subdivided floor cannot trigger a full mesh traversal.
        normal=self.normals[first];origin=self.points[self.faces[first][0]]
        patch=set();seen=set();stack=[first]
        while stack and len(patch)<128:
            face=stack.pop()
            if face in seen or face in self.region_for:continue
            seen.add(face)
            if (normal-self.normals[face]).length()>1e-5:continue
            if face!=first and any(abs((self.points[v]-origin)*normal)>self.tolerance for v in self.faces[face]):continue
            patch.add(face);stack.extend(sorted(self.neighbors[face]-seen,reverse=True))
        region=len(self.regions);self.regions.append(patch)
        self.region_for.update((f,region) for f in patch)
        self.widths.append(search._patch_width(self,patch))

    def chains(self,seed):
        faces={seed};es=self.face_edges[seed]
        if len(es)!=4:return faces
        for i in range(4):
            previous=seed;edge=es[i];seen={seed};flat=0;turn=0.
            width=sum(self.lengths[e] for j,e in enumerate(es) if j%2!=i%2)*.5
            while True:
                following=[f for f in self.edge_faces[edge] if f!=previous]
                if len(following)!=1:break
                face=following[0]
                if face in seen:break
                seen.add(face);edges=self.face_edges[face];faces.add(face)
                if len(edges)!=4:break
                index=edges.index(edge)
                step=sum(self.lengths[e] for j,e in enumerate(edges) if j%2!=index%2)*.5
                if step>max(width*8,self.width_tolerance):break
                angle=math.acos(max(-1.,min(1.,self.normals[previous]*self.normals[face])))
                flat=flat+1 if angle<1e-5 else 0;turn+=angle
                if flat>=3 or turn>math.pi:break
                previous=face;edge=edges[(index+2)%4]
        return faces


def walk_profile(data,section,incoming):
    """Continue the profile's quad direction without re-inferring it per cell."""
    faces={s[0] for s in section};initial=set(faces)
    for side in (1,3):
        row=list(section);edges=list(incoming);previous_step=None
        while True:
            following=[]
            for state,edge in zip(row,edges):
                es=data.face_edges[state[0]];cross=es[(es.index(edge)+side)%4]
                others=[f for f in data.edge_faces[cross] if f!=state[0]]
                if len(others)!=1 or len(data.face_edges[others[0]])!=4:break
                f=others[0];following.append((f,1-data.face_edges[f].index(cross)%2))
            else:
                new={s[0] for s in following}
                if len(new)!=len(row) or new & faces:break
                width=lambda s:sum(data.lengths[e] for i,e in enumerate(data.face_edges[s[0]]) if i%2!=s[1])*.5
                if any(not .3<width(b)/max(width(a),data.width_tolerance)<3 for a,b in zip(row,following)):break
                if len(row)<=4:
                    middle=lambda states:sum((sum((om.MVector(data.points[v]) for v in data.faces[s[0]]),om.MVector())/4 for s in states),om.MVector())/len(states)
                    step=middle(following)-middle(row)
                    if previous_step is not None and step.normal()*previous_step.normal()<.5:break
                    previous_step=step
                shared=[next((e for e in data.face_edges[a[0]] if b[0] in data.edge_faces[e]),None) for a,b in zip(following,following[1:])]
                if any(e is None for e in shared):break
                if shared:
                    es=data.face_edges[following[0][0]];next_edges=[es[(es.index(shared[0])+2)%4]]+shared
                else:
                    es=data.face_edges[following[0][0]]
                    cross=next(e for e in es if row[0][0] in data.edge_faces[e])
                    next_edges=[es[(es.index(cross)+2-side)%4]]
                # Stop where the transverse transition turns into a parent
                # wall. At intersections the face normals can all be equal;
                # actual cross-section geometry must supply this check.
                if len(row)>1:
                    def bend(states,ins):
                        path=[data.fn.getEdgeVertices(ins[0])[0]]
                        for s,e in zip(states,ins):
                            es=data.face_edges[s[0]];out=es[(es.index(e)+2)%4];vs=data.faces[s[0]]
                            i=vs.index(path[-1]);pair=set(data.fn.getEdgeVertices(out))
                            choices=pair & {vs[i-1],vs[(i+1)%4]}
                            if len(choices)!=1:return 0
                            path.append(choices.pop())
                        vectors=[(data.points[b]-data.points[a]).normal() for a,b in zip(path,path[1:])]
                        return sum(math.acos(max(-1.,min(1.,a*b))) for a,b in zip(vectors,vectors[1:]))
                    before=bend(row,edges);after=bend(following,next_edges)
                    if before>.05 and not .4<after/before<2.5:break
                faces.update(new);row=following;edges=next_edges;continue
            break
    return faces


def detect_seeded(mesh,seeds,*,include_corners=True,_data=None):
    data=LocalMesh(mesh) if _data is None else _data
    data.reusable=set()
    if not seeds or any(f<0 or f>=data.fn.numPolygons for f in seeds):
        raise ValueError('Выберите существующий полигон фаски.')
    result=[];covered=set()
    for seed in sorted(seeds):
        if seed in covered:continue
        quad_seeds={seed} if len(data.faces[seed])==4 else {f for f in data.neighbors[seed] if len(data.faces[f])==4}
        bands=[]
        probes=set(quad_seeds)
        for _ in range(2):probes.update(f for q in tuple(probes) for f in data.neighbors[q] if len(data.faces[f])==4)
        if quad_seeds and all((data.normals[q]-data.normals[seed]).length()<1e-5 for q in probes):
            def parallelogram(q):
                a,b,c,d=(data.points[v] for v in data.faces[q])
                return ((a-b)+(c-d)).length()<max((a-b).length(),(b-c).length(),1e-8)*1e-5
            if all(parallelogram(q) for q in probes):continue
        for index,quad in enumerate(sorted(probes)):
            search._advance(.05+.15*index/max(len(probes),1),'Поиск исходной фаски')
            search._ribbon_faces(data,'extended',bands,junctions=True,seeds={quad},face_ids=data.chains(quad))
        scope=set().union(*bands) if bands else set(probes)
        scope.add(seed)
        for _ in range(3):scope.update(f for q in tuple(scope) for f in data.neighbors[q])
        ids=sorted(scope);vertices=sorted({v for f in ids for v in data.faces[f]})
        mapping={v:i for i,v in enumerate(vertices)}
        owner=om.MFnMeshData().create();fn=om.MFnMesh()
        fn.create([data.points[v] for v in vertices],[len(data.faces[f]) for f in ids],
                  [mapping[v] for f in ids for v in data.faces[f]],parent=owner)
        fn=om.MFnMesh(owner)
        snapshot=search._Mesh(mesh,_DataMesh(fn,data.mesh,owner));snapshot.source_faces=ids;snapshot.source_data=data
        found=search.detect(mesh,seeds={ids.index(seed)},use_history=False,include_corners=include_corners,_data=snapshot)
        nearby=search.detect(mesh,use_history=False,include_corners=include_corners,_data=snapshot)
        search._advance(.8,'Проверка выделенной фаски')
        if not any(ids[f]==seed for g in found.groups for f in g.faces):found=nearby
        local_seed=ids.index(seed) if seed in scope else None
        thin_choices=[]
        if local_seed is not None and (local_seed not in snapshot._base_geometric or not any(local_seed in g.faces for g in found.groups)):
            thin=search._thin_strip_faces(snapshot,'extended')
            for p in search._components(thin,snapshot.neighbors):
                mapped={ids[f] for f in p}
                mapped=search._attach_fans(data,mapped) if include_corners else mapped
                if seed in mapped:thin_choices.append(mapped)
        if len(data.faces[seed])==3 or not thin_choices and not any(ids[f]==seed for g in found.groups for f in g.faces):
            thin=search._thin_strip_faces(data,'extended',face_ids=scope)
            for p in search._components(thin,data.neighbors):
                patch=search._attach_fans(data,p) if include_corners else p
                if seed in patch:thin_choices.append(patch)
        if thin_choices and (not any(ids[f]==seed for g in found.groups for f in g.faces) or
                             any(len(data.faces[f])==3 for p in thin_choices for f in p)):
            patch=min(thin_choices,key=lambda p:len(p));group=search._group(data,patch,'seed')
            result.append(group);covered.update(patch);continue
        classified={ids[f] for f in nearby.faces}
        memberships={}
        for group in nearby.groups:
            for face in group.faces:memberships[face]=memberships.get(face,0)+1
        reusable={ids[f] for f,count in memberships.items() if count==1}
        candidates=[]
        for group in found.groups:
            patch={ids[f] for f in group.faces}
            complete=search._extend_band(data,patch,scope-classified,junctions=True)
            if complete<=classified and search._group(data,complete,'seed').ready:patch=complete
            final=search._extend_band(data,patch,set(),junctions=True)
            if patch<final:
                try:
                    _,rows,_=search._strip(data,patch)
                    if len(final)-len(patch)<=len(rows[0])-1 and search._group(data,final,'seed').ready:patch=final
                except ValueError:pass
            if seed in patch:
                candidates.append(search.BevelGroup(data.mesh,tuple(sorted(patch)),'seed',group.ready,group.reason))
        if candidates:
            group=min(candidates,key=lambda g:(not g.ready,-len(g.faces)))
            data.reusable.update(set(group.faces)&reusable)
            result.append(group);covered.update(group.faces);continue
    return search.Detection(tuple(result))
