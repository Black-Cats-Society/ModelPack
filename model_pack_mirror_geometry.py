"""Linear polygon clipping and reflection, with welding restricted to the seam."""
import math
from maya.api import OpenMaya as om


def build(source,source_data,axis,space,direction,pivot,threshold,matrix,backend=None,pivot_in_frame=False):
    mesh_fn=backend.Mesh if backend else om.MFnMesh
    data_fn=backend.Data if backend else om.MFnMeshData
    component_fn=backend.Component if backend else om.MFnSingleIndexedComponent
    fn=mesh_fn(source);points=fn.getPoints();inverse=matrix.inverse()
    transformed=matrix!=om.MMatrix()
    frame=[]
    for point in points:
        converted=point*matrix if transformed else point
        frame.append((converted.x,converted.y,converted.z))
    if space==1 and transformed and not pivot_in_frame:
        center=om.MPoint(*pivot)*matrix;pivot=(center.x,center.y,center.z)
    if threshold>0:
        frame=[tuple(pivot[axis] if i==axis and abs(value-pivot[axis])<=threshold*.5 else value
                     for i,value in enumerate(p)) for p in frame]
    counts,flat=fn.getVertices();polygons=[];corner_offsets=[];ptr=0;edge_faces={}
    for count in counts:
        polygon=list(flat[ptr:ptr+count]);corner_offsets.append(ptr);ptr+=count;polygons.append(polygon)
        for a,b in zip(polygon,polygon[1:]+polygon[:1]):
            key=(min(a,b),max(a,b));edge_faces[key]=edge_faces.get(key,0)+1
    boundary={v for pair,n in edge_faces.items() if n==1 for v in pair}
    distance=[(p[axis]-pivot[axis])*(1 if direction else -1) for p in frame]
    epsilon=max(1e-10,max((abs(d) for d in distance),default=1)*1e-10)
    positions=[];registry={};seam=set();output_faces=[];owners=[];descriptions=[];copies=[]
    seam_hash={};flat_axes=[i for i in range(3) if i!=axis]

    def vertex(desc,polygon,mirrored):
        token,i,j,t=desc;key=(token,mirrored)
        if key in registry:return registry[key]
        a,b=polygon[i],polygon[j]
        p=list(frame[a]) if i==j else [x+(y-x)*t for x,y in zip(frame[a],frame[b])]
        welded=threshold>0 and (i!=j or (a in boundary or abs(distance[a])<=epsilon)) and abs(p[axis]-pivot[axis])<=max(threshold*.5,epsilon)
        if welded:p[axis]=pivot[axis]
        if mirrored and welded and (token,False) in registry:
            registry[key]=registry[(token,False)];return registry[key]
        if mirrored:p[axis]=2*pivot[axis]-p[axis]
        cell=None
        if welded:
            cell=tuple(math.floor(p[i]/threshold) for i in flat_axes)
            for x in (-1,0,1):
                for y in (-1,0,1):
                    for candidate,q in seam_hash.get((cell[0]+x,cell[1]+y),()):
                        if sum((a-b)**2 for a,b in zip(p,q))<=threshold**2:
                            registry[key]=candidate;return candidate
        point=om.MPoint(*p)
        if transformed:point*=inverse
        index=len(positions);positions.append(point);registry[key]=index
        if welded:
            seam.add(index);seam_hash.setdefault(cell,[]).append((index,p))
        return index

    for face,polygon in enumerate(polygons):
        pieces=[list(range(len(polygon)))]
        transitions=sum((distance[a]>epsilon)!=(distance[b]>epsilon) for a,b in zip(polygon,polygon[1:]+polygon[:1]))
        if transitions>2 and min(distance[v] for v in polygon)<-epsilon:
            # A concave n-gon can cross the plane in several disjoint pieces.
            local={v:i for i,v in enumerate(polygon)}
            pieces=[[local[v] for v in fn.getPolygonTriangleVertices(face,t)]
                    for t in range(fn.getTriangles()[0][face])]
        for piece in pieces:
            clipped=[]
            for i,j in zip(piece[-1:]+piece[:-1],piece):
                a,b=polygon[i],polygon[j];da,db=distance[a],distance[b]
                inside_a,inside_b=da>=-epsilon,db>=-epsilon
                if inside_a!=inside_b:
                    t=da/(da-db)
                    if t<1e-10:desc=(('v',a),i,i,0.)
                    elif t>1-1e-10:desc=(('v',b),j,j,0.)
                    else:desc=(('e',min(a,b),max(a,b)),i,j,t)
                    if not clipped or clipped[-1][0]!=desc[0]:clipped.append(desc)
                if inside_b:
                    desc=(('v',b),j,j,0.)
                    if not clipped or clipped[-1][0]!=desc[0]:clipped.append(desc)
            if len(clipped)>1 and clipped[0][0]==clipped[-1][0]:clipped.pop()
            if len(clipped)<3:continue
            coplanar=all(abs(distance[polygon[d[1]]])<=epsilon and abs(distance[polygon[d[2]]])<=epsilon for d in clipped)
            for mirrored in ((False,) if coplanar else (False,True)):
                descs=list(reversed(clipped)) if mirrored else clipped
                ids=[vertex(desc,polygon,mirrored) for desc in descs]
                # Large weld tolerances can collapse corners at the border.
                clean=[(v,d) for index,(v,d) in enumerate(zip(ids,descs)) if v!=ids[index-1]]
                if len(clean)<3:continue
                output_faces.append([v for v,_ in clean]);descriptions.append([d for _,d in clean])
                owners.append(face);copies.append(mirrored)
    data=data_fn().create();result=mesh_fn()
    result.create(positions,[len(p) for p in output_faces],[v for p in output_faces for v in p],parent=data)
    for uvset in fn.getUVSetNames():
        u,v=fn.getUVs(uvset);uvcounts,uvids=fn.getAssignedUVs(uvset)
        offsets=[];ptr=0
        for count in uvcounts:offsets.append(ptr);ptr+=count
        pool_u=list(u);pool_v=list(v);added={};assigned=[];assigned_counts=[]
        for owner,descs,mirrored in zip(owners,descriptions,copies):
            if uvcounts[owner]!=len(polygons[owner]):assigned_counts.append(0);continue
            assigned_counts.append(len(descs));offset=offsets[owner]
            for token,i,j,t in descs:
                ua,ub=uvids[offset+i],uvids[offset+j]
                if i==j and not mirrored:index=ua
                else:
                    key=(mirrored,token,ua,ub,round(t,12))
                    if key not in added:
                        added[key]=len(pool_u);pool_u.append(u[ua]+(u[ub]-u[ua])*t);pool_v.append(v[ua]+(v[ub]-v[ua])*t)
                    index=added[key]
                assigned.append(index)
        if uvset not in result.getUVSetNames():result.createUVSet(uvset)
        result.setUVs(pool_u,pool_v,uvset);result.assignUVs(assigned_counts,assigned,uvset)
    # Preserve smoothing away from the newly joined seam.
    original_smooth={};incident={};supports={}
    for edge in range(fn.numEdges):
        a,b=fn.getEdgeVertices(edge)
        pair=(min(a,b),max(a,b));original_smooth[pair]=fn.isEdgeSmooth(edge)
        incident.setdefault(a,set()).add(pair);incident.setdefault(b,set()).add(pair)
    for (token,mirrored),vertex_id in registry.items():
        edges=incident[token[1]] if token[0]=='v' else {token[1:]}
        supports.setdefault(vertex_id,set()).update(edges)
    neighbors={}
    for face,polygon in enumerate(output_faces):
        for a,b in zip(polygon,polygon[1:]+polygon[:1]):neighbors.setdefault((min(a,b),max(a,b)),[]).append(face)
    for edge in range(result.numEdges):
        a,b=result.getEdgeVertices(edge);pair=(min(a,b),max(a,b))
        original_edges=supports.get(a,set()) & supports.get(b,set())
        soft=all(original_smooth[e] for e in original_edges)
        faces=neighbors[pair]
        if not original_edges and len(faces)==2:
            soft=result.getPolygonNormal(faces[0])*result.getPolygonNormal(faces[1])>=math.cos(math.radians(30))-1e-8
        result.setEdgeSmoothing(edge,soft)
    result.cleanupEdgeSmoothing()
    if backend is not None:
        locked=fn.lockedNormals()
        if locked is not None:
            flags,normals,normal_ids=locked;values=[];faces=[];vertices=[]
            for face,(owner,descs,mirrored,polygon) in enumerate(zip(owners,descriptions,copies,output_faces)):
                start=corner_offsets[owner]
                for (_,i,j,t),vertex_id in zip(descs,polygon):
                    a,b=normal_ids[start+i],normal_ids[start+j]
                    if not (flags[a] or flags[b]):continue
                    normal=(normals[a]*(1-t)+normals[b]*t).normal()
                    if mirrored:
                        if transformed:normal*=inverse.transpose()
                        normal[axis]*=-1
                        if transformed:normal*=matrix.transpose()
                    values.append(normal.normal());faces.append(face);vertices.append(vertex_id)
            result.setLockedNormals(values,faces,vertices)
        for name in fn.colorSets():
            source_colors=fn.colors(name);colors=[];faces=[];vertices=[]
            for face,(owner,descs,polygon) in enumerate(zip(owners,descriptions,output_faces)):
                start=corner_offsets[owner]
                for (_,i,j,t),vertex_id in zip(descs,polygon):
                    colors.append([a+(b-a)*t for a,b in zip(source_colors[start+i],source_colors[start+j])])
                    faces.append(face);vertices.append(vertex_id)
            result.setColors(name,colors,faces,vertices)
    # Face membership follows the original face through clipping and mirroring.
    src_groups=data_fn(source_data);dst_groups=data_fn(data)
    dst_groups.copyObjectGroups(source_data)
    for index in range(src_groups.objectGroupCount):
        group=src_groups.objectGroup(index)
        comp=src_groups.objectGroupComponent(group)
        if not comp.isNull() and comp.apiType()==om.MFn.kMeshPolygonComponent:
            original=component_fn(comp)
            ids=None if original.isComplete else set(original.getElements())
            target=component_fn();component=target.create(om.MFn.kMeshPolygonComponent)
            target.addElements([f for f,owner in enumerate(owners) if ids is None or owner in ids])
            dst_groups.setObjectGroupComponent(group,component)
    return data
