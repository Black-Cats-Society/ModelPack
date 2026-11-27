"""Select and edit individual bevel bands in a mesh's construction history."""
from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations
import json
from statistics import median

from maya import cmds
from maya.api import OpenMaya as om

from slide_for_maya.core import SlideError
from .geometry import source_anchors
from .graph import EdgeGraph
from .session import NODE_NAME, _build, _mesh_fn, _resample_weights, _set_string, _shape


def _input(plug):
    return cmds.connectionInfo(plug, sourceFromDestination=True)


def _base(node):
    attr = 'baseMesh' if cmds.attributeQuery('baseMesh', node=node, exists=True) else 'sourceMesh'
    return _input(f'{node}.{attr}')


def _mesh(plug):
    return om.MFnMesh(om.MSelectionList().add(plug).getPlug(0).asMObject())


def _topology(mesh):
    counts, vertices = mesh.getVertices()
    return mesh.numVertices, tuple(counts), tuple(vertices)


def chain(mesh):
    """Bevels on the visible branch, in application order."""
    plug = _input(_shape(mesh) + '.inMesh')
    nodes = []
    while plug and plug.endswith('.outputMesh'):
        node = plug.rsplit('.', 1)[0]
        if cmds.nodeType(node) != NODE_NAME:
            break
        nodes.append(node)
        plug = _base(node)
    return tuple(reversed(nodes))


def _components(mesh, edges):
    incident = defaultdict(list)
    for edge in edges:
        for vertex in mesh.getEdgeVertices(edge):
            incident[vertex].append(edge)
    remaining = set(edges)
    result = []
    for edge in sorted(remaining):
        if edge not in remaining:
            continue
        group = [edge]
        remaining.remove(edge)
        for member in group:
            for vertex in mesh.getEdgeVertices(member):
                for neighbor in incident.pop(vertex, ()):
                    if neighbor in remaining:
                        group.append(neighbor)
                        remaining.remove(neighbor)
        result.append(tuple(sorted(group)))
    return tuple(result)


def _contours(mesh, edges):
    """Join continuations through junctions without merging their branches."""
    ends = {edge: mesh.getEdgeVertices(edge) for edge in edges}
    incident = defaultdict(list)
    for edge, vertices in ends.items():
        for vertex in vertices:
            incident[vertex].append(edge)
    junction_edges = {edge for members in incident.values() if len(members) > 2 for edge in members}
    if not junction_edges:
        return _components(mesh, edges)
    points = mesh.getPoints()
    lookup = {frozenset(ends[e]): e for e in junction_edges}
    side_normals = defaultdict(list)
    counts, vertices = mesh.getVertices()
    offset = 0
    for face, count in enumerate(counts):
        ring = list(vertices[offset:offset+count])
        offset += count
        for a, b in zip(ring, ring[1:] + ring[:1]):
            edge = lookup.get(frozenset((a, b)))
            if edge is not None:
                side_normals[edge].append(mesh.getPolygonNormal(face))
    links = defaultdict(set)
    for vertex, members in incident.items():
        if len(members) <= 2:
            if len(members) == 2:
                a, b = members
                links[a].add(b)
                links[b].add(a)
            continue
        directions = {}
        for edge in members:
            first, last = ends[edge]
            other = last if first == vertex else first
            directions[edge] = (points[other] - points[vertex]).normal()
        ranks = defaultdict(list)
        for a, b in combinations(members, 2):
            straight = (1. - directions[a] * directions[b]) * .5
            first, second = side_normals[a], side_normals[b]
            match = .5
            if len(first) == len(second) == 2:
                match += .25 * max(first[0] * second[0] + first[1] * second[1],
                                   first[0] * second[1] + first[1] * second[0])
            score = .55 * straight + .45 * match
            ranks[a].append((score, b))
            ranks[b].append((score, a))
        continuation = {}
        for edge, options in ranks.items():
            best, next_best = sorted(options, reverse=True)[:2]
            if best[0] >= .56 and best[0] - next_best[0] > .055:
                continuation[edge] = best[1]
        for a, b in continuation.items():
            if continuation.get(b) == a:
                links[a].add(b)
    remaining = set(edges)
    result = []
    while remaining:
        edge = min(remaining)
        remaining.remove(edge)
        trail = [edge]
        for item in trail:
            linked = links[item] & remaining
            remaining.difference_update(linked)
            trail.extend(sorted(linked))
        result.append(tuple(sorted(trail)))
    return tuple(result)


