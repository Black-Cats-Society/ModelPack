"""Deterministic constrained centroidal lattices for curved pattern masks."""
import math
import numpy as np


def nearest(samples, points):
    result = np.empty(len(samples),dtype=int)
    for start in range(0,len(samples),512):
        batch = samples[start:start+512]
        dx = batch[:,0,None]-points[None,:,0]
        dy = batch[:,1,None]-points[None,:,1]
        result[start:start+len(batch)] = np.argmin(dx*dx+dy*dy,axis=1)
    return result


def lattice(outer, guides, mask_type, ring_samples, separation, rows, columns, angle, staggered, anchors_only=False):
    """Relax a single regular lattice while freezing the contour rows."""
    outer = np.asarray(outer)
    perimeter = lambda ring:float(np.linalg.norm(np.roll(ring,-1,axis=0)-ring,axis=1).sum())
    spacing = perimeter(outer)/columns
    radial = np.linalg.norm(outer-outer.mean(axis=0),axis=1).max()/max(rows-1,1)
    step = max(separation*1.12,spacing,radial*(2/math.sqrt(3) if staggered else 1))
    contact = max(separation*1.005,step*.82)
    masks = [mask_type(p) for p in guides]
    outer_mask = mask_type(outer)
    def labels(points):
        return sum((mask.contains(points).astype(int) for mask in masks),np.zeros(len(points),dtype=int))
    fixed = []
    tangents = []
    fixed_bands = []
    for index,polygon in enumerate([outer]+[np.asarray(p) for p in guides]):
        count = max(3,round(perimeter(polygon)/spacing))
        candidates = []
        while count>=3:
            best = []
            best_distance = -1.
            for phase in (0.,.25,.5,.75):
                samples = ring_samples(polygon,count,(angle/360*count+phase)%1,separation)
                positions = np.array([p for p,t in samples])
                distance = math.inf
                if fixed:
                    delta = positions[:,None,:]-np.asarray(fixed)[None,:,:]
                    distance = np.min(np.sum(delta*delta,axis=2))
                if distance>best_distance:
                    best,best_distance = samples,distance
            if best_distance>=separation*separation:
                candidates = best
                break
            count = len(best)-1
        if len(candidates)<3:
            raise ValueError('Guides: закреплённые ряды слишком близко для размера Source.')
        fixed.extend(p for p,t in candidates)
        tangents.extend(t for p,t in candidates)
        fixed_bands.extend([{0} if index==0 else {len(guides)-index,len(guides)-index+1}]*len(candidates))
    if anchors_only:
        for point,tangent in zip(fixed,tangents):
            yield point,tangent,True
        return
    fixed = np.asarray(fixed)
    low,high = outer.min(axis=0),outer.max(axis=0)
    radians = math.radians(angle)
    rotation = np.array([[math.cos(radians),-math.sin(radians)],[math.sin(radians),math.cos(radians)]])
    bounds = outer@rotation
    grid_low,grid_high = bounds.min(axis=0),bounds.max(axis=0)
    vertical = step*(math.sqrt(3)*.5 if staggered else 1.)
    nx,ny = np.ceil((grid_high-grid_low)/[step,vertical]).astype(int)+1
    if nx*ny>20000:
        raise ValueError('Слишком высокая плотность. Уменьшите Max Rows / Max per Row.')
    seeds = np.array([[grid_low[0]+(x+(.5 if staggered and y%2 else 0))*step,
                       grid_low[1]+y*vertical] for y in range(ny) for x in range(nx)])@rotation.T
    seeds = seeds[outer_mask.contains(seeds)]
    if len(seeds):
        distances = np.min(np.sum((seeds[:,None,:]-fixed[None,:,:])**2,axis=2),axis=1)
        seeds = seeds[distances>separation*separation*1.02]
    points = np.vstack((fixed,seeds))
    pinned = len(fixed)
    if len(points)>1500:
        raise ValueError('Более 1500 флоатеров. Уменьшите Max Rows / Max per Row.')
    bands = labels(points)
    # Regular quadrature samples approximate Voronoi cells. No random seeding
    # or sequential acceptance decisions enter the pattern.
    sample_step = max(step/3,math.sqrt(np.prod(high-low)/24000))
    sx,sy = np.ceil((high-low)/sample_step).astype(int)+1
    xx,yy = np.meshgrid(low[0]+(np.arange(sx)+.5)*sample_step,
                        low[1]+(np.arange(sy)+.5)*sample_step)
    samples = np.column_stack((xx.ravel(),yy.ravel()))
    samples = samples[outer_mask.contains(samples)]
    sample_bands = labels(samples)
    for _ in range(16):
        target = points.copy()
        for band in range(len(guides)+1):
            indices = np.array([i for i in range(len(points)) if
                                (band in fixed_bands[i] if i<pinned else bands[i]==band)])
            cloud = samples[sample_bands==band]
            if not len(indices) or not len(cloud):
                continue
            owners = indices[nearest(cloud,points[indices])]
            counts = np.bincount(owners,minlength=len(points))
            moving = np.flatnonzero((counts>0)&(np.arange(len(points))>=pinned))
            for axis in range(2):
                sums = np.bincount(owners,weights=cloud[:,axis],minlength=len(points))
                target[moving,axis] = sums[moving]/counts[moving]
        delta = points[:,None,:]-points[None,:,:]
        squared = np.sum(delta*delta,axis=2)
        np.fill_diagonal(squared,math.inf)
        lengths = np.sqrt(np.maximum(squared,1e-20))
        factor = np.maximum(contact-lengths,0)/lengths
        correction = np.sum(delta*factor[:,:,None],axis=1)
        target[pinned:] += correction[pinned:]*.65
        movement = (target-points)*.7
        movement[:pinned] = 0
        for _ in range(8):
            candidate = points+movement
            valid = outer_mask.contains(candidate)&(labels(candidate)==bands)
            if np.all(valid[pinned:]):
                break
            movement[~valid] *= .5
        points[pinned:] += movement[pinned:]
    # Resolve near contacts collectively. Contour anchors stay exactly fixed.
    for _ in range(12):
        delta = points[:,None,:]-points[None,:,:]
        squared = np.sum(delta*delta,axis=2)
        np.fill_diagonal(squared,math.inf)
        lengths = np.sqrt(np.maximum(squared,1e-20))
        factor = np.maximum(contact-lengths,0)/lengths
        if np.max(factor)<1e-5:
            break
        correction = np.sum(delta*factor[:,:,None],axis=1)
        proposed = points+correction*.55
        valid = outer_mask.contains(proposed)&(labels(proposed)==bands)
        points[pinned:] = np.where(valid[pinned:,None],proposed[pinned:],points[pinned:])
    # Boundary-derived directions keep noncircular sources tangent to the rows.
    tangent_array = np.asarray(tangents)
    owner = nearest(points[pinned:],fixed) if len(points)>pinned else []
    for i,point in enumerate(points):
        tangent = tangent_array[i if i<pinned else owner[i-pinned]]
        yield point,tangent,i<pinned
