"""Continuous triangle contacts, with swept-box pruning and cached limits."""
import math
import itertools
import numpy as np
from maya.api import OpenMaya as om


def _roots(coefficients):
    """Real polynomial roots inside (0,1], using derivative intervals."""
    c = list(coefficients)
    scale = max(map(abs,c),default=0)
    if scale<1e-18:
        return []
    while len(c)>1 and abs(c[-1])<scale*1e-12:
        c.pop()
    if len(c)==1:
        return []
    if len(c)==2:
        root = -c[0]/c[1]
        return [root] if 1e-7<root<=1 else []
    if len(c)==3:
        discriminant = c[1]*c[1]-4*c[2]*c[0]
        if discriminant<0: return []
        q = -.5*(c[1]+math.copysign(math.sqrt(discriminant),c[1]))
        roots = (q/c[2],c[0]/q) if abs(q)>scale*1e-15 else (-c[1]/(2*c[2]),)
        return sorted({v for v in roots if 1e-7<v<=1})
    def value(x):
        result = 0
        for v in reversed(c): result = result*x+v
        return result
    critical = _roots([i*c[i] for i in range(1,len(c))]) if len(c)>2 else []
    boundaries = [0]+critical+[1]
    result = [x for x in boundaries[1:] if abs(value(x))<scale*1e-10]
    for low,high in zip(boundaries,boundaries[1:]):
        first,last = value(low),value(high)
        if first*last>=0:
            continue
        for _ in range(42):
            middle = (low+high)*.5
            current = value(middle)
            if first*current<=0: high=middle
            else: low,first=middle,current
        result.append((low+high)*.5)
    return sorted({v for v in result if 1e-7<v<=1})


def _coplanarity(points,motions):
    a,b,c = [points[i]-points[0] for i in (1,2,3)]
    da,db,dc = [motions[i]-motions[0] for i in (1,2,3)]
    return (a*(b^c),da*(b^c)+a*((db^c)+(b^dc)),
            da*((db^c)+(b^dc))+a*(db^dc),da*(db^dc))


def _inside(point,a,b,c,tolerance):
    u,v,w = b-a,c-a,point-a
    uu,uv,vv,wu,wv = u*u,u*v,v*v,w*u,w*v
    denominator = uu*vv-uv*uv
    if denominator<=1e-24:
        return False
    s,t = (wu*vv-wv*uv)/denominator,(wv*uu-wu*uv)/denominator
    normal = u^v
    return s>=-1e-7 and t>=-1e-7 and s+t<=1+1e-7 and abs(w*normal)<=tolerance*normal.length()


def _edge_contact(a,b,c,d,tolerance):
    u,v,w = b-a,d-c,a-c
    uu,uv,vv,uw,vw = u*u,u*v,v*v,u*w,v*w
    determinant = uu*vv-uv*uv
    if determinant<=1e-24:
        return False
    s,t = (uv*vw-vv*uw)/determinant,(uu*vw-uv*uw)/determinant
    return (-1e-7<=s<=1+1e-7 and -1e-7<=t<=1+1e-7 and
            ((a+u*s)-(c+v*t)).length()<=tolerance)


def _point_edge_times(point,a,b,dp,da,db,tolerance):
    u,v,du,dv = b-a,point-a,db-da,dp-da
    coefficients = (u^v,(du^v)+(u^dv),du^dv)
    axis = max(range(3),key=lambda i:sum(abs(tuple(c)[i]) for c in coefficients))
    for t in _roots([tuple(c)[axis] for c in coefficients]):
        p,x,y = point+dp*t,a+da*t,b+db*t
        direction = y-x
        squared = direction*direction
        if squared<=1e-24: continue
        fraction = ((p-x)*direction)/squared
        if -1e-7<=fraction<=1+1e-7 and (p-(x+direction*fraction)).length()<=tolerance:
            yield t