@dataclass(frozen=True)
class Band:
    mesh: str
    node: str
    edges: tuple


def bands(mesh):
    mesh = _shape(mesh).rsplit('|', 1)[0]
    result = []
    for node in chain(mesh):
        edges = _metadata(node)['edges']
        base = _mesh(_base(node))
        edges = [edge for edge in edges if 0 <= edge < base.numEdges]
        result.extend(Band(mesh, node, group) for group in _contours(base, edges))
    return result


def _source_bands(node, choices):
    """Map template input edges to their original, possibly subdivided edges."""
    base = _mesh(_base(node))
    owners = {frozenset(base.getEdgeVertices(e)): band for edges, band in choices.items() for e in edges}
    original_count = base.numVertices
    limits = (json.loads(cmds.getAttr(node + '.widthLimits')) if _metadata(node).get('cuts') else {})
    source = _mesh(node + '.sourceMesh')
    selected = json.loads(cmds.getAttr(node + '.selectedEdges'))
    result = {}
    for edge in selected:
        pair = frozenset(source.getEdgeVertices(edge))
        original = set()
        for vertex in pair:
            if vertex < original_count:
                original.add(vertex)
            else:
                sample = limits.get(str(vertex), {})
                if isinstance(sample, dict):
                    original.update(v for v, _ in sample.get('blend', ()))
        band = owners.get(frozenset(original))
        if band is not None:
            result[pair] = band
    return result


def _face_owners(node, choices):
    """Map rendered bevel faces to saved source-edge components."""
    if not choices:
        return {}
    output = _mesh(node + '.outputMesh')
    dep = om.MFnDependencyNode(om.MSelectionList().add(node).getDependNode(0))
    implementation = dep.userNode()
    template = getattr(implementation, '_template', None)
    if not template or not template.bevel_faces:
        return {}
    weld = getattr(implementation, '_welded_faces', None)
    if weld:
        counts, vertices = weld[0]
        face_indices = _welded_face_indices(weld, output, template.matrix)
    else:
        counts, vertices = output.getVertices()
        face_indices = {}
    owners = _source_bands(node, choices)
    vertex_owners = defaultdict(set)
    for pair, band in owners.items():
        for v in pair:
            vertex_owners[v].add(band)
    result = {}
    offset = 0
    for face, count in enumerate(counts):
        if face in template.bevel_faces and (not weld or face in face_indices):
            anchors = {a for v in vertices[offset:offset+count] for a in template.anchor_sets[v]}
            matches = {owners[pair] for a, b in combinations(anchors, 2)
                       if (pair := frozenset((a, b))) in owners}
            if not matches:
                matches = set().union(*(vertex_owners[a] for a in anchors))
            if len(matches) == 1:
                result[face_indices[face] if weld else face] = matches.pop()
        offset += count
    return result


def _welded_face_indices(weld, output, matrix):
    """Track the surviving polygons through Maya's native edge collapse."""
    (counts, vertices), points, edges, tolerance = weld
    roots = list(range(len(points)))

    def root(vertex):
        while roots[vertex] != vertex:
            vertex = roots[vertex]
        return vertex

    for a, b in edges:
        roots[root(a)] = root(b)
    candidates = defaultdict(list)
    start = 0
    for face, count in enumerate(counts):
        key = frozenset(root(v) for v in vertices[start:start+count])
        if len(key) > 2:
            candidates[key].append(face)
        start += count
    current = [p * matrix for p in output.getPoints()]
    anchors = source_anchors(points, current, range(len(points)), max(tolerance, 1e-8) / 16)
    result = {}
    for face in range(output.numPolygons):
        ids = [anchors[v] for v in output.getPolygonVertices(face)]
        if None not in ids:
            matches = candidates[frozenset(root(v) for v in ids)]
            if len(matches) == 1:
                result[matches[0]] = face
    return result


