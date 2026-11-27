"""Parallel inward and outward guide wavefronts, clipped to their bands."""
import math
import numpy as np
from model_pack_pattern_lattice import lattice


def waves(outer, guides, mask_type, ring_samples, inset_polygon,
          separation, rows, columns, angle, staggered, center, inner_ring):
    anchored = list(lattice(outer,guides,mask_type,ring_samples,separation,
                            rows,columns,angle,staggered,anchors_only=True))
    occupied = [p for p,t,fixed in anchored]
    yield from anchored
    perimeter = sum(np.linalg.norm(np.asarray(b)-a) for a,b in zip(outer,outer[1:]+outer[:1]))
    along = max(separation*1.05,perimeter/columns)
    generated = len(anchored)
    # Preserve the original center-to-guide construction: complete scaled
    # contours sampled by arc length, rather than miter offsets which can
    # collapse at different corners and lose parts of an otherwise round row.
    base_array = np.asarray(guides[0])
    inner_ring = np.asarray(inner_ring)
    radius = float(np.linalg.norm(inner_ring-center,axis=1).mean())
    step = max(along*(math.sqrt(3)*.5 if staggered else 1),separation*1.05)
    row_count = min(max(rows-1,1),max(1,round(radius/step)))
    edge = np.roll(base_array,-1,axis=0)-base_array
    squared = np.sum(edge*edge,axis=1)
    delta = center-base_array
    t = np.clip(np.sum(delta*edge,axis=1)/np.maximum(squared,1e-30),0,1)
    clearance = float(np.linalg.norm(delta-t[:,None]*edge,axis=1).min())
    if separation>0:
        row_count = min(row_count,max(1,math.floor(clearance/(separation*1.05))))
    if all(np.linalg.norm(center-p)>=separation for p in occupied):
        occupied.append(center)
        yield center,np.array([1.,0.]),False
    for index in range(1,row_count):
        contour = center+(inner_ring-center)*(index/row_count)
        length = float(np.linalg.norm(np.roll(contour,-1,axis=0)-contour,axis=1).sum())
        count = max(1,round(length/along))
        generated += count
        if generated>20000:
            raise ValueError('Guide Waves: уменьшите Max Rows / Max per Row.')
        phase = .5 if staggered and index%2 else 0
        candidates = ring_samples(contour,count,phase,separation)
        points = np.asarray([p for p,tangent in candidates])
        distance = np.sum((points[:,None,:]-np.asarray(occupied)[None,:,:])**2,axis=2).min(axis=1)
        allowed = distance>=separation*separation
        accepted = [sample for sample,valid in zip(candidates,allowed) if valid]
        occupied.extend(p for p,tangent in accepted)
        for p,tangent in accepted:
            yield p,tangent,False
    for base,limit in zip(guides,guides[1:]+[outer]):
        base_array = np.asarray(base)
        edge = np.roll(base_array,-1,axis=0)-base_array
        squared = np.sum(edge*edge,axis=1)
        # The farthest usable wave is determined by the widest part of the
        # band. Narrow parts terminate against the next anchored contour.
        delta = np.asarray(limit)[:,None,:]-base_array[None,:,:]
        t = np.clip(np.sum(delta*edge[None,:,:],axis=2)/np.maximum(squared,1e-30),0,1)
        distances = np.linalg.norm(delta-t[:,:,None]*edge[None,:,:],axis=2).min(axis=1)
        maximum = float(distances.max())
        step = max(along*(math.sqrt(3)*.5 if staggered else 1),separation*1.05,
                   maximum/max(rows-1,1))
        limit_mask = mask_type(limit)
        base_mask = mask_type(base)
        for index in range(1,min(rows,math.ceil(maximum/step))+1):
            contour = inset_polygon(base,-index*step)
            length = sum(np.linalg.norm(np.asarray(b)-a) for a,b in zip(contour,contour[1:]+contour[:1]))
            count = max(3,round(length/along))
            generated += count
            if generated>20000:
                raise ValueError('Guide Waves: уменьшите Max Rows / Max per Row.')
            phase = (angle/360*count+(.5 if staggered and index%2 else 0))%1
            candidates = ring_samples(contour,count,phase,separation)
            points = np.asarray([p for p,tangent in candidates])
            allowed = limit_mask.contains(points)&~base_mask.contains(points)
            if occupied:
                distance = np.sum((points[:,None,:]-np.asarray(occupied)[None,:,:])**2,axis=2).min(axis=1)
                allowed &= distance>=separation*separation
            accepted = [sample for sample,valid in zip(candidates,allowed) if valid]
            occupied.extend(p for p,tangent in accepted)
            for p,tangent in accepted:
                yield p,tangent,False