def _contact(first,second,p,d,tolerance):
    limit = math.inf
    for triangle,other in ((first,second),(second,first)):
        a,b,c = triangle
        for vertex in other:
            ids = [a,b,c,vertex]
            coefficients = _coplanarity([p[i] for i in ids],[d[i] for i in ids])
            for t in _roots(coefficients):
                if t<limit and _inside(p[vertex]+d[vertex]*t,*[p[i]+d[i]*t for i in triangle],tolerance):
                    limit=t
            # Coplanar motion has no isolated 3D volume roots. Its first
            # contact occurs when a vertex crosses a triangle boundary.
            if max(map(abs,coefficients))<tolerance**3:
                for x,y in zip(triangle,triangle[1:]+triangle[:1]):
                    limit=min(limit,min(_point_edge_times(p[vertex],p[x],p[y],d[vertex],d[x],d[y],tolerance),default=math.inf))
    for a,b in zip(first,first[1:]+first[:1]):
        for c,e in zip(second,second[1:]+second[:1]):
            ids = [a,b,c,e]
            for t in _roots(_coplanarity([p[i] for i in ids],[d[i] for i in ids])):
                if t<limit and _edge_contact(*[p[i]+d[i]*t for i in ids],tolerance):
                    limit=t
    return limit


def _overlap(a,b):
    return all(a[i]<=b[i+3] and b[i]<=a[i+3] for i in range(3))


def _root_array(coefficients):
    """Batch linear/quadratic roots; solve the remaining cubics exactly."""
    c = np.asarray(coefficients)
    result = np.full((len(c),3),math.inf)
    scale = np.max(np.abs(c),axis=1)
    cubic = np.abs(c[:,3])>scale*1e-12
    quadratic = ~cubic&(np.abs(c[:,2])>scale*1e-12)
    linear = ~cubic&~quadratic&(np.abs(c[:,1])>scale*1e-12)
    ids = np.flatnonzero(linear)
    result[ids,0] = -c[ids,0]/c[ids,1]
    ids = np.flatnonzero(quadratic)
    discriminant = c[ids,1]**2-4*c[ids,2]*c[ids,0]
    ids = ids[discriminant>=0]; discriminant=discriminant[discriminant>=0]
    q = -.5*(c[ids,1]+np.copysign(np.sqrt(discriminant),c[ids,1]))
    result[ids,0] = q/c[ids,2]
    valid = np.abs(q)>scale[ids]*1e-15
    result[ids[valid],1] = c[ids[valid],0]/q[valid]
    # Bernstein control values bound the polynomial on [0,1]. If all have
    # one sign, there cannot be a new contact inside the interval.
    control = np.column_stack((c[:,0],c[:,0]+c[:,1]/3,
                               c[:,0]+2*c[:,1]/3+c[:,2]/3,c.sum(axis=1)))
    cubic &= ((control.min(axis=1)<0)&(control.max(axis=1)>0))|(np.abs(c.sum(axis=1))<=scale*1e-10)
    for i in np.flatnonzero(cubic):
        roots = _roots(c[i])
        result[i,:len(roots)] = roots
    result[(result<=1e-7)|(result>1)] = math.inf
    return result


