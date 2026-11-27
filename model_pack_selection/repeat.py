"""Repeat complete face/edge patches along a regular quad lattice."""
from collections import defaultdict,deque
import math


class Faces:
    def __init__(self,data):self.data=data
    def initial(self,face):return face,0
    def step(self,state,axis,sign):
        face,rotation=state;data=self.data
        if len(data.edges[face])!=4:return None
        i=(axis+(0 if sign>0 else 2)-rotation)%4
        edge=data.edges[face][i];other=data.across(face,edge)
        if other is None or len(data.edges[other])!=4:return None
        j=data.edges[other].index(edge)
        return other,(i+rotation+2-j)%4
    def preferred(self,state,routes):
        face,rotation=state;data=self.data;local=data.edges[face]
        scores=[]
        for axis,route in enumerate(routes):
            span=0.
            for direction in (axis+1,axis+3):
                a,b=data.fn.getEdgeVertices(local[(direction-rotation)%4])
                span+=(data.points[a]-data.points[b]).length()*.5
            scores.append((route.closed,len(route.states),span))
        return int(scores[1]>scores[0])


class Edges:
    def __init__(self,data):
        self.data=data;self.pairs=[data.fn.getEdgeVertices(e) for e in range(data.fn.numEdges)]
        self.incident=defaultdict(set)
        for edge,pair in enumerate(self.pairs):
            for vertex in pair:self.incident[vertex].add(edge)
        self.cache={}
    def initial(self,edge):return edge,*self.pairs[edge]
    def continuation(self,edge,vertex):
        faces=self.data.edge_faces[edge]
        if len(faces)>2:return None
        choices=[e for e in self.incident[vertex]-{edge}
                 if len(self.data.edge_faces[e])<=2 and not faces.intersection(self.data.edge_faces[e])]
        return choices[0] if len(choices)==1 else None
    def step(self,state,axis,sign):
        key=state,axis,sign
        if key in self.cache:return self.cache[key]
        edge,a,b=state;data=self.data;result=None
        if axis==0:
            vertex=b if sign>0 else a;other=self.continuation(edge,vertex)
            # Do not travel one-way through an ambiguous pole.
            if other is not None and self.continuation(other,vertex)==edge:
                end=next(v for v in self.pairs[other] if v!=vertex)
                result=(other,vertex,end) if sign>0 else (other,end,vertex)
        elif len(data.edge_faces[edge])<=2:
            choices=[]
            for face in data.edge_faces[edge]:
                if len(data.edges[face])!=4:continue
                i=data.edges[face].index(edge);vertices=data.faces[face]
                forward=(vertices[i],vertices[(i+1)%4])==(a,b)
                if forward!=(sign>0):continue
                other=data.edges[face][(i+2)%4]
                if len(data.edge_faces[other])>2:continue
                u,v=vertices[(i+2)%4],vertices[(i+3)%4]
                choices.append((other,v,u) if forward else (other,u,v))
            if len(choices)==1:result=choices[0]
        self.cache[key]=result;return result
    def preferred(self,state,routes):return 0  # Edge loop; across selects its edge ring.


class Route:
    def __init__(self,states,closed):self.states=states;self.closed=closed


class Vertices:
    """Transport two vertex directions through quads without crossing poles."""
    def __init__(self,data):
        self.data=data;self.edges=Edges(data)
        self.lookup={frozenset(pair):e for e,pair in enumerate(self.edges.pairs)}
    def opposite(self,vertex,neighbor):
        if neighbor is None:return None
        edge=self.lookup[frozenset((vertex,neighbor))]
        other=self.edges.continuation(edge,vertex)
        return next(v for v in self.edges.pairs[other] if v!=vertex) if other is not None else None
    def initial(self,vertex):
        quads=[face for face in self.data.faces if len(face)==4 and vertex in face]
        if not quads:raise ValueError('Для направления вершин нужна полоса четырёхугольников.')
        face=quads[0];i=face.index(vertex);a,b=face[(i+1)%4],face[(i-1)%4]
        return vertex,(a,b,self.opposite(vertex,a),self.opposite(vertex,b))
    def step(self,state,axis,sign):
        vertex,directions=state;index=axis+(0 if sign>0 else 2);other=directions[index]
        if other is None:return None
        edge=self.lookup[frozenset((vertex,other))]
        if len(self.data.edge_faces[edge])>2:return None
        result=[None]*4;result[(index+2)%4]=vertex;result[index]=self.opposite(other,vertex)
        for face_id in self.data.edge_faces[edge]:
            face=self.data.faces[face_id]
            if len(face)!=4:continue
            a=face.index(vertex);b=face.index(other)
            near=face[(a-1)%4] if face[(a+1)%4]==other else face[(a+1)%4]
            far=face[(b-1)%4] if face[(b+1)%4]==vertex else face[(b+1)%4]
            slots=[i for i in ((index+1)%4,(index+3)%4) if directions[i]==near]
            if len(slots)!=1:return None
            result[slots[0]]=far
        return other,tuple(result)
    def preferred(self,state,routes):
        return int(len(routes[1].states)>len(routes[0].states))