def _routes(choices):
    routes = {}
    for band in choices:
        routes.setdefault(band.node, {})[band.edges] = band
    return routes


def _ray_hit(fn, origin, direction, faces=None):
    subset = {} if faces is None else dict(faceIds=faces, idsSorted=True)
    return fn.closestIntersection(om.MFloatPoint(origin), om.MFloatVector(direction),
                                  om.MSpace.kObject, 1e10, False,
                                  accelParams=fn.autoUniformGridParams(), **subset)


def _matched_ray(routes, direction, expected, extent):
    origin = expected - direction.normal() * (4 * extent)
    for node, choices in reversed(tuple(routes.items())):
        owners = _face_owners(node, choices)
        if owners:
            hit = _ray_hit(_mesh(node + '.outputMesh'), origin, direction, sorted(owners))
            # A later bevel may trim earlier polygons. Only the part still
            # visible in the actual output can be picked.
            if hit and (om.MPoint(hit[0]) - expected).length() <= extent:
                return owners[hit[2]]


def pick(meshes, view, anchor, session=None, *, with_face=False):
    origin, direction = om.MPoint(), om.MVector()
    view.viewToWorld(round(anchor[0]), round(anchor[1]), origin, direction)
    hits = []
    for mesh in meshes:
        display = session._display_mesh if session and session.mesh == mesh else mesh
        fn = _mesh_fn(display or mesh)
        inverse = fn.dagPath().inclusiveMatrixInverse()
        local_origin, local_direction = origin * inverse, direction * inverse
        hit = _ray_hit(fn, local_origin, local_direction)
        if hit:
            point = om.MPoint(hit[0])
            distance = (point * fn.dagPath().inclusiveMatrix() - origin).length()
            box = om.MFnDagNode(fn.dagPath()).boundingBox
            tolerance = max(box.width, box.height, box.depth, 1e-5) * 2e-5
            hits.append((distance, mesh, local_direction, point, tolerance, hit[2]))
    if not hits:
        return None
    _, mesh, local_direction, point, tolerance, face = min(hits, key=lambda hit: hit[0])
    if session and session.mesh == mesh and getattr(session, 'history_plan', None):
        routes = session.history_plan.routes(session)
    else:
        routes = _routes(bands(mesh))
    band = _matched_ray(routes, local_direction, point, tolerance)
    return (band, mesh, face) if with_face else band


def from_faces(mesh, faces):
    choices = bands(mesh)
    if not choices:
        return None
    routes = _routes(choices)
    latest = chain(mesh)[-1]
    hits = _face_owners(latest, routes.get(latest, {}))
    owners = {hits.get(face) for face in faces}
    if len(owners) == 1 and None not in owners:
        return owners.pop()
    if owners != {None} or len(routes) <= 1:
        return None
    fn = _mesh_fn(mesh)
    points = fn.getPoints()
    box = om.MFnDagNode(fn.dagPath()).boundingBox
    offset = max(box.width, box.height, box.depth, 1e-5)
    owners = set()
    for face in faces:
        vertices = fn.getPolygonVertices(face)
        position = om.MPoint(*(sum(points[v][i] for v in vertices) / len(vertices) for i in range(3)))
        normal = fn.getPolygonNormal(face)
        owners.add(_matched_ray(routes, -normal, position, offset * 2e-5))
    return owners.pop() if len(owners) == 1 and None not in owners else None