def _batch_contact(pairs,points,motions,triangles,tolerance):
    """Continuous contacts for a batch of unrelated swept triangles."""
    first = np.asarray([triangles[i][0] for i,j in pairs])
    second = np.asarray([triangles[j][0] for i,j in pairs])
    ids = np.concatenate((first,second),axis=1)
    vertex_faces = [(0,1,2,i) for i in (3,4,5)]+[(3,4,5,i) for i in (0,1,2)]
    edges = [(a,b,c,d) for a,b in ((0,1),(1,2),(2,0)) for c,d in ((3,4),(4,5),(5,3))]
    events = np.asarray(vertex_faces+edges)
    p = points[ids[:,events]].reshape((-1,4,3))
    d = motions[ids[:,events]].reshape((-1,4,3))
    a,b,c = (p[:,i]-p[:,0] for i in (1,2,3))
    da,db,dc = (d[:,i]-d[:,0] for i in (1,2,3))
    def dot(x,y): return np.einsum('ij,ij->i',x,y)
    coefficients = np.column_stack((dot(a,np.cross(b,c)),
        dot(da,np.cross(b,c))+dot(a,np.cross(db,c)+np.cross(b,dc)),
        dot(da,np.cross(db,c)+np.cross(b,dc))+dot(a,np.cross(db,dc)),dot(da,np.cross(db,dc))))
    roots = _root_array(coefficients)
    limits = np.full(len(p),math.inf)
    vertex_event = np.tile(np.arange(15)<6,len(pairs))
    for column in range(3):
        chosen = np.flatnonzero(np.isfinite(roots[:,column]))
        if not len(chosen): continue
        t = roots[chosen,column]
        current = p[chosen]+d[chosen]*t[:,None,None]
        vf = vertex_event[chosen]
        selected=chosen[vf]; r=current[vf]
        if len(selected):
            u,v,w = r[:,1]-r[:,0],r[:,2]-r[:,0],r[:,3]-r[:,0]
            uu,uv,vv,wu,wv = dot(u,u),dot(u,v),dot(v,v),dot(w,u),dot(w,v)
            denominator = uu*vv-uv*uv
            valid=denominator>1e-24; safe=np.where(valid,denominator,1)
            s,f = (wu*vv-wv*uv)/safe,(wv*uu-wu*uv)/safe
            normal=np.cross(u,v)
            valid &= (s>=-1e-7)&(f>=-1e-7)&(s+f<=1+1e-7)&(np.abs(dot(w,normal))<=tolerance*np.linalg.norm(normal,axis=1))
            limits[selected[valid]] = np.minimum(limits[selected[valid]],t[vf][valid])
        selected=chosen[~vf]; r=current[~vf]
        if len(selected):
            u,v,w=r[:,1]-r[:,0],r[:,3]-r[:,2],r[:,0]-r[:,2]
            uu,uv,vv,uw,vw=dot(u,u),dot(u,v),dot(v,v),dot(u,w),dot(v,w)
            denominator=uu*vv-uv*uv; valid=denominator>1e-24
            safe=np.where(valid,denominator,1)
            s,f=(uv*vw-vv*uw)/safe,(uu*vw-uv*uw)/safe
            distance=np.linalg.norm(w+u*s[:,None]-v*f[:,None],axis=1)
            valid &= (s>=-1e-7)&(s<=1+1e-7)&(f>=-1e-7)&(f<=1+1e-7)&(distance<=tolerance)
            limits[selected[valid]] = np.minimum(limits[selected[valid]],t[~vf][valid])
    # Coplanar vertex/face events become point/edge quadratic contacts.
    coplanar = np.flatnonzero(vertex_event&(np.max(np.abs(coefficients),axis=1)<tolerance**3))
    if len(coplanar):
        mappings=np.asarray([(3,0,1),(3,1,2),(3,2,0)])
        cp=p[coplanar[:,None,None],mappings[None,:,:]].reshape((-1,3,3))
        cd=d[coplanar[:,None,None],mappings[None,:,:]].reshape((-1,3,3))
        u,v,du,dv=cp[:,2]-cp[:,1],cp[:,0]-cp[:,1],cd[:,2]-cd[:,1],cd[:,0]-cd[:,1]
        cross=np.stack((np.cross(u,v),np.cross(du,v)+np.cross(u,dv),np.cross(du,dv)),axis=1)
        axis=np.abs(cross).sum(axis=1).argmax(axis=1)
        coefficients=cross[np.arange(len(cp)),:,axis]
        cr=_root_array(np.column_stack((coefficients,np.zeros(len(cp)))))
        owners=np.repeat(coplanar,3)
        for column in range(2):
            chosen=np.flatnonzero(np.isfinite(cr[:,column]))
            if not len(chosen): continue
            t=cr[chosen,column]; r=cp[chosen]+cd[chosen]*t[:,None,None]
            direction=r[:,2]-r[:,1]; square=dot(direction,direction)
            valid=square>1e-24; safe=np.where(valid,square,1)
            f=dot(r[:,0]-r[:,1],direction)/safe
            valid &= (f>=-1e-7)&(f<=1+1e-7)&(np.linalg.norm(r[:,0]-r[:,1]-direction*f[:,None],axis=1)<=tolerance)
            np.minimum.at(limits,owners[chosen[valid]],t[valid])
    values=limits.reshape((-1,15)).min(axis=1)
    closest=int(values.argmin())
    return values[closest],closest


