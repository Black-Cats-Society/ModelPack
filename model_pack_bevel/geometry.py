"""Cached deformation of Maya bevel templates, in physical world units."""
from collections import defaultdict
import itertools
import math

from maya.api import OpenMaya as om

from .profiles import Curve, profile_value
from .collision import first_contact


class TemplateGeometry:
    def __init__(self, source, flat, rounded, graph, reference_width, matrix, topology=None, triangles=None):
        self.matrix = matrix
        self.inverse = matrix.inverse()
        self.reference_width = reference_width
        self.source = [om.MPoint(p) * matrix for p in source]
        self.flat = [om.MPoint(p) * matrix for p in flat]
        self.rounded = [om.MPoint(p) * matrix for p in rounded]
        if len(self.flat) != len(self.rounded):
            raise ValueError("Шаблоны сечения имеют разную топологию.")
        self.anchors = []
        self.samples = [.5] * len(self.flat)
        cell = max(reference_width * 64, 1e-8)
        buckets = defaultdict(list)

        def key(point):
            return tuple(math.floor(v / cell) for v in (point.x, point.y, point.z))

        for vertex, point in enumerate(self.source):
            buckets[key(point)].append(vertex)
        selected = set(graph.neighbors)
        groups = defaultdict(list)
        for index, point in enumerate(self.flat):
            center = key(point)
            candidates = [v for delta in itertools.product((-1, 0, 1), repeat=3)
                          for v in buckets.get(tuple(a + b for a, b in zip(center, delta)), ())]
            closest = min(candidates, key=lambda v: (point - self.source[v]).length()) if candidates else None
            distance = (point - self.source[closest]).length() if closest is not None else math.inf
            anchor = closest if closest in selected and distance < cell else None
            self.anchors.append(anchor)
            if anchor is not None:
                groups[anchor].append(index)
        self.groups = dict(groups)
        # Flat samples lie on straight cross-section chords. Find their two
        # fixed border points to orient and evaluate the spline consistently.
        tolerance = max(reference_width * 1e-3, 1e-8)
        for members in self.groups.values():
            borders = [i for i in members
                       if (self.flat[i] - self.rounded[i]).length() <= tolerance]
            for vertex in members:
                if (self.flat[vertex] - self.rounded[vertex]).length() <= tolerance:
                    self.samples[vertex] = 0
                    continue
                choices = []
                for a, b in itertools.combinations(borders, 2):
                    direction = self.flat[b] - self.flat[a]
                    squared = direction * direction
                    if squared < 1e-16:
                        continue
                    t = ((self.flat[vertex] - self.flat[a]) * direction) / squared
                    residual = (self.flat[vertex] - (self.flat[a] + direction * t)).length()
                    if -1e-4 <= t <= 1 + 1e-4 and residual <= tolerance:
                        # Orient by coordinates, not native vertex ID changes.
                        forward = tuple(flat[a])[:3] < tuple(flat[b])[:3]
                        choices.append((squared, t if forward else 1-t))
                if choices:
                    self.samples[vertex] = max(choices)[1]
        self.constraints = []
        self.constraint_faces = []
        self._independent_key = None
        self._independent_bound = 0
        self._independent_limits = {}
        self._independent_contacts = []
        self._independent_cache = {"adjacency_positions":[tuple(self.source[a] if a is not None else p)[:3]
                                                         for a,p in zip(self.anchors,self.flat)]}
        self.uniform_limit = math.inf
        self.triangles = []
        self._contact_key = None
        self._contact_limit = math.inf
        self._shared_contact_faces = ()
        self._contact_bound = 0
        self._contact_cache = {}
        self._local_contact_cache = {"adjacency_positions":[tuple(self.source[a] if a is not None else p)[:3]
                                                            for a,p in zip(self.anchors,self.flat)]}
        self.contact_faces = ()
        self._previous = None
        if topology is not None:
            counts,connects = topology
            origins = [self.source[a] if a is not None else p for a,p in zip(self.anchors,self.flat)]
            vectors = [(p-origin)/reference_width for p,origin in zip(self.flat,origins)]
            start = 0
            for face,count in enumerate(counts):
                ids = list(connects[start:start+count])
                start += count
                if not any(self.anchors[i] is not None for i in ids):
                    continue
                points = [origins[i] for i in ids]
                normal = om.MVector()
                for first,second in zip(points,points[1:]+points[:1]):
                    normal += om.MVector(first)^om.MVector(second)
                if normal.length() < 1e-15:
                    # Newly generated strips collapse at zero width; original
                    # residual faces determine where those strips can expand.
                    continue
                normal.normalize()
                self.constraints.append((ids,normal))
                self.constraint_faces.append(face)
            self.uniform_limit = self._collision(origins,vectors)*(1-1e-4)
            if triangles is not None:
                tri_counts,tri_vertices = triangles
                start = 0
                for face,count in enumerate(tri_counts):
                    for _ in range(count):
                        self.triangles.append((tuple(tri_vertices[start:start+3]),face))
                        start+=3

    @staticmethod
    def _area(points,normal):
        center = points[0]
        return sum(((points[i]-center)^(points[i+1]-center))*normal
                   for i in range(1,len(points)-1))

    @staticmethod
    def _positive_root(constant,linear,quadratic):
        epsilon = 1e-12*max(abs(constant),abs(linear),abs(quadratic),1e-20)
        if abs(quadratic) <= epsilon:
            return -constant/linear if linear < -epsilon else math.inf
        discriminant = linear*linear-4*quadratic*constant
        if discriminant < -epsilon*max(abs(linear),abs(quadratic),abs(constant)):
            return math.inf
        root = math.sqrt(max(0,discriminant))
        q = -.5*(linear+math.copysign(root,linear))
        choices = (q/quadratic,constant/q) if abs(q)>epsilon else (-linear/(2*quadratic),)
        return min((v for v in choices if v>1e-8),default=math.inf)

    def _collision(self,origins,vectors):
        limit = math.inf
        for ids,normal in self.constraints:
            border,_ = self._border_contact(origins,vectors,ids)
            limit = min(limit,border)
            points = [origins[i] for i in ids]
            directions = [vectors[i] for i in ids]
            constant = self._area(points,normal)
            plus = self._area([p+d for p,d in zip(points,directions)],normal)
            minus = self._area([p-d for p,d in zip(points,directions)],normal)
            limit = min(limit,self._positive_root(constant,(plus-minus)*.5,(plus+minus)*.5-constant))
        return limit

    @staticmethod
    def _border_contact(points,movements,ids):
        """A residual border may collapse at one end before face area is zero."""
        limit,members = math.inf,()
        for a,b in zip(ids,ids[1:]+ids[:1]):
            span = points[b]-points[a]
            motion = movements[b]-movements[a]
            squared = motion*motion
            if squared<=1e-24:
                continue
            time = -(span*motion)/squared
            if time<=1e-8 or time>=limit:
                continue
            if (span+motion*time).length()<=max(span.length()*1e-8,1e-10):
                limit,members = time,(a,b)
        return limit,members

    def _evaluate_independent(self, width, shape, weights, limits, profile_kind, curve):
        """Advance sections to contact, then continue with the remaining sections.

        Width caps belong to source cross sections, so all samples of a section
        stop together. Cache a checked interval to keep ordinary dragging cheap.
        """
        origins = [self.source[a] if a is not None else p for a,p in zip(self.anchors,self.flat)]
        units = []
        for i,(flat,anchor,origin) in enumerate(zip(self.flat,self.anchors,origins)):
            if anchor is None:
                units.append(om.MVector())
                continue
            t = self.samples[i]
            denominator = math.sin(math.pi*t)
            factor = profile_value(profile_kind,t,shape,curve)/denominator if denominator>1e-7 else 0
            units.append(((flat-origin)+(self.rounded[i]-flat)*factor)/self.reference_width)
        key = (shape,profile_kind,curve.points,tuple(sorted(weights.items())))
        # Most local gestures change only a few samples by a small amount.
        # Validate that short motion first; a fresh sweep from the collapsed
        # source would unnecessarily revisit the whole bevel on every frame.
        prior = getattr(self,"_independent_previous",None)
        if (key!=self._independent_key and prior and prior[0]==(width,shape,profile_kind,curve.points)
                and not self._independent_contacts):
            target = {a:max(width*max(0,weights.get(a,1)),self.reference_width*.005) for a in self.groups}
            points = [origin+unit*prior[1][a] if a is not None else origin
                      for origin,unit,a in zip(origins,units,self.anchors)]
            movements = [unit*(target[a]-prior[1][a]) if a is not None else om.MVector()
                         for unit,a in zip(units,self.anchors)]
            contact,_ = first_contact(points,movements,self.triangles,1,self._local_contact_cache)
            if min(contact,self._collision(points,movements))>1:
                self._independent_key,self._independent_bound = key,width
                self._independent_limits = dict.fromkeys(self.groups,math.inf)
        if key!=self._independent_key or width>self._independent_bound:
            bound = max(width*1.25,self.reference_width)
            intended = {a:max(0,weights.get(a,1))*bound for a in self.groups}
            current = dict.fromkeys(self.groups,0.0)
            frozen = set()
            caps = {a:math.inf for a in self.groups}
            contacts = []
            for _ in range(len(self.groups)+1):
                points = [origin+unit*current[a] if a is not None else origin
                          for origin,unit,a in zip(origins,units,self.anchors)]
                movements = [unit*(intended[a]-current[a]) if a is not None and a not in frozen
                             else om.MVector() for unit,a in zip(units,self.anchors)]
                collision, faces = first_contact(points,movements,self.triangles,1,self._independent_cache)
                members = set(self._independent_cache.get("contact_vertices",()))
                # Residual faces must retain positive projected area, even
                # when both faces of a contact share a border and CCD skips it.
                for (ids,normal),face in zip(self.constraints,self.constraint_faces):
                    base = [points[i] for i in ids]
                    directions = [movements[i] for i in ids]
                    constant = self._area(base,normal)
                    plus = self._area([p+d for p,d in zip(base,directions)],normal)
                    minus = self._area([p-d for p,d in zip(base,directions)],normal)
                    root = self._positive_root(constant,(plus-minus)*.5,(plus+minus)*.5-constant)
                    border,border_ids = self._border_contact(points,movements,ids)
                    limiting_ids = ids
                    if border<root:
                        root,limiting_ids = border,border_ids
                    if root<collision-1e-8:
                        collision,faces,members = root,(face,),set(limiting_ids)
                    elif root<=collision+1e-8 and root<=1:
                        members.update(limiting_ids)
                        faces = tuple(set(faces+(face,)))
                if collision>1:
                    for a in self.groups:
                        if a not in frozen:
                            current[a] = intended[a]
                    break
                factor = max(0,collision*(1-1e-4))
                for a in self.groups:
                    if a not in frozen:
                        current[a] += (intended[a]-current[a])*factor
                affected = {self.anchors[i] for i in members
                            if self.anchors[i] is not None and movements[i].length()>1e-12}-frozen
                if not affected:
                    # Degenerate input: retain the last safe state.
                    affected = set(self.groups)-frozen
                for a in affected:
                    caps[a] = current[a]
                threshold = min((current[a]/weights.get(a,1) for a in affected
                                 if weights.get(a,1)>1e-12),default=bound)
                contacts.append((threshold,faces))
                frozen.update(affected)
                if len(frozen)==len(self.groups):
                    break
            self._independent_key,self._independent_bound = key,bound
            self._independent_limits,self._independent_contacts = caps,contacts
        field = {a:max(self.reference_width*.005,min(width*max(0,weights.get(a,1)),
                                                   self._independent_limits[a])) for a in self.groups}
        self.contact_faces = tuple(sorted({face for threshold,faces in self._independent_contacts
                                          if width>=threshold*(1-1e-6) for face in faces}))
        self.at_contact = bool(self.contact_faces)
        self.clamped = sum(width*weights.get(a,1)>self._independent_limits[a] for a in self.groups)
        self.effective_width,self.effective_weights = width,None
        self._independent_previous = ((width,shape,profile_kind,curve.points),dict(field))
        return om.MPointArray([(origin+unit*field[a] if a is not None else origin)*self.inverse
                               for origin,unit,a in zip(origins,units,self.anchors)])

    def evaluate(self, width, shape, weights, limits, profile_kind="round", curve=None, keep_equal=True):
        curve = curve or Curve(((0, 0, None), (.5, .5, None), (1, 0, None)), False)
        if not keep_equal:
            self._previous = None
            return self._evaluate_independent(width,shape,weights,limits,profile_kind,curve)
        parameters = (width,shape,profile_kind,curve.points)
        if self._previous and self._previous[0]==parameters and self._previous[1]==weights:
            return om.MPointArray(self._previous[2])
        self.effective_weights = None
        result = om.MPointArray()
        self.clamped = 0
        # Bound width by the first collapse of an original residual face.
        # Short segments along the selected line do not impose a width limit.
        selected_weights = [weights.get(v,1.0) for v in self.groups]
        uniform = bool(selected_weights) and max(selected_weights)-min(selected_weights) <= 1e-9
        shared_limit = min((limits.get(v,math.inf) for v in self.groups),default=math.inf)
        base = min(width,self.uniform_limit) if self.constraints else width
        field = {a:max(base*weights.get(a,1),self.reference_width*.005) for a in self.groups}
        if self.constraints and uniform:
            field = {a:min(value,self.uniform_limit) for a,value in field.items()}
        if self.constraints and not uniform:
            origins = [self.source[a]+(p-self.source[a])*(base/self.reference_width)
                       if a is not None else p for a,p in zip(self.anchors,self.flat)]
            vectors = [(p-self.source[a])*((field[a]-base)/self.reference_width)
                       if a is not None else om.MVector() for a,p in zip(self.anchors,self.flat)]
            factor = min(1,self._collision(origins,vectors)*(1-1e-4))
            field = {a:base+(target-base)*factor for a,target in field.items()}
        for index, flat in enumerate(self.flat):
            anchor = self.anchors[index]
            if anchor is None:
                result.append(flat * self.inverse)
                continue
            requested = field[anchor]
            limit = math.inf if self.constraints else shared_limit if uniform else limits.get(anchor, math.inf)
            actual = min(requested, limit)
            if requested > limit:
                self.clamped += 1
            # Keep a valid strip at zero local weight. The whole zero-width
            # operation is displayed as the original mesh and cannot commit.
            actual = max(actual, self.reference_width * .005)
            t = self.samples[index]
            denominator = math.sin(math.pi * t)
            value = profile_value(profile_kind, t, shape, curve)
            factor = value / denominator if denominator > 1e-7 else 0
            vector = flat - self.source[anchor]
            vector += (self.rounded[index] - flat) * factor
            point = self.source[anchor] + vector * (actual / self.reference_width)
            result.append(point * self.inverse)
        self.effective_width = width
        self.at_contact = False
        local = self._previous and self._previous[0]==parameters and self._previous[1]!=weights
        if self.triangles and width>1e-9 and local:
            previous = [p*self.matrix for p in self._previous[2]]
            vectors = [p*self.matrix-origin for p,origin in zip(result,previous)]
            limit,faces = first_contact(previous,vectors,self.triangles,1,self._local_contact_cache)
            factor = min(1,limit*(1-1e-4))
            if factor<1:
                self.contact_faces = faces
                self.at_contact = True
                self.clamped += 1
                old_field = self._previous[3]
                self.effective_weights = {a:(old_field[a]+(field[a]-old_field[a])*factor)/base
                                          for a in field}
                field = {a:old_field[a]+(value-old_field[a])*factor for a,value in field.items()}
                result = om.MPointArray([(origin+vector*factor)*self.inverse
                                         for origin,vector in zip(previous,vectors)])
        elif self.triangles and width>1e-9:
            origins = [self.source[a] if a is not None else p for a,p in zip(self.anchors,self.flat)]
            vectors = [(p*self.matrix-origin)/width for p,origin in zip(result,origins)]
            # Width motion reuses a cached contact distance; profile and local
            # field changes invalidate it. No new topology per mouse update.
            key = tuple(tuple(round(v,9) for v in vector) for vector in vectors)
            if key!=self._contact_key or (width>self._contact_bound and math.isinf(self._contact_limit)):
                # Test a little ahead of the requested width. Sweeping to a
                # distant theoretical maximum makes small profile edits slow
                # on long curved selections; a checked interval is reusable.
                bound = min(self.uniform_limit,max(width*1.25,self.reference_width))
                self._contact_limit,self._shared_contact_faces = first_contact(origins,vectors,self.triangles,bound,self._contact_cache)
                self._contact_key = key
                self._contact_bound = bound
            self.contact_faces = self._shared_contact_faces
            factor = min(1,self._contact_limit*(1-1e-4)/width)
            self.at_contact = width>=self._contact_limit*(1-1e-4)*(1-1e-6)
            if factor<1:
                self.effective_width = width*factor
                self.clamped += 1
                result = om.MPointArray([(origin+vector*self.effective_width)*self.inverse
                                         for origin,vector in zip(origins,vectors)])
        self._previous = (parameters,dict(weights if self.effective_weights is None else self.effective_weights),
                          om.MPointArray(result),dict(field))
        return result