def from_edges(mesh, edges):
    """Resolve visible hard guides to a single saved bevel when possible."""
    choices = bands(mesh)
    if not choices:
        return None
    from .band_overlay import visible_faces

    fn = _mesh_fn(mesh)
    regions = visible_faces(fn, _routes(choices))
    iterator = om.MItMeshEdge(fn.dagPath())
    owners = []
    for edge in edges:
        iterator.setIndex(edge)
        adjacent = {regions[f] for f in iterator.getConnectedFaces() if f in regions}
        if adjacent:
            owners.append(adjacent)
    if len(owners) < 2:
        return None
    matches = set.intersection(*owners)
    return matches.pop() if len(matches) == 1 else None


def _metadata(node):
    if cmds.attributeQuery('sessionMeta', node=node, exists=True):
        return json.loads(cmds.getAttr(node + '.sessionMeta'))
    return dict(edges=json.loads(cmds.getAttr(node + '.selectedEdges')), cuts=0)


def _graph(node):
    selected = json.loads(cmds.getAttr(node + '.selectedEdges'))
    matrix = om.MFnMatrixData(om.MSelectionList().add(node + '.meshMatrix').getPlug(0).asMObject()).matrix()
    mesh = _mesh(node + '.sourceMesh')
    return EdgeGraph([point * matrix for point in mesh.getPoints()],
                     ((edge, mesh.getEdgeVertices(edge)) for edge in selected))


def _weights(node):
    return {int(vertex): weight for vertex, weight in json.loads(cmds.getAttr(node + '.widthWeights')).items()}


class _EdgeReferences:
    def __init__(self, mesh, edges):
        self.edges = tuple(edges)
        self.topology = _topology(mesh)
        self.points = mesh.getPoints()
        self.ends = [mesh.getEdgeVertices(edge) for edge in self.edges]

    def resolve(self, mesh):
        if not self.edges or self.topology == _topology(mesh):
            return self.edges
        # Other disconnected bands leave these source edges in place. Recover
        # their new IDs only if both endpoints still identify a unique edge.
        candidates = defaultdict(list)
        points = mesh.getPoints()
        for edge in range(mesh.numEdges):
            a, b = mesh.getEdgeVertices(edge)
            candidates[_edge_key(points, a, b)].append(edge)
        mapped = [candidates[_edge_key(self.points, a, b)] for a, b in self.ends]
        if any(len(matches) != 1 for matches in mapped):
            raise SlideError('Изменение затрагивает исходные рёбра другой фаски.')
        return tuple(matches[0] for matches in mapped)


def _edge_key(points, a, b):
    return tuple(sorted((tuple(points[a])[:3], tuple(points[b])[:3])))


class _Stage:
    """A saved stage that will follow the edited bevel in the final mesh."""

    def __init__(self, node, edges=None):
        self.node = node
        self.insert = edges is not None
        self.meta = _metadata(node)
        self.refs = _EdgeReferences(_mesh(_base(node)), self.meta['edges'] if edges is None else edges)
        self.sharp = self.meta.get('sharp', False)
        self.supports = self.meta.get('support_edges', 0) if self.sharp else self.meta.get('round_supports', 0)
        self.numbers = {name: cmds.getAttr(node + '.' + name) for name in ('width', 'shape', 'segments')}
        if self.sharp and not (self.meta.get('support_spacing') or self.meta.get('support_percent')):
            self.supports = 0
        self.strings = {name: cmds.getAttr(node + '.' + name) for name in
                        ('profileKind', 'profileCurve', 'falloffKind', 'falloffCurve')}
        self.weights = _weights(node)
        self.graph = _graph(node) if self.weights else None

    def build(self, session, plug, *, preview):
        edges = self.refs.resolve(_mesh(plug))
        stage = session._base_copy('bevelFollow#', plug)
        cmds.setAttr(stage + '.visibility', False)
        cmds.connectAttr(plug, _shape(stage) + '.inMesh', force=True)
        if not edges:
            return stage, None, [], edges
        node, graph, owned = _build(stage, edges, self.numbers['segments'], self.meta['cuts'],
                                    self.meta.get('junction_fill', False), sharp=self.sharp,
                                    support_edges=self.supports)
        if self.insert or preview:
            for name, value in self.numbers.items():
                cmds.setAttr(node + ('.segmentCount' if name == 'segments' else '.' + name), value)
            for name, value in self.strings.items():
                _set_string(node, name, value)
        weights = _resample_weights(self.graph, self.weights, graph) if self.weights else {}
        _set_string(node, 'widthWeights', weights)
        _set_string(node, 'sessionMeta', dict(self.meta, edges=list(edges)))
        return stage, node, owned, edges


