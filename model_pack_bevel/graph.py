"""Physical distances on selected edges, including cycles and junctions."""
from __future__ import annotations
import heapq
import math

from .profiles import Curve, falloff_value


class EdgeGraph:
    def __init__(self, points, edges):
        self.points = tuple(tuple(p)[:3] for p in points)
        self.edges = dict(edges)
        self.neighbors = {}
        for edge, (a, b) in self.edges.items():
            length = math.dist(self.points[a], self.points[b])
            if length <= 1e-10:
                raise ValueError("У выбранного ребра нулевая длина.")
            self.neighbors.setdefault(a, []).append((b, length))
            self.neighbors.setdefault(b, []).append((a, length))

    def distances(self, edge, fraction):
        a, b = self.edges[edge]
        length = math.dist(self.points[a], self.points[b])
        queue = [(length * fraction, a), (length * (1 - fraction), b)]
        heapq.heapify(queue)
        result = {}
        while queue:
            distance, vertex = heapq.heappop(queue)
            if vertex in result:
                continue
            result[vertex] = distance
            for neighbor, step in self.neighbors[vertex]:
                if neighbor not in result:
                    heapq.heappush(queue, (distance + step, neighbor))
        # The farthest point can be inside an edge opposite the anchor on a
        # loop, rather than at an existing vertex (an odd or uneven loop).
        radius = max(result.values(), default=0)
        for member, (u, v) in self.edges.items():
            if u not in result or v not in result:
                continue
            if member == edge:
                # This edge contains the anchor; account only for the two
                # subedges, avoiding a fictitious path through that anchor.
                subedges = ((0, result[u], length * fraction),
                            (0, result[v], length * (1 - fraction)))
            else:
                subedges = ((result[u], result[v], math.dist(self.points[u], self.points[v])),)
            for d0, d1, span in subedges:
                position = (d1 + span - d0) / 2
                if 0 <= position <= span:
                    radius = max(radius, d0 + position)
        return result, max(radius, 1e-10)

    def influence(self, edge, fraction, kind="round", curve=None, all_components=False):
        fraction = max(0,min(1,fraction))
        distances, radius = self.distances(edge, fraction)
        curve = curve or Curve()
        result = {v: falloff_value(kind, d / radius, curve) for v, d in distances.items()}
        if not all_components:
            return result
        a,b = self.edges[edge]
        anchor = tuple(x+(y-x)*fraction for x,y in zip(self.points[a],self.points[b]))
        remaining = set(self.neighbors)-set(result)
        while remaining:
            seed = min(remaining)
            component = {seed}; queue = [seed]
            for vertex in queue:
                for neighbor,_ in self.neighbors[vertex]:
                    if neighbor not in component:
                        component.add(neighbor); queue.append(neighbor)
            choices=[]
            for member,(u,v) in self.edges.items():
                if u not in component: continue
                vector = tuple(y-x for x,y in zip(self.points[u],self.points[v]))
                squared = sum(d*d for d in vector)
                t = max(0,min(1,sum((p-x)*d for p,x,d in zip(anchor,self.points[u],vector))/squared))
                point = tuple(x+t*d for x,d in zip(self.points[u],vector))
                choices.append((math.dist(point,anchor),member,t))
            _,member,t = min(choices)
            result.update(self.influence(member,t,kind,curve))
            remaining -= component
        return result
