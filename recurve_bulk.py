"""Bulk topology assembly: only selected incident faces use Python corner records."""
import bisect
import math
import numpy as np
import maya.api.OpenMaya as om


def ints(values):
    return np.fromiter(values,dtype=np.int32,count=len(values))


def native(values):
    # Maya API expects Python ints rather than numpy scalar objects.
    return om.MIntArray(values.tolist())


def splice(original,counts,replacements):
    offsets=np.empty(len(counts)+1,dtype=np.int64)
    offsets[0]=0;np.cumsum(counts,out=offsets[1:])
    blocks=[];newcounts=[];last=0
    for face,parts in sorted(replacements.items()):
        blocks.append(original[offsets[last]:offsets[face]])
        newcounts.append(counts[last:face])
        blocks.append(np.array([v for part in parts for v in part],dtype=np.int32))
        newcounts.append(np.array([len(part) for part in parts],dtype=np.int32))
        last=face+1
    blocks.append(original[offsets[last]:]);newcounts.append(counts[last:])
    return np.concatenate(newcounts),np.concatenate(blocks)


def rebuild_bulk(source,chain,count,connect,interpolate,triangulate):
    fn=om.MFnMesh(source)
    curve=[tuple(fn.getPoint(v))[:3] for v in chain]
    samples=interpolate(curve,count)
    lengths=[0.0]
    for a,b in zip(curve,curve[1:]):
        distance=math.dist(a,b)
        if distance<1e-10:raise ValueError('Chain contains a zero-length edge.')
        lengths.append(lengths[-1]+distance)
    params=[s/lengths[-1]*count for s in lengths]
    buckets=[int(math.floor(s+0.5)) for s in params]
    excluded=sorted(chain)
    base=fn.numVertices-len(chain)
    remap=np.arange(fn.numVertices,dtype=np.int32)
    remap-=np.searchsorted(excluded,remap).astype(np.int32)
    remap[np.array(chain,dtype=np.int32)]=base+np.array(buckets,dtype=np.int32)
    points=fn.getPoints()
    for v in reversed(excluded):points.remove(v)
    for p in samples:points.append(om.MPoint(*p))
    spans={}
    for i,(a,b) in enumerate(zip(chain,chain[1:])):
        spans[a,b]=(i,False);spans[b,a]=(i,True)
    counts0,connections0=fn.getVertices()
    counts=ints(counts0);connections=remap[ints(connections0)]
    offsets=np.empty(len(counts)+1,dtype=np.int64)
    offsets[0]=0;np.cumsum(counts,out=offsets[1:])
    affected=set()
    it=om.MItMeshVertex(source)
    for v in chain:
        it.setIndex(v);affected.update(it.getConnectedFaces())
    pieces={};topology={}
    for f in sorted(affected):
        verts=list(fn.getPolygonVertices(f));corners=[]
        for j,a in enumerate(verts):
            b=verts[(j+1)%len(verts)]
            corners.append((int(remap[a]),j,j,0.0))
            if (a,b) in spans:
                i,reverse=spans[a,b]
                ks=list(range(buckets[i]+1,buckets[i+1]))
                if reverse:ks.reverse()
                for k in ks:
                    w=(k-params[i])/(params[i+1]-params[i])
                    if reverse:w=1-w
                    corners.append((base+k,j,(j+1)%len(verts),max(0,min(1,w))))
        clean=[]
        for c in corners:
            if not clean or c[0]!=clean[-1][0]:clean.append(c)
        if len(clean)>1 and clean[0][0]==clean[-1][0]:clean.pop()
        if len(clean)<3:
            parts=[]
        else:
            if len({c[0] for c in clean})!=len(clean):raise ValueError('This count would create a pinched face.')
            split=connect and len(clean)>3 and (len(clean)>4 or any(c[1]!=c[2] for c in clean))
            parts=triangulate(clean,points) if split else [clean]
        pieces[f]=parts
        topology[f]=[[c[0] for c in part] for part in parts]
    outcounts,outconnections=splice(connections,counts,topology)
    nativecounts,nativeconnections=native(outcounts),native(outconnections)
    data=om.MFnMeshData().create();out=om.MFnMesh()
    out.create(points,nativecounts,nativeconnections,parent=data)
    actualcounts,actualconnections=out.getVertices()
    if out.numVertices!=len(points) or not np.array_equal(ints(actualcounts),outcounts) or not np.array_equal(ints(actualconnections),outconnections):
        raise ValueError('Maya would alter unrelated topology at this count.')
    # Smoothing is transferred by vertex-pair keys using compact native arrays.
    oldedges=np.fromiter((v for e in range(fn.numEdges) for v in fn.getEdgeVertices(e)),dtype=np.int32,count=fn.numEdges*2).reshape(-1,2)
    edgevertices=remap[oldedges]
    edgevertices.sort(axis=1)
    keys=edgevertices[:,0].astype(np.int64)*len(points)+edgevertices[:,1]
    smooth=np.fromiter((fn.isEdgeSmooth(e) for e in range(fn.numEdges)),dtype=np.bool_,count=fn.numEdges)
    extras=[];extra_smooth=[]
    for i,(a,b) in enumerate(zip(chain,chain[1:])):
        matches=np.nonzero(((oldedges[:,0]==a)&(oldedges[:,1]==b))|
                           ((oldedges[:,0]==b)&(oldedges[:,1]==a)))[0]
        if len(matches):
            for k in range(buckets[i],buckets[i+1]):
                extras.append((base+k)*len(points)+base+k+1);extra_smooth.append(smooth[matches[0]])
    if extras:
        keys=np.concatenate((keys,np.array(extras,dtype=np.int64)))
        smooth=np.concatenate((smooth,np.array(extra_smooth,dtype=np.bool_)))
    order=np.argsort(keys,kind='stable');keys=keys[order];smooth=smooth[order]
    resultedges=np.fromiter((v for e in range(out.numEdges) for v in out.getEdgeVertices(e)),dtype=np.int32,count=out.numEdges*2).reshape(-1,2)
    resultedges.sort(axis=1)
    resultkeys=resultedges[:,0].astype(np.int64)*len(points)+resultedges[:,1]
    positions=np.searchsorted(keys,resultkeys,side='right')-1
    valid=(positions>=0)&(keys[np.maximum(positions,0)]==resultkeys)
    result_smooth=np.ones(out.numEdges,dtype=np.bool_)
    result_smooth[valid]=smooth[positions[valid]]
    out.setEdgeSmoothings(om.MIntArray(range(out.numEdges)),result_smooth.tolist());out.cleanupEdgeSmoothing()
    from recurve_normals import transfer_bulk
    transfer_bulk(fn,out,points,pieces,outcounts,outconnections,splice,ints)
    uvsets={}
    for name in fn.getUVSetNames():
        us,vs=fn.getUVs(name);uc,ui=fn.getAssignedUVs(name);uvparts={};cache={}
        for f,parts in pieces.items():
            records=[]
            for part in parts:
                ids=[]
                try:
                    for _,a,b,w in part:
                        ia=fn.getPolygonUVid(f,a,name);ib=fn.getPolygonUVid(f,b,name)
                        if a==b or w<=0:idx=ia
                        elif w>=1:idx=ib
                        else:
                            key=(ia,ib,round(w,12)) if ia<=ib else (ib,ia,round(1-w,12))
                            if key not in cache:
                                cache[key]=len(us)
                                us.append(us[ia]+(us[ib]-us[ia])*w);vs.append(vs[ia]+(vs[ib]-vs[ia])*w)
                            idx=cache[key]
                        ids.append(idx)
                except RuntimeError:
                    ids=[]
                records.append(ids)
            uvparts[f]=records
        uvcounts,uvids=splice(ints(ui),ints(uc),uvparts)
        uvsets[name]=(us,vs,native(uvcounts),native(uvids))
    from recurve_uv import assign_uv_sets
    data=assign_uv_sets(data,uvsets,fn.currentUVSetName())
    from recurve_colors import transfer_bulk
    data=transfer_bulk(fn,data,pieces,splice,ints)
    from recurve import transfer_face_groups
    owners = (f for f in range(fn.numPolygons) for _ in range(len(pieces[f]) if f in pieces else 1))
    data=transfer_face_groups(source,data,owners)
    selected_result=resultedges[(resultedges[:,0]>=base)&(resultedges[:,1]>=base)]
    pairs={frozenset((int(a),int(b))) for a,b in selected_result}
    if any(frozenset((base+k,base+k+1)) not in pairs for k in range(count)):
        raise ValueError('Topology cannot represent this segment count.')
    return data,list(range(base,base+count+1))