class HistoryEdit:
    def __init__(self, band, selected_bands=None):
        self.band = band
        self.selected_bands = tuple(selected_bands or (band,))
        if any(item.mesh != band.mesh or item.node != band.node for item in self.selected_bands):
            raise SlideError('Выберите фаски из одной операции Bevel.')
        choices = bands(band.mesh)
        self._routes = _routes(choices)
        self.index = choices.index(band) + 1
        meta = _metadata(band.node)
        base = _mesh(_base(band.node))
        selected = {edge for item in self.selected_bands for edge in item.edges}
        remaining = tuple(edge for edge in meta['edges'] if edge not in selected)
        vertices = {v for edge in selected for v in base.getEdgeVertices(edge)}
        self.joined = any(vertices.intersection(base.getEdgeVertices(edge)) for edge in remaining)
        self.edges = tuple(meta['edges']) if self.joined else tuple(sorted(selected))
        nodes = chain(band.mesh)
        index = nodes.index(band.node)
        self.head = nodes[:index]
        self.stages = []
        if remaining and not self.joined:
            self.stages.append(_Stage(band.node, remaining))
        self.stages.extend(_Stage(node) for node in nodes[index+1:])
        self._graph = _graph(band.node)
        self._weights = _weights(band.node)
        if self.joined:
            self._global_width = cmds.getAttr(band.node + '.width')
            spacing = meta.get('support_spacing')
            if spacing is None:
                spacing = self._global_width * (meta.get('support_percent') or 0.) / 100.
            self._outside = 0. if meta.get('sharp') else meta.get('round_supports', 0) * spacing
            source = _source_bands(band.node, self._routes[band.node])
            self.guide_edges = {edge for edge, vertices in self._graph.edges.items()
                                if source.get(frozenset(vertices)) in self.selected_bands}
            selected_vertices = {v for pair, owner in source.items() if owner in self.selected_bands for v in pair}
            shared = {v for pair, owner in source.items() if owner not in self.selected_bands for v in pair}
            self._editable = selected_vertices - shared or selected_vertices
            if not self._editable:
                raise SlideError('Не удалось сопоставить контур с рёбрами истории фаски.')
            span = self._global_width + self._outside
            self.width = max(0., median(self._weights.get(v, 1.) * span - self._outside for v in self._editable))
        self._preview = []
        self._preview_key = None
        self.blocked = None
        self.original = None
        self.committed_routes = {}

    def unchanged(self, session):
        return self.original is None or session.snapshot() == self.original

    def routes(self, session):
        if self.blocked or self.unchanged(session):
            return self._routes
        routes = {node: self._routes[node] for node in self.head if node in self._routes}
        if session.node:
            routes[session.node] = (self._routes[self.band.node] if self.joined else
                                    {item.edges: item for item in self.selected_bands}
                                    if len(self.selected_bands) > 1 else {session.edges: self.band})
        for recipe, (_, node, _, edges) in zip(self.stages, self._preview):
            if node:
                mapping = dict(zip(recipe.refs.edges, edges))
                routes[node] = {tuple(mapping[e] for e in group): band
                                for group, band in self._routes[recipe.node].items()
                                if all(e in mapping for e in group)}
        return routes

    def weights(self, graph):
        if self.joined:
            return {}
        return _resample_weights(self._graph, self._weights, graph)

    def width_field(self, width, weights):
        """Encode a contour edit in the node's shared, per-vertex width field."""
        span = max(self._global_width + self._outside, 1e-12)
        result = dict(self._weights)
        for vertex in self._editable:
            delta = width * weights.get(vertex, 1.) - self.width
            if abs(delta) > 1e-12:
                result[vertex] = max(0., self._weights.get(vertex, 1.) + delta / span)
        return self._global_width, result

    def clear(self):
        owned = [obj for stage, _, nodes, _ in self._preview for obj in (stage, *nodes)]
        if owned:
            cmds.delete([obj for obj in owned if cmds.objExists(obj)])
        self._preview.clear()
        self._preview_key = None

    def preview(self, session, display):
        if self.unchanged(session):
            self.clear()
            self.blocked = None
            cmds.setAttr(session.mesh + '.visibility', session.visible)
            return session.mesh
        if not self.stages:
            return display
        plug = _shape(session.preview) + '.outMesh'
        key = _topology(_mesh(plug))
        if key != self._preview_key:
            self.clear()
            try:
                for recipe in self.stages:
                    build = recipe.build(session, plug, preview=True)
                    self._preview.append(build)
                    plug = _shape(build[0]) + '.outMesh'
            except SlideError as exc:
                self.clear()
                self.blocked = str(exc)
            else:
                self.blocked = None
            self._preview_key = key
        if self.blocked:
            cmds.setAttr(session.mesh + '.visibility', session.visible)
            return session.mesh
        stage = self._preview[-1][0]
        cmds.setAttr(stage + '.visibility', session.visible)
        for group in session.isolate_sets:
            if cmds.objExists(group) and not cmds.sets(stage, isMember=group):
                cmds.sets(stage, add=group)
        cmds.setAttr(session.mesh + '.visibility', False)
        return stage

    def commit(self, session):
        node = self.band.node if self.joined else session._replace_history()
        session._write(node)
        plug = node + '.outputMesh'
        inserted = []
        for recipe in self.stages:
            if not recipe.insert and recipe.refs.topology == _topology(_mesh(plug)) and _base(recipe.node) == plug:
                plug = recipe.node + '.outputMesh'
                continue
            stage, temporary, owned, edges = recipe.build(session, plug, preview=False)
            if recipe.insert:
                cmds.connectAttr(session.mesh + '.worldMatrix[0]', temporary + '.meshMatrix', force=True)
                for sample in owned:
                    if cmds.attributeQuery('sharpJoin', node=sample, exists=True):
                        cmds.connectAttr(session.mesh + '.worldMatrix[0]', sample + '.meshMatrix', force=True)
                inserted.append(stage)
                plug = temporary + '.outputMesh'
                saved = temporary
            else:
                weights = cmds.getAttr(temporary + '.widthWeights') if temporary else '{}'
                replacement = (stage, temporary, owned) if temporary else None
                session._replace_templates(recipe.node, plug, replacement,
                                           sharp=recipe.sharp, supports=recipe.supports)
                _set_string(recipe.node, 'sessionMeta', dict(recipe.meta, edges=list(edges)))
                _set_string(recipe.node, 'widthWeights', weights)
                if not temporary:
                    cmds.delete(stage)
                plug = recipe.node + '.outputMesh'
                saved = recipe.node
            mapping = dict(zip(recipe.refs.edges, edges))
            for group, band in self._routes[recipe.node].items():
                if all(e in mapping for e in group):
                    self.committed_routes[band] = Band(band.mesh, saved, tuple(sorted(mapping[e] for e in group)))
        destination = _shape(session.mesh) + '.inMesh'
        if _input(destination) != plug:
            cmds.connectAttr(plug, destination, force=True)
        if inserted:
            cmds.delete(inserted)
        return node
