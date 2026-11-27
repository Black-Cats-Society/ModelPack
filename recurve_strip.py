"""Resample only the selected cylinder path; preserve neighbouring rings."""
import math
import bisect
import maya.api.OpenMaya as om


def circle(points,ordered=True):
    if len(points)<3:return None
    # Widely spaced samples avoid an ill-conditioned fit on dense rings.
    # On a closed ring, first/second/last are three almost adjacent vertices.
    p0=om.MVector(*points[0]);a=om.MVector(*points[len(points)//3])-p0
    b=om.MVector(*points[(2*len(points))//3])-p0;n=a^b
    if n.length()<1e-10:return None
    center=p0+((n^a)*(b*b)+(b^n)*(a*a))/(2*(n*n))
    radius=(p0-center).length()
    if radius<1e-10:return None
    normal=n.normal();u=(p0-center).normal();v=normal^u
    tol=max(1e-6,radius*1e-5)
    angles=[]
    for xyz in points:
        q=om.MVector(*xyz)-center
        if abs(q.length()-radius)>tol or abs(q*normal)>tol:return None
        angle=math.atan2(q*v,q*u)
        if ordered and angles:
            while angle<angles[-1]-1e-7:angle+=2*math.pi
        angles.append(angle)
    if ordered and angles[-1]>2*math.pi+1e-6:return None
    return center,radius,normal,u,v,angles


def line(points):
    start=om.MVector(*points[0]);end=om.MVector(*points[-1]);direction=(end-start).normal()
    if (end-start).length()<1e-10:return None
    distances=[]
    for xyz in points:
        q=om.MVector(*xyz)-start;t=q*direction
        if (q-direction*t).length()>1e-6:return None
        if distances and t<=distances[-1]+1e-10:return None
        distances.append(t)
    return start,0,None,direction,None,distances


def supported_circle(fn,source,row,closed):
    fit=circle([tuple(fn.getPoint(v))[:3] for v in row])
    if fit is None or closed or len(row)>=4:return fit
    # Every three non-collinear points define a circle. Require a fourth
    # neighbouring point on that same circle before treating a short arc as one.
    center,radius,normal,_,_,_=fit
    tol=max(1e-6,radius*1e-5)
    it=om.MItMeshVertex(source)
    for vertex in (row[0],row[-1]):
        it.setIndex(vertex)
        for other in it.getConnectedVertices():
            if other in row:continue
            q=om.MVector(fn.getPoint(other))-center
            if abs(q*normal)<=tol and abs(q.length()-radius)<=tol:return fit
    return None


def short_arc(fn,source,row,axis,reference=None):
    it=om.MItMeshVertex(source);it.setIndex(row[0])
    points=[tuple(fn.getPoint(v))[:3] for v in row]
    for vertex in it.getConnectedVertices():
        if vertex in row:continue
        p=fn.getPoint(vertex)
        if abs((om.MVector(p.x,p.y,p.z)-om.MVector(*points[0]))*axis)>1e-6:continue
        if reference is None:
            # The initial circle must use a neighbour on the cylinder wall,
            # not a radial cap edge or cap centre.
            a=om.MItMeshVertex(source);a.setIndex(vertex)
            shared=set(it.getConnectedFaces())&set(a.getConnectedFaces())
            if not any(len(fn.getPolygonVertices(f))==4 and abs(fn.getPolygonNormal(f)*axis)<1e-5 for f in shared):continue
        fit=circle(points+[tuple(p)[:3]])
        if fit is None or abs(fit[2]*axis)<1-1e-5:continue
        if reference is not None:
            delta=fit[0]-reference[0]
            if (delta-axis*(delta*axis)).length()>1e-5:continue
        center,radius,normal,u,v,_=fit
        angles=[0.0]
        for xyz in points[1:]:
            q=om.MVector(*xyz)-center;angle=math.atan2(q*v,q*u)
            if angle<0:angle+=2*math.pi
            angles.append(angle)
        return center,radius,normal,u,v,angles
    return None


def discover(fn,source,first,closed, *, neighbors_only=False):
    cache={};it=om.MItMeshVertex(source)
    def faces_at(vertex):
        if vertex not in cache:
            it.setIndex(vertex);cache[vertex]=set(it.getConnectedFaces())
        return cache[vertex]
    rows=[list(first)];seen={tuple(first):0};used=set();bands=[]
    index=0
    while index<len(rows):
        row=rows[index];pairs=list(zip(row,row[1:]+row[:1] if closed else row[1:]))
        row_vertices=set(row)
        choices=[]
        for a,b in pairs:
            options=[]
            for f in sorted(faces_at(a)&faces_at(b)-used):
                vs=list(fn.getPolygonVertices(f))
                if len(vs)!=4:continue
                j=vs.index(a)
                if vs[(j+1)%4]==b:op=(vs[(j+3)%4],vs[(j+2)%4])
                elif vs[(j-1)%4]==b:op=(vs[(j+1)%4],vs[(j+2)%4])
                else:continue
                if op[0] not in row_vertices and op[1] not in row_vertices:options.append((f,op[0],op[1]))
            choices.append(options)
        if choices and all(choices):
            for seed in choices[0]:
                path=[seed]
                for opts in choices[1:]:
                    matching=[o for o in opts if o[1]==path[-1][2]]
                    if len(matching)!=1:break
                    path.append(matching[0])
                if len(path)!=len(choices):continue
                if closed and path[-1][2]!=path[0][1]:continue
                opposite=[path[0][1]]+[p[2] for p in path]
                if closed:opposite.pop()
                if len(set(opposite))!=len(opposite):continue
                key=tuple(opposite)
                if key not in seen:
                    seen[key]=len(rows);rows.append(opposite)
                other=seen[key]
                fids=[p[0] for p in path]
                if any(f in used for f in fids):continue
                used.update(fids);bands.append((index,other,fids))
        if neighbors_only:
            break
        index+=1
        if len(rows)>2048:raise ValueError('Circular strip is too long for this operation.')
    return rows,bands


def rebuild_strip(source,chain,count,connect,triangulate,group=None,interpolate=None):
    fn=om.MFnMesh(source);closed=chain[0]==chain[-1]
    root=chain[:-1] if closed else chain
    if closed and count<3:raise ValueError('A closed ring needs at least 3 segments.')
    # Only use this solver for circular sections; general chains use the other solver.
    if group is not None:
        return rebuild_group(source,group,count,connect,triangulate,interpolate)
    initial=supported_circle(fn,source,root,closed)
    initial_line=line([tuple(fn.getPoint(v))[:3] for v in root])
    if initial is None and initial_line is None:return None
    rows,bands=discover(fn,source,root,closed)
    if not bands and initial is None:return None
    if initial is not None:
        fits=[circle([tuple(fn.getPoint(v))[:3] for v in row]) for row in rows]
    else:
        # A longitudinal edge on a cylinder: parallel lines arranged on a circle.
        cross=circle([tuple(fn.getPoint(row[0]))[:3] for row in rows],ordered=False)
        if cross is not None and abs(cross[2]*initial_line[3])>1-1e-5:
            fits=[line([tuple(fn.getPoint(v))[:3] for v in row]) for row in rows]
        elif len(root)==2:
            axes=[(om.MVector(fn.getPoint(row[0]).x,fn.getPoint(row[0]).y,fn.getPoint(row[0]).z)-
                   om.MVector(fn.getPoint(root[0]).x,fn.getPoint(root[0]).y,fn.getPoint(root[0]).z)).normal() for row in rows[1:]]
            initial=next((fit for axis in axes if abs(axis*initial_line[3])<1e-5
                          for fit in [short_arc(fn,source,root,axis)] if fit is not None),None)
            if initial is None:return None
            fits=[short_arc(fn,source,row,initial[2],initial) for row in rows]
        else:return None
    if any(fit is None for fit in fits):return None
    # Neighbouring rows establish the cylinder's geometry only. They are not
    # selected, so their vertices and edges must remain untouched. Transition
    # faces use the same local corner remapping as other adjacent polygons.
    rows = [root]
    fits = [fits[0]]
    bands = []
    return build_rows(source,rows,fits,bands,count,connect,triangulate,closed=closed)


def build_rows(source,rows,fits,bands,count,connect,triangulate,curves=None,fixed=None,closed=False):
    fn=om.MFnMesh(source)
    fixed=set() if fixed is None else fixed
    preserve=set(fixed)
    if curves is not None:
        preserve.update(r for r,row in enumerate(rows) if len(row)==(count if closed else count+1))
    oldpoints=[tuple(p)[:3] for p in fn.getPoints()]
    excluded={v for r,row in enumerate(rows) if r not in preserve for v in row}
    points=[p for i,p in enumerate(oldpoints) if i not in excluded]
    mapping={v:i for i,v in enumerate(v for v in range(fn.numVertices) if v not in excluded)}
    samples=[];params=[];buckets=[];spans={}
    for r,(row,fit) in enumerate(zip(rows,fits)):
        center,radius,normal,u,v,angles=fit
        total=2*math.pi if closed else angles[-1]
        if total<1e-10:return None
        pp=[angle/total*count for angle in angles]
        if closed:pp.append(float(count))
        if r in preserve:pp=[float(i) for i in range(count+1)]
        # Face correspondence uses each row's original parameterization.
        params.append(pp)
        bb=[int(math.floor(p+0.5)) for p in pp]
        buckets.append(bb)
        ids=[]
        for k in range(count if closed else count+1):
            angle=total*k/count
            xyz=center+u*angle if normal is None else center+(u*math.cos(angle)+v*math.sin(angle))*radius
            p=tuple(xyz)
            if k==0:p=oldpoints[row[0]]
            if not closed and k==count:p=oldpoints[row[-1]]
            if curves is not None and r not in fixed:p=curves[r][k]
            if r in preserve:
                ids.append(mapping[row[k]])
            else:
                ids.append(len(points));points.append(p)
        samples.append(ids)
        for i,original in enumerate(row):mapping[original]=ids[bb[i]%len(ids)]
        pairs=zip(row,row[1:]+row[:1] if closed else row[1:])
        if r not in preserve:
            for i,(a,b) in enumerate(pairs):
                spans[a,b]=(r,i,False);spans[b,a]=(r,i,True)
    band_start={};band_faces=set()
    # Corresponding rows always share cross edges. The mode controls only
    # extra splits of adjacent transition/cap faces, not these quad bands.
    for ra,rb,fids in bands:
        band_start[min(fids)]=(ra,rb,fids)
        band_faces.update(fids)
    faceparts=[]
    owners=[]
    counts=om.MIntArray()
    indices=om.MIntArray()

    def add_face(part, owner):
        faceparts.append(part)
        owners.append(owner)
        counts.append(len(part))
        for corner in part:
            indices.append(corner[0])
    # Record = new vertex, original face, two original face-corner indices, weight.
    def record(r,k,f,i=None):
        pp=params[r]
        if i is None:i=min(len(pp)-2,max(0,bisect.bisect_right(pp,k)-1))
        row=rows[r];a,b=row[i],row[(i+1)%len(row)]
        vs=list(fn.getPolygonVertices(f))
        w=max(0,min(1,(k-pp[i])/(pp[i+1]-pp[i])))
        return samples[r][k%len(samples[r])],f,vs.index(a),vs.index(b),w
    for f in range(fn.numPolygons):
        if f in band_faces:
            if f not in band_start:continue
            ra,rb,fids=band_start[f]
            first=next(face for face in fids if rows[ra][0] in fn.getPolygonVertices(face) and rows[ra][1] in fn.getPolygonVertices(face))
            vs=list(fn.getPolygonVertices(first));j=vs.index(rows[ra][0])
            forward=vs[(j+1)%len(vs)]==rows[ra][1]
            def source_span(r,t,end=False):
                pp=params[r];row=rows[r]
                search=bisect.bisect_left if end else bisect.bisect_right
                i=min(len(pp)-2,max(0,search(pp,t)-1))
                a,b=row[i],row[(i+1)%len(row)]
                owner=next((face for face in fids if a in fn.getPolygonVertices(face) and b in fn.getPolygonVertices(face)),None)
                if owner is None:raise ValueError('Cannot find a source face for the strip boundary.')
                return i,owner
            def rec(r,t,end=False):
                i,owner=source_span(r,t,end)
                return record(r,t,owner,i)
            for k in range(count):
                part=[rec(ra,k),rec(ra,k+1,True),rec(rb,k+1,True),rec(rb,k)]
                if not forward:part.reverse()
                add_face(part, source_span(ra,k+0.5)[1])
            continue
        vs=list(fn.getPolygonVertices(f))
        if excluded.isdisjoint(vs):
            # UV/color/normal correspondence is the identity for this face.
            # Reconstruct it only when transferring attributes, one face at a time.
            faceparts.append(None)
            owners.append(f)
            counts.append(len(vs))
            for vertex in vs:
                indices.append(mapping[vertex])
            continue
        corners=[]
        for j,a in enumerate(vs):
            b=vs[(j+1)%len(vs)]
            corners.append((mapping[a],f,j,j,0.0))
            if (a,b) in spans:
                r,i,reverse=spans[a,b];pp=params[r];bb=buckets[r]
                ks=list(range(bb[i]+1,bb[i+1]))
                if reverse:ks.reverse()
                for k in ks:
                    w=(k-pp[i])/(pp[i+1]-pp[i])
                    if reverse:w=1-w
                    corners.append((samples[r][k%len(samples[r])],f,j,(j+1)%len(vs),max(0,min(1,w))))
        clean=[]
        for c in corners:
            if not clean or c[0]!=clean[-1][0]:clean.append(c)
        if len(clean)>1 and clean[0][0]==clean[-1][0]:clean.pop()
        if len(clean)<3:continue
        if len({c[0] for c in clean})!=len(clean):raise ValueError('Circular strip would create a pinched face.')
        split=connect and len(clean)>3 and (len(clean)>4 or any(c[2]!=c[3] for c in clean)) and excluded.intersection(vs)
        if split:
            reduced=[(c[0],c[2],c[3],c[4]) for c in clean]
            for part in triangulate(reduced,points):
                add_face([(a,f,b,c,w) for a,b,c,w in part], f)
        else:
            add_face(clean, f)
    data=om.MFnMeshData().create();out=om.MFnMesh()
    out.create([om.MPoint(*p) for p in points],counts,indices,parent=data)
    actual = out.getVertices()[1]
    if out.numVertices!=len(points) or len(actual)!=len(indices) or any(a != b for a, b in zip(actual, indices)):
        raise ValueError('Maya would change unrelated topology in the circular strip.')
    # Only store hard edges. A pair
    # encoded as an integer avoids one frozenset per edge on dense meshes.
    stride = out.numVertices
    def edge_key(a, b):
        return a * stride + b if a <= b else b * stride + a
    hard = set()
    support = {(min(rows[a][0], rows[b][0]), max(rows[a][0], rows[b][0])): True
               for a, b, _ in bands}
    column_keys = {edge_key(a, b) for ra, rb, _ in bands
                   for a, b in zip(samples[ra], samples[rb])}
    inherited_columns = set()
    for edge in range(fn.numEdges):
        a, b = fn.getEdgeVertices(edge)
        smooth = fn.isEdgeSmooth(edge)
        key = edge_key(mapping[a], mapping[b])
        if smooth:
            hard.discard(key)
        else:
            hard.add(key)
        if key in column_keys:
            inherited_columns.add(key)
        if support:
            pair = (min(a, b), max(a, b))
            if pair in support:
                support[pair] = smooth
        if (a, b) in spans:
            r, i, _ = spans[a, b]
            ids = samples[r]
            for k in range(buckets[r][i], buckets[r][i+1]):
                key = edge_key(ids[k % len(ids)], ids[(k+1) % len(ids)])
                (hard.discard if smooth else hard.add)(key)
                if key in column_keys:
                    inherited_columns.add(key)
    for ra, rb, _ in bands:
        a, b = rows[ra][0], rows[rb][0]
        if not support[min(a,b), max(a,b)]:
            hard.update(edge_key(x,y) for x,y in zip(samples[ra],samples[rb])
                        if edge_key(x,y) not in inherited_columns)
    # Set both states explicitly since newly created Maya meshes can have hard edges.
    out.setEdgeSmoothings(range(out.numEdges),
                          [edge_key(*out.getEdgeVertices(e)) not in hard for e in range(out.numEdges)])
    out.cleanupEdgeSmoothing()
    uvsets=fn.getUVSetNames();uvcoords={name:[list(a) for a in fn.getUVs(name)] for name in uvsets}
    uvcounts={name:[] for name in uvsets};uvids={name:[] for name in uvsets};cache={name:{} for name in uvsets}
    from recurve_normals import NormalTransfer
    normal_transfer=NormalTransfer(fn,points)
    from recurve_colors import ColorTransfer
    color_transfer=ColorTransfer(fn)
    for target,part in enumerate(faceparts):
        if part is None:
            f = owners[target]
            part = [(mapping[v], f, j, j, 0.0) for j, v in enumerate(fn.getPolygonVertices(f))]
        for vid,f,a,b,w in part:
            normal_transfer.add(target,vid,f,a,b,w)
            color_transfer.add(f,a,b,w)
        for name in uvsets:
            ids=[]
            try:
                for vid,f,a,b,w in part:
                    ia=fn.getPolygonUVid(f,a,name);ib=fn.getPolygonUVid(f,b,name)
                    if a==b or w<=0:idx=ia
                    elif w>=1:idx=ib
                    else:
                        key=(ia,ib,round(w,12)) if ia<=ib else (ib,ia,round(1-w,12))
                        if key not in cache[name]:
                            us,vs=uvcoords[name];cache[name][key]=len(us)
                            us.append(us[ia]+(us[ib]-us[ia])*w);vs.append(vs[ia]+(vs[ib]-vs[ia])*w)
                        idx=cache[name][key]
                    ids.append(idx)
            except RuntimeError:ids=[]
            uvcounts[name].append(len(ids));uvids[name].extend(ids)
    normal_transfer.apply(out)
    from recurve_uv import assign_uv_sets
    data=assign_uv_sets(data,{name:(us,vs,uvcounts[name],uvids[name]) for name,(us,vs) in uvcoords.items()},
                        fn.currentUVSetName())
    from recurve import transfer_face_groups
    data=transfer_face_groups(source,color_transfer.apply(data),owners)
    chosen=[ids+ids[:1] if closed else ids for r,ids in enumerate(samples) if r not in fixed]
    return data,chosen[0] if curves is None else chosen


def circular_arc_bands(fn,source,row):
    """Find the exact opposite arc, excluding faces beyond its endpoints."""
    fit=supported_circle(fn,source,row,False)
    if fit is None:return []
    center,radius,axis,u,v,angles=fit
    total=angles[-1]
    if total<=1e-8 or total>=2*math.pi-1e-7:return []
    tol=max(radius*1e-5,1e-6)
    sections={}
    for vertex,p in enumerate(fn.getPoints()):
        q=om.MVector(p)-center;level=q*axis
        if abs(level)<=tol:continue
        radial=q-axis*level
        if abs(radial.length()-radius)>tol:continue
        angle=math.atan2(radial*v,radial*u)
        if angle<-1e-6:angle+=2*math.pi
        if angle>total+1e-6:continue
        key=round(level/tol)
        sections.setdefault(key,[]).append((max(0,angle),vertex))
    root=set(row);result=[]
    for entries in sections.values():
        entries.sort();opposite=[vertex for _,vertex in entries]
        if len(opposite)<2 or entries[0][0]>1e-6 or abs(entries[-1][0]-total)>1e-6:continue
        other=set(opposite);union=root|other
        faces=[];boundary=set()
        for face in range(fn.numPolygons):
            vs=list(fn.getPolygonVertices(face));vertices=set(vs)
            if not vertices<=union or not vertices&root or not vertices&other:continue
            faces.append(face)
            for a,b in zip(vs,vs[1:]+vs[:1]):
                edge=frozenset((a,b))
                if edge in boundary:boundary.remove(edge)
                else:boundary.add(edge)
        expected={frozenset((a,b)) for seq in (row,opposite) for a,b in zip(seq,seq[1:])}
        expected|={frozenset((row[0],opposite[0])),frozenset((row[-1],opposite[-1]))}
        if faces and boundary==expected:result.append((opposite,faces))
    return result


def periodic_samples(points,count):
    """Periodic Catmull-Rom with uniform arc-length sampling."""
    dense=[points[0]]
    size=len(points)
    for i in range(size):
        p0,p1,p2,p3=[points[k%size] for k in (i-1,i,i+1,i+2)]
        for step in range(1,33):
            t=step/32.
            dense.append(tuple(.5*(2*b+(-a+c)*t+(2*a-5*b+4*c-d)*t*t+
                                      (-a+3*b-3*c+d)*t*t*t) for a,b,c,d in zip(p0,p1,p2,p3)))
    lengths=[0.]
    for a,b in zip(dense,dense[1:]):lengths.append(lengths[-1]+math.dist(a,b))
    if lengths[-1]<1e-10:raise ValueError('The selected ring has zero length.')
    samples=[]
    for k in range(count+1):
        s=lengths[-1]*k/count
        i=min(len(dense)-2,max(0,bisect.bisect_right(lengths,s)-1))
        w=(s-lengths[i])/max(1e-15,lengths[i+1]-lengths[i])
        samples.append(tuple(a+(b-a)*w for a,b in zip(dense[i],dense[i+1])))
    samples[-1]=samples[0]
    return samples


def align_open_rows(fn, rows, bands):
    """Resolve all strip orientations together before sampling any row."""
    graph = {index: [] for index in range(len(rows))}
    for a, b, faces in bands:
        boundary = set()
        for face in faces:
            ring = list(fn.getPolygonVertices(face))
            for u, v in zip(ring, ring[1:] + ring[:1]):
                edge = frozenset((u, v))
                if edge in boundary:boundary.remove(edge)
                else:boundary.add(edge)
        first, second = rows[a], rows[b]
        straight = sum(frozenset(pair) in boundary for pair in
                       ((first[0], second[0]), (first[-1], second[-1])))
        crossed = sum(frozenset(pair) in boundary for pair in
                      ((first[0], second[-1]), (first[-1], second[0])))
        if max(straight, crossed) != 2:
            raise ValueError('Cannot establish the source strip endpoint correspondence.')
        flip = int(crossed > straight)
        graph[a].append((b, flip));graph[b].append((a, flip))
    flips = {}
    for root in graph:
        if root in flips:continue
        flips[root] = 0
        pending = [root]
        for a in pending:
            for b, relative in graph[a]:
                desired = flips[a] ^ relative
                if b in flips:
                    if flips[b] != desired:
                        raise ValueError('Selected strips have incompatible endpoint correspondence.')
                else:
                    flips[b] = desired;pending.append(b)
    return [list(reversed(row)) if flips[index] else list(row) for index, row in enumerate(rows)]


def align_closed_rows(rows, correspondences, fixed):
    """Propagate the same seam and winding through adjacent rings."""
    neighbors = [[] for _ in rows]
    for a, b, first, second in correspondences:
        if len(first) != len(second):
            continue
        neighbors[a].append((b, dict(zip(first, second))))
        neighbors[b].append((a, dict(zip(second, first))))
    aligned = {}
    for root in sorted(range(len(rows)), key=lambda index: (index not in fixed, index)):
        if root in aligned:
            continue
        aligned[root] = rows[root]
        pending = [root]
        for a in pending:
            for b, opposite in neighbors[a]:
                order = [opposite[vertex] for vertex in aligned[a]]
                if b not in aligned:
                    aligned[b] = order
                    pending.append(b)
                elif aligned[b] != order:
                    raise ValueError('Selected rings have incompatible seam correspondence.')
    return [aligned[index] for index in range(len(rows))]


def rebuild_group(source,chains,count,connect,triangulate,interpolate):
    """Align selected rows through original face strips, then rebuild once."""
    fn=om.MFnMesh(source)
    closed=chains[0][0]==chains[0][-1]
    if any((c[0]==c[-1])!=closed for c in chains):
        raise ValueError('Select either open chains or closed rings together.')
    rows=[list(c[:-1] if closed else c) for c in chains]
    if len(set(v for row in rows for v in row))!=sum(map(len,rows)):
        raise ValueError('Selected chains must not share vertices.')
    bands=[];used=set();correspondences=[]
    for r in range(len(rows)):
        found,links=discover(fn,source,rows[r],closed,neighbors_only=True)
        for a,b,faces in links:
            if a!=0:continue
            other=next((s for s,row in enumerate(rows) if s!=r and set(row)==set(found[b])),None)
            if other is None or any(f in used for f in faces):continue
            if closed:
                correspondences.append((r, other, tuple(rows[r]), tuple(found[b])))
            rows[other]=found[b]
            bands.append((r,other,faces));used.update(faces)
    fixed=set()
    if not closed:
        for r,row in enumerate(list(rows)):
            for opposite,faces in circular_arc_bands(fn,source,row):
                if any(f in used for f in faces):continue
                component=set(opposite)
                matched=next((s for s,selected in enumerate(rows) if s!=r and set(selected)==component),None)
                if matched is None:
                    if len(opposite)!=count+1 or any(component.intersection(selected) for selected in rows):continue
                    s=len(rows);rows.append(opposite);fixed.add(s)
                else:
                    s=matched;rows[s]=opposite
                bands.append((r,s,faces));used.update(faces)
    # A previous local edit may have triangulated the band. Recover its
    # unchanged opposite boundary and reconnect equal-count rows directly.
    for r,row in enumerate(list(rows)):
        root=set(row);incident=set()
        it=om.MItMeshVertex(source)
        for v in row:
            it.setIndex(v);incident.update(it.getConnectedFaces())
        candidates={v for f in incident for v in fn.getPolygonVertices(f)}-root
        graph={v:set() for v in candidates}
        # Recover the boundary from this band, not from all mesh edges.
        # Cap triangulation can add chords between rim vertices; those
        # chords are outside the band and must not make its ring branch.
        for face in incident:
            vs=list(fn.getPolygonVertices(face))
            for a,b in zip(vs,vs[1:]+vs[:1]):
                if a in graph and b in graph:
                    graph[a].add(b);graph[b].add(a)
        while graph:
            seed=next(iter(graph));component={seed};todo=[seed]
            while todo:
                for v in graph[todo.pop()]:
                    if v not in component:component.add(v);todo.append(v)
            adj={v:graph.pop(v) for v in component}
            matched=next((s for s,selected in enumerate(rows) if s!=r and set(selected)==component),None)
            expected=len(component) if matched is not None else (count if closed else count+1)
            if len(component)!=expected or any(len(n)>2 for n in adj.values()):continue
            if matched is None and any(component.intersection(selected) for selected in rows):continue
            ends=[v for v in component if len(adj[v])==1]
            if (not closed and len(ends)!=2) or (closed and any(len(n)!=2 for n in adj.values())):continue
            start=min(component,key=lambda v:(fn.getPoint(v)-fn.getPoint(row[0])).length()) if closed else min(ends,key=lambda v:(fn.getPoint(v)-fn.getPoint(row[0])).length())
            opposite=[];previous=None;current=start
            while current not in opposite:
                opposite.append(current)
                options=sorted(adj[current]-({previous} if previous is not None else set()))
                if not options:break
                previous,current=current,options[0]
            if len(opposite)!=expected:continue
            union=root|component
            faces=[f for f in incident if set(fn.getPolygonVertices(f))<=union and set(fn.getPolygonVertices(f))&root and set(fn.getPolygonVertices(f))&component]
            if not faces or any(f in used for f in faces):continue
            # Require every boundary edge, preventing incomplete regions.
            boundary=set()
            for f in faces:
                vs=list(fn.getPolygonVertices(f))
                for a,b in zip(vs,vs[1:]+vs[:1]):
                    pair=frozenset((a,b))
                    if pair in boundary:boundary.remove(pair)
                    else:boundary.add(pair)
            def pairs(seq):return {frozenset((a,b)) for a,b in zip(seq,seq[1:]+seq[:1] if closed else seq[1:])}
            expected_boundary=pairs(row)|pairs(opposite)
            if not closed:
                expected_boundary|={frozenset((row[0],opposite[0])),frozenset((row[-1],opposite[-1]))}
            if expected_boundary!=boundary:continue
            if closed:
                first=next((f for f in faces if row[0] in fn.getPolygonVertices(f) and row[1] in fn.getPolygonVertices(f)),None)
                if first is None:continue
                # Choose the neighbour's direction by source boundary winding.
                vs=list(fn.getPolygonVertices(first));forward=vs[(vs.index(row[0])+1)%len(vs)]==row[1]
                owner=next(f for f in faces if opposite[0] in fn.getPolygonVertices(f) and opposite[1] in fn.getPolygonVertices(f))
                vs=list(fn.getPolygonVertices(owner));otherforward=vs[(vs.index(opposite[0])+1)%len(vs)]==opposite[1]
                if forward==otherforward:opposite=[opposite[0]]+list(reversed(opposite[1:]))
            if matched is None:
                s=len(rows);rows.append(opposite);fixed.add(s)
            else:
                s=matched;rows[s]=opposite
            if closed:
                correspondences.append((r, s, tuple(row), tuple(opposite)))
            bands.append((r,s,faces));used.update(faces)
    if closed:
        rows = align_closed_rows(rows, correspondences, fixed)
    else:
        rows = align_open_rows(fn, rows, bands)
    fits=[];curves=[]
    for r,row in enumerate(rows):
        points=[tuple(fn.getPoint(v))[:3] for v in row]
        fit=supported_circle(fn,source,row,closed)
        if fit is not None:
            center,radius,normal,u,v,angles=fit
            total=2*math.pi if closed else angles[-1]
            samples=[tuple(center+(u*math.cos(total*k/count)+v*math.sin(total*k/count))*radius) for k in range(count+1)]
        else:
            lengths=[0.0]
            for a,b in zip(points,points[1:]):lengths.append(lengths[-1]+math.dist(a,b))
            if closed:
                total=lengths[-1]+math.dist(points[-1],points[0])
                if total<1e-10:raise ValueError('The selected ring has zero length.')
                angles=[s/total*2*math.pi for s in lengths]
                samples=periodic_samples(points,count)
            else:
                angles=lengths
                samples=interpolate(points,count)
            fit=(om.MVector(*points[0]),0,None,om.MVector(1,0,0),None,angles)
        samples[0]=points[0]
        if not closed:samples[-1]=points[-1]
        if r in fixed:
            fit=(fit[0],fit[1],fit[2],fit[3],fit[4],[float(i) for i in range(len(row))])
        fits.append(fit);curves.append(samples)
    return build_rows(source,rows,fits,bands,count,connect,triangulate,curves,fixed,closed)