def first_contact(points,vectors,triangles,width,cache=None):
    """First nonadjacent face contact along a linear width expansion."""
    if cache is not None:
        cache["contact_vertices"] = ()
    if not triangles or width<=1e-12:
        return math.inf,()
    motions = [v*width for v in vectors]
    tolerance = max(width*1e-8,1e-9)
    positions = np.asarray([tuple(p)[:3] for p in points])
    displacement = np.asarray([tuple(d) for d in motions])
    indices = np.asarray([ids for ids,_ in triangles])
    coordinates = np.concatenate((positions[indices],(positions+displacement)[indices]),axis=1)
    exact_boxes = np.concatenate((coordinates.min(axis=1)-tolerance,coordinates.max(axis=1)+tolerance),axis=1)
    # Batch the convex swept-hull planes in NumPy, which ships with Maya.
    a,b,c = np.asarray(list(itertools.combinations(range(6),3))).T
    normals = np.cross(coordinates[:,b]-coordinates[:,a],coordinates[:,c]-coordinates[:,a])
    lengths = np.linalg.norm(normals,axis=2)
    normals /= np.maximum(lengths,1e-30)[:,:,None]
    offsets = np.einsum('tlk,tlk->tl',normals,coordinates[:,a])
    distances = np.einsum('tlk,tpk->tlp',normals,coordinates)-offsets[:,:,None]
    negative = distances.min(axis=2)>=-tolerance
    valid = (lengths>1e-12)&(negative|(distances.max(axis=2)<=tolerance))
    normals[negative] *= -1
    offsets[negative] *= -1
    offsets[~valid] = math.inf
    # A coplanar swept triangle has no 3D side faces. Include its in-plane
    # support directions too, otherwise unrelated cap sections all reach the
    # expensive continuous solver despite being visibly far apart.
    base = normals[np.arange(len(triangles)),lengths.argmax(axis=1)]
    ea,eb = np.asarray(list(itertools.combinations(range(6),2))).T
    lateral = np.cross(coordinates[:,eb]-coordinates[:,ea],base[:,None,:])
    lateral_lengths = np.linalg.norm(lateral,axis=2)
    lateral /= np.maximum(lateral_lengths,1e-30)[:,:,None]
    projections = np.einsum('tlk,tpk->tlp',lateral,coordinates)
    edge_offset = np.einsum('tlk,tlk->tl',lateral,coordinates[:,ea])
    edge_distances = projections-edge_offset[:,:,None]
    flip = edge_distances.min(axis=2)>=-tolerance
    edge_valid = (lateral_lengths>1e-12)&(flip|(edge_distances.max(axis=2)<=tolerance))
    lateral[flip] *= -1
    edge_offset[flip] *= -1
    edge_offset[~edge_valid] = math.inf
    normals = np.concatenate((normals,lateral),axis=1)
    offsets = np.concatenate((offsets,edge_offset),axis=1)
    # Coplanar combinations repeat the same support plane many times.
    plane_ids = np.repeat(np.arange(len(triangles)),normals.shape[1])
    valid = np.isfinite(offsets.ravel())
    values = np.column_stack((plane_ids[valid],normals.reshape((-1,3))[valid],offsets.ravel()[valid]))
    _,unique = np.unique(np.round(values,9),axis=0,return_index=True)
    values = values[unique]
    owners = values[:,0].astype(int)
    counts = np.bincount(owners,minlength=len(triangles))
    size = int(counts.max(initial=0))
    compressed_normals = np.zeros((len(triangles),size,3))
    compressed_offsets = np.full((len(triangles),size),math.inf)
    columns = np.arange(len(owners))-np.repeat(np.cumsum(counts)-counts,counts)
    compressed_normals[owners,columns] = values[:,1:4]
    compressed_offsets[owners,columns] = values[:,4]
    normals,offsets = compressed_normals,compressed_offsets
    reuse = (cache is not None and "boxes" in cache and
             cache["topology"]==tuple(triangles) and
             np.all(exact_boxes[:,:3]>=cache["boxes"][:,:3]) and
             np.all(exact_boxes[:,3:]<=cache["boxes"][:,3:]))
    if reuse:
        pairs = cache["pairs"]
    else:
        boxes = exact_boxes.copy()
        if cache is not None:
            padding = np.maximum((boxes[:,3:]-boxes[:,:3])*.1,width*.005)
            boxes[:,:3] -= padding
            boxes[:,3:] += padding
        adjacency = positions if cache is None else cache.setdefault("adjacency_positions",positions.copy())
        origins = [{tuple(adjacency[v]) for v in ids} for ids,_ in triangles]
        face_members = {}
        vertex_faces = {}
        for ids,face in triangles:
            face_members.setdefault(face,set()).update(ids)
            for vertex in ids:
                vertex_faces.setdefault(vertex,set()).add(face)
        neighbors = {face:set().union(*(vertex_faces[v] for v in members))
                     for face,members in face_members.items()}
        face_ids = np.asarray([face for ids,face in triangles])
        pair_list=[]
        # This vector comparison replaces repeated Python tree traversals.
        for i,(first,face) in enumerate(triangles):
            overlap = ((boxes[:,:3]<=boxes[i,3:])&(boxes[:,3:]>=boxes[i,:3])).all(axis=1)
            overlap &= face_ids!=face
            for j in np.flatnonzero(overlap[i+1:])+i+1:
                second,other=triangles[j]
                if other in neighbors[face]: continue
                # Existing generated-corner adjacency at width zero is not
                # a new contact. Include only unrelated source sections.
                if origins[i].intersection(origins[j]): continue
                pair_list.append((i,j))
        pairs = np.asarray(pair_list,dtype=int).reshape((-1,2))
        if cache is not None:
            cache.update(boxes=boxes,pairs=pairs,topology=tuple(triangles))
    limit=math.inf
    faces=()
    moving = (np.linalg.norm(displacement[indices],axis=2)>1e-12).any(axis=1)
    pairs = pairs[moving[pairs[:,0]]|moving[pairs[:,1]]]
    for start in range(0,len(pairs),2048):
        firsts,seconds = pairs[start:start+2048].T
        separated = (np.einsum('jpk,jlk->jlp',coordinates[seconds],normals[firsts]).min(axis=2)>
                     offsets[firsts]+tolerance).any(axis=1)
        separated |= (np.einsum('jpk,jlk->jlp',coordinates[firsts],normals[seconds]).min(axis=2)>
                      offsets[seconds]+tolerance).any(axis=1)
        selected = pairs[start:start+2048][~separated]
        if not len(selected): continue
        contact,closest = _batch_contact(selected,positions,displacement,triangles,tolerance)
        if contact<limit:
            i,j=selected[closest]
            limit,faces=contact,(triangles[i][1],triangles[j][1])
            if cache is not None:
                cache["contact_vertices"] = tuple(set(triangles[i][0]+triangles[j][0]))
    return limit*width,faces