class Repeater:
    def __init__(self,grid):self.grid=grid;self.routes={}
    def route(self,state,axis):
        key=state,axis
        if key in self.routes:return self.routes[key]
        first=[state];seen={state[0]};closed=False
        while (other:=self.grid.step(first[-1],axis,1)) is not None:
            if other[0] in seen:
                closed=other==state;break
            first.append(other);seen.add(other[0])
        second=[state]
        if not closed:
            while (other:=self.grid.step(second[-1],axis,-1)) is not None:
                if other[0] in seen:break
                second.append(other);seen.add(other[0])
        states=first if closed else list(reversed(second[1:]))+first
        route=Route(states,closed)
        for i,current in enumerate(states):self.routes[current,axis]=route,i
        return self.routes[key]

    def patch(self,selected,root):
        state=self.grid.initial(root);states={root:state};coords={root:(0,0)};queue=deque([root]);conflicts=[]
        while queue:
            current=queue.popleft();x,y=coords[current]
            for axis in (0,1):
                for sign in (-1,1):
                    other=self.grid.step(states[current],axis,sign)
                    if other is None or other[0] not in selected:continue
                    index=other[0];position=(x+sign,y) if axis==0 else (x,y+sign)
                    if index in states:
                        if states[index]!=other:raise ValueError('Направление группы неоднозначно у полюса. Выберите отдельную полосу четырёхугольников.')
                        conflicts.append((position[0]-coords[index][0],position[1]-coords[index][1]))
                    else:states[index]=other;coords[index]=position;queue.append(index)
        routes=[self.route(state,axis)[0] for axis in (0,1)]
        for difference in conflicts:
            for axis,value in enumerate(difference):
                if value and (not routes[axis].closed or value%len(routes[axis].states)):
                    raise ValueError('Группа пересекает неоднозначный стык. Выберите отдельную регулярную полосу.')
        return states,coords,routes

    def repeated(self,states,coords,axis,skip):
        width=max(p[axis] for p in coords.values())-min(p[axis] for p in coords.values())+1
        period=width+skip;paths={key:self.route(state,axis) for key,state in states.items()}
        lower,upper=-math.inf,math.inf
        for route,index in paths.values():
            if route.closed:lo,hi=0,len(route.states)-width
            else:lo,hi=-index,len(route.states)-1-index
            lower=max(lower,lo);upper=min(upper,hi)
        result=set(states)
        if lower>0 or upper<0:return result
        def copy_at(offset):
            mapped={}
            for source,(route,index) in paths.items():
                i=index+offset
                if route.closed:i%=len(route.states)
                if not 0<=i<len(route.states):return None
                mapped[source]=route.states[i]
            ids={s[0] for s in mapped.values()}
            if len(ids)!=len(states) or ids.intersection(result):return None
            # Preserve the entire group's adjacency/orientation. A copy that
            # crosses a pole or loses a row is stopped instead of clipped.
            for source,state in states.items():
                for direction in (0,1):
                    other=self.grid.step(state,direction,1)
                    if other is not None and other[0] in states:
                        if self.grid.step(mapped[source],direction,1)!=mapped[other[0]]:return None
            return ids
        for direction,count in ((1,math.floor(upper/period)),(-1,math.floor(-lower/period))):
            for multiple in range(1,count+1):
                copied=copy_at(direction*multiple*period)
                if copied is None:break
                result.update(copied)
        return result

    def select(self,selected,across,skip):
        if not isinstance(skip,int) or not 0<=skip<=1000:raise ValueError('Пропуск должен быть целым числом от 0 до 1000.')
        remaining=set(selected);result=set()
        while remaining:
            root=min(remaining);states,coords,routes=self.patch(remaining,root)
            axis=self.grid.preferred(states[root],routes)^bool(across)
            result.update(self.repeated(states,coords,axis,skip));remaining.difference_update(states)
        return result


def faces(data,selected,*,across=False,skip=1):
    selected=data.checked(selected)
    if any(len(data.edges[f])!=4 for f in selected):raise ValueError('Повторение по кольцу требует группы четырёхугольных полигонов.')
    return Repeater(Faces(data)).select(selected,across,skip)


def edges(data,selected,*,across=False,skip=1):
    selected=set(selected)
    if not selected or any(e<0 or e>=data.fn.numEdges for e in selected):raise ValueError('Выберите существующие рёбра.')
    return Repeater(Edges(data)).select(selected,across,skip)


def vertices(data,selected,*,across=False,skip=1):
    selected=set(selected)
    if not selected or any(v<0 or v>=data.fn.numVertices for v in selected):raise ValueError('Выберите существующие вершины.')
    return Repeater(Vertices(data)).select(selected,across,skip)


def grow_components(data,selected,kind,*,shrink=False,narrow=None):
    selected=set(selected)
    grid=Vertices(data) if kind=='vertex' else Edges(data)
    if narrow is None:
        if kind=='vertex':
            neighbors={v:{u for e in grid.edges.incident[v] for u in grid.edges.pairs[e] if u!=v} for v in selected}
        else:
            neighbors={e:set().union(*(grid.incident[v] for v in grid.pairs[e]))-{e} for e in selected}
    else:
        repeater=Repeater(grid);remaining=set(selected);neighbors={}
        while remaining:
            root=min(remaining);states,coords,routes=repeater.patch(remaining,root)
            spans=[max(p[i] for p in coords.values())-min(p[i] for p in coords.values()) for i in (0,1)]
            wide=int(spans[1]>spans[0]) if spans[0]!=spans[1] else grid.preferred(states[root],routes)
            axis=wide^bool(narrow)
            for vertex,state in states.items():
                neighbors[vertex]={other[0] for sign in (-1,1) if (other:=grid.step(state,axis,sign)) is not None}
            remaining.difference_update(states)
    if shrink:return {v for v in selected if neighbors[v] and neighbors[v]<=selected}
    return selected|set().union(*neighbors.values()) if neighbors else selected
