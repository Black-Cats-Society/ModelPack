"""A transparent viewport helper, with no scene nodes or selection side effects."""
import copy
from PySide6 import QtCore,QtGui,QtWidgets
from shiboken6 import wrapInstance,isValid
from maya import cmds
from maya.api import OpenMaya as om,OpenMayaUI as omui
from . import core,components

_RETAINED=[]


class Projection:
    """One camera snapshot, float pixels and homogeneous frustum clipping."""
    def __init__(self,view,width=None,height=None):
        self.view=view;self.matrix=None
        try:
            matrix=view.modelViewMatrix()*view.projectionMatrix()
            self.width=width if width is not None else view.portWidth()
            self.height=height if height is not None else view.portHeight()
            self.matrix=matrix
        except (AttributeError,RuntimeError):
            self.width=width;self.height=height

    def clip_point(self,point):
        p=om.MPoint(point)*self.matrix
        return (p.x,p.y,p.z,p.w)

    @staticmethod
    def distances(p):
        x,y,z,w=p
        return (w+x,w-x,w+y,w-y,w+z,w-z,w-1e-10)

    @staticmethod
    def mix(a,b,t):return tuple(x+(y-x)*t for x,y in zip(a,b))

    def pixel(self,p):
        return ((p[0]/p[3]+1)*self.width*.5,(p[1]/p[3]+1)*self.height*.5)

    def point(self,point):
        if self.matrix is None:
            x,y,_=self.view.worldToView(om.MPoint(point))
            # worldToView's bool also rejects points outside the screen.
            return float(x),float(y),True
        p=self.clip_point(point);x,y=self.pixel(p) if p[3]>1e-10 else (0.,0.)
        return x,y,all(d>=0 for d in self.distances(p))

    def polygon(self,points):
        if self.matrix is None:return [self.point(p)[:2] for p in points]
        polygon=[self.clip_point(p) for p in points]
        for plane in range(7):
            result=[]
            for a,b in zip(polygon,polygon[1:]+polygon[:1]):
                da,db=self.distances(a)[plane],self.distances(b)[plane]
                if da>=0:result.append(a)
                if (da>=0)!=(db>=0):result.append(self.mix(a,b,da/(da-db)))
            polygon=result
            if not polygon:break
        return [self.pixel(p) for p in polygon]

    def line(self,a,b):
        if self.matrix is None:return self.point(a)[:2],self.point(b)[:2]
        a,b=self.clip_point(a),self.clip_point(b)
        for plane in range(7):
            da,db=self.distances(a)[plane],self.distances(b)[plane]
            if da<0 and db<0:return None
            if (da>=0)!=(db>=0):
                intersection=self.mix(a,b,da/(da-db))
                if da<0:a=intersection
                else:b=intersection
        return self.pixel(a),self.pixel(b)


class Section:
    def __init__(self,chain,axis=None):
        self.chain=chain;self.mode='local';self.up_axis=None;self.axis_choice=axis
        if axis not in (None,'x','y','z'):raise ValueError('Неизвестная ось плоскости.')
        guides=[om.MVector(end)-om.MVector(chain.points[v]) for rails in chain.rails.values() for v,end in rails.items()]
        plane=chain.plane if getattr(chain,'kind','edge')!='edge' else core.fit_plane(list(chain.points.values()),guides,allow_secondary=chain.closed)
        self.can_tilt=plane is not None
        if plane is None:
            # A straight selected row still needs a visible helper. Its rails
            # define the transverse direction, while movement keeps line mode.
            center=sum((om.MVector(p) for p in chain.points.values()),om.MVector())/len(chain.points)
            tangent=om.MVector(chain.points[chain.vertices[-1]])-om.MVector(chain.points[chain.vertices[0]])
            if tangent.length()<1e-12 or not guides:raise ValueError('Нет плоскости сечения.')
            tangent=tangent.normal();normal=max((g-tangent*(g*tangent) for g in guides),key=lambda g:g.length())
            if normal.length()<1e-12:raise ValueError('Нет поперечных направляющих.')
            plane=center,normal.normal()
        if axis is not None:
            center=sum((om.MVector(p) for p in chain.points.values()),om.MVector())/len(chain.points)
            plane=center,om.MVector(*{'x':(1,0,0),'y':(0,1,0),'z':(0,0,1)}[axis])
            self.can_tilt=True
        self.center,self.normal=plane
        self.slide_axis=om.MVector(self.normal)
        has_source_rails=any(chain.external.values()) if getattr(chain,'kind','edge')!='edge' else False
        self.slide_field_axis=components.guide_axis(chain) if has_source_rails else self.slide_axis
        if getattr(chain,'kind','edge')=='edge' and guides:
            seed=max(guides,key=lambda vector:vector.length())
            self.slide_field_axis=sum((g if g*seed>=0 else -g for g in guides),om.MVector()).normal()
        self.slide_directions=components.guide_directions(chain,self.slide_field_axis) if getattr(chain,'kind','edge')!='edge' else None
        if axis is not None:
            self.slide_field_axis=om.MVector(self.slide_axis)
            if self.slide_directions is not None:
                self.slide_directions={v:om.MVector(self.slide_axis) for v in chain.vertices}
        self.hinge=None
        pinned=chain.pinned
        if self.axis_choice is not None and getattr(chain,'kind','edge')=='edge':
            pinned=pinned&{chain.vertices[0],chain.vertices[-1]} if not chain.closed else frozenset()
        self.pinned=pinned
        pins=[om.MVector(chain.points[v]) for v in sorted(pinned)]
        if len(pins)>=2:
            first=pins[0];last=max(pins,key=lambda p:(p-first).length());axis=last-first
            axis=axis-self.normal*(axis*self.normal)
            if axis.length()>chain.length*1e-8 and all((axis^(p-first)).length()<chain.length*.001*axis.length() for p in pins):
                self.hinge=(first,axis.normal())
        segments=getattr(chain,'segments',tuple(zip(chain.vertices,chain.vertices[1:]+(chain.vertices[:1] if chain.closed else ()))))
        vectors=[om.MVector(chain.points[b])-om.MVector(chain.points[a]) for a,b in segments]
        projected=[v-self.normal*(v*self.normal) for v in vectors]
        axis=max(projected,key=lambda v:v.length(),default=om.MVector())
        if axis.length()<1e-10:
            axis=min((om.MVector(1,0,0),om.MVector(0,1,0),om.MVector(0,0,1)),key=lambda a:abs(a*self.normal))
            axis=axis-self.normal*(axis*self.normal)
        self.u=self.hinge[1] if self.hinge else axis.normal()
        self.v=(self.normal^self.u).normal()
        coords=[(self.u*(om.MVector(p)-self.center),self.v*(om.MVector(p)-self.center)) for p in chain.points.values()]
        low_u,high_u=min(p[0] for p in coords),max(p[0] for p in coords)
        low_v,high_v=min(p[1] for p in coords),max(p[1] for p in coords)
        padding=max(high_u-low_u,high_v-low_v,chain.length*.01)*.06
        self.bounds=(low_u-padding,high_u+padding,low_v-padding,high_v+padding)
        if self.hinge:
            # Make a side of the helper coincide with the real fixed hinge.
            # Padding beyond it would create dots which seem to move despite
            # the mesh endpoints being pinned.
            hinge_v=(self.hinge[0]-self.center)*self.v;tolerance=chain.length*.001
            a,b,c,d=self.bounds
            if high_v<=hinge_v+tolerance:d=hinge_v
            elif low_v>=hinge_v-tolerance:c=hinge_v
            self.bounds=(a,b,c,d)
        a,b,c,d=self.bounds
        self.corners=[self.point(x,y) for x,y in ((a,c),(b,c),(b,d),(a,d))]
        self.controls=self.corners+[(p+self.corners[(i+1)%4])*.5 for i,p in enumerate(self.corners)]

    def normal_axis(self):
        return max(zip('xyz',tuple(self.slide_axis)),key=lambda item:abs(item[1]))[0]

    def with_axis(self,axis):
        # Explicit axes establish a new movement reference for both the
        # frame and its components. Only subsequent tilts keep that reference.
        return Section(self.chain,axis=axis)

    def in_mode(self,mode,up_axis):
        if mode=='world':return self.with_axis(self.normal_axis()).oriented(mode,up_axis)
        if self.mode=='world' or (mode=='local' and self.mode!='local' and self.axis_choice is not None):
            section=Section(self.chain)
            return section.oriented(mode,up_axis)
        return self.oriented(mode,up_axis)

    def oriented(self,mode,up_axis,points=None):
        """Change the frame's in-plane axes, retaining the section plane."""
        section=self.rebind(self.chain);section.mode=mode;section.up_axis=up_axis
        if mode=='world':
            # Absolute world basis: X -> YZ, Y -> XZ, Z -> XY.
            axis=self.normal_axis()
            section.u=om.MVector(0,1,0) if axis=='x' else om.MVector(1,0,0)
            section.v=(self.normal^section.u).normal()
        elif mode=='global':
            up=om.MVector(0,0,1) if up_axis=='z' else om.MVector(0,1,0)
            u=up^self.normal
            if u.length()<1e-8:
                # A horizontal section has no unique top edge. Choose a
                # stable world axis; both frame axes already lie in the floor.
                x=om.MVector(1,0,0);u=x-self.normal*(x*self.normal)
            section.u=u.normal();section.v=(self.normal^section.u).normal()
        else:
            segments=getattr(self.chain,'segments',tuple(zip(self.chain.vertices,self.chain.vertices[1:]+(self.chain.vertices[:1] if self.chain.closed else ()))))
            vectors=[om.MVector(self.chain.points[b])-om.MVector(self.chain.points[a]) for a,b in segments]
            projected=[v-self.normal*(v*self.normal) for v in vectors]
            axis=max(projected,key=lambda v:v.length(),default=self.u)
            section.u=axis.normal() if axis.length()>1e-10 else self.u;section.v=(self.normal^section.u).normal()
        support=(om.MVector(p) for p in (points if points is not None else self.chain.points.values())) if mode in ('global','world') else iter(self.corners)
        coords=[(section.u*(p-section.center),section.v*(p-section.center)) for p in support]
        a,b=min(p[0] for p in coords),max(p[0] for p in coords)
        c,d=min(p[1] for p in coords),max(p[1] for p in coords)
        minimum=max(self.chain.length*1e-8,1e-10)
        if b-a<minimum:a-=minimum*.5;b+=minimum*.5
        if d-c<minimum:c-=minimum*.5;d+=minimum*.5
        section.bounds=(a,b,c,d);section.hinge=None
        section.corners=[section.point(x,y) for x,y in ((a,c),(b,c),(b,d),(a,d))]
        section.controls=section.corners+[(p+section.corners[(i+1)%4])*.5 for i,p in enumerate(section.corners)]
        return section

    def point(self,u,v):return self.center+self.u*u+self.v*v

    def coordinates(self,point):
        # A tilted helper can be a parallelogram because its corners follow
        # the slide direction. Preserve it exactly, using its dual basis for
        # picking instead of assuming the two sides stay perpendicular.
        offset=om.MVector(point)-self.center;dot=self.u*self.v;denominator=1.-dot*dot
        return ((offset*self.u-dot*(offset*self.v))/denominator,
                (offset*self.v-dot*(offset*self.u))/denominator)

    def matches(self,chain):
        previous=self.chain
        return (previous.topology==chain.topology and core.target_key(previous)==core.target_key(chain) and
                previous.points==chain.points and previous.rails==chain.rails and previous.pinned==chain.pinned and
                getattr(previous,'external',None)==getattr(chain,'external',None) and
                getattr(previous,'support',None)==getattr(chain,'support',None))

    def rebind(self,chain):
        section=copy.copy(self);section.chain=chain
        return section

    def completed(self,gesture,side,chain):
        section=self.rebind(chain)
        section.can_tilt=gesture.plane is not None
        section.corners=[self.moved(p,gesture,side) for p in self.corners]
        section.controls=[self.moved(p,gesture,side) for p in self.controls]
        section.center=sum(section.corners,om.MVector())*.25
        u=section.corners[1]-section.corners[0];v=section.corners[3]-section.corners[0]
        section.u=u.normal();section.v=v.normal()
        section.normal=(section.u^section.v).normal()
        if section.normal*self.normal<0:section.normal=-section.normal
        section.bounds=(-u.length()*.5,u.length()*.5,-v.length()*.5,v.length()*.5)
        section.hinge=None
        support=dict(gesture.chain.points);support.update(gesture.current)
        # World alignment defines the initial reference. During a drag the
        # frame follows the tilted section, retaining its exact final pose.
        return section.oriented('global',self.up_axis,points=support.values()) if self.mode=='global' else section

    def opposite(self,point):
        a,b,c,d=self.bounds;u,v=(a+b)*.5,(c+d)*.5
        x,y=self.coordinates(point);x-=u;y-=v
        scale=max(abs(x)/((b-a)*.5),abs(y)/((d-c)*.5))
        if scale<1e-8:return self.corners[0]
        return self.point(u-x/scale,v-y/scale)

    def fixed(self,point):
        return self.hinge is not None and ((point-self.hinge[0])^self.hinge[1]).length()<self.chain.length*.001

    def hit(self,view,anchor):
        origin,direction=om.MPoint(),om.MVector()
        view.viewToWorld(int(anchor[0]),int(anchor[1]),origin,direction)
        denominator=direction*self.normal
        if abs(denominator)<1e-8:return None
        distance=((self.center-om.MVector(origin))*self.normal)/denominator
        if distance<0:return None
        point=om.MVector(origin)+direction*distance
        u,v=self.coordinates(point);a,b,c,d=self.bounds
        return point if a<=u<=b and c<=v<=d else None

    def moved(self,point,gesture,side):
        if gesture is None or gesture.plane is None or gesture.current_plane is None:return point
        if isinstance(gesture,components.Gesture):return gesture.moved(point)
        normal,offset=gesture.current_plane;direction=getattr(gesture,'helper_axis',gesture.guides[side]).normal()
        denominator=normal*direction
        if abs(denominator)<1e-10:return point
        return point+direction*((offset-normal*(point-gesture.center))/denominator)


def nearest_edge(chain,point):
    candidates=[];point=om.MVector(point)
    if getattr(chain,'kind','edge')!='edge':
        return min(chain.vertices,key=lambda v:(om.MVector(chain.points[v])-point).length()),0.
    for i,edge in enumerate(chain.edges):
        a=om.MVector(chain.points[chain.vertices[i]])
        b=om.MVector(chain.points[chain.vertices[(i+1)%len(chain.vertices)]])
        vector=b-a;fraction=max(0.,min(1.,((point-a)*vector)/(vector*vector)))
        candidates.append(((point-a-vector*fraction).length(),edge,fraction))
    _,edge,fraction=min(candidates)
    return edge,fraction


class Canvas(QtWidgets.QWidget):
    def __init__(self,parent,manager):
        # Maya's viewport is a native GPU surface. A translucent child widget
        # over that surface can leave its raster backing store uncleared and
        # hide the GPU image on Windows. Use a separate composited tool window
        # owned by Maya, positioned over the drawing region instead.
        flags=(QtCore.Qt.Tool|QtCore.Qt.FramelessWindowHint|QtCore.Qt.NoDropShadowWindowHint|
               QtCore.Qt.WindowTransparentForInput|QtCore.Qt.WindowDoesNotAcceptFocus)
        super().__init__(parent.window(),flags);self.manager=manager;self.viewport=parent
        self.setObjectName('modelPackPlaneSlideOverlay')
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(QtCore.Qt.NoFocus)

    def paintEvent(self,event):
        painter=QtGui.QPainter(self)
        try:self.paint_frame(painter)
        finally:painter.end()

    def paint_frame(self,painter):
        # Transparency blended with SourceOver does not erase old pixels.
        # Replace the complete previous frame, including when nothing is drawn.
        painter.save();painter.setCompositionMode(QtGui.QPainter.CompositionMode_Source)
        painter.fillRect(self.rect(),QtCore.Qt.transparent)
        painter.setCompositionMode(QtGui.QPainter.CompositionMode_SourceOver)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        projection=Projection(self.manager.view,self.width(),self.height())
        try:
            for section in self.manager.sections:self.draw_section(painter,section,projection)
        except (RuntimeError,ValueError):pass
        finally:painter.restore()

    def draw_section(self,painter,section,projection):
        drag=self.manager.drag;gesture=drag['gesture'] if drag and core.target_key(drag['gesture'].chain)==core.target_key(section.chain) else None
        side=drag.get('side',next(iter(gesture.guides))) if gesture else None
        def project(point):
            x,y,visible=projection.point(point)
            return pixel_screen((x,y)),visible
        def pixel_screen(p):return QtCore.QPointF(p[0],self.height()-1-p[1])
        def line(a,b):
            segment=projection.line(a,b)
            if segment is not None:painter.drawLine(pixel_screen(segment[0]),pixel_screen(segment[1]))
        displayed=section.completed(gesture,side,section.chain) if gesture is not None and section.mode=='global' else None
        corners=displayed.corners if displayed is not None else [section.moved(p,gesture,side) for p in section.corners]
        clipped=projection.polygon(corners)
        painter.setPen(QtGui.QPen(QtGui.QColor(150,225,75,220),1.5))
        painter.setBrush(QtGui.QColor(110,205,85,35))
        painter.setPen(QtCore.Qt.NoPen)
        if clipped:painter.drawPolygon(QtGui.QPolygonF([pixel_screen(p) for p in clipped]))
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.setPen(QtGui.QPen(QtGui.QColor(150,225,75,220),1.5))
        for a,b in zip(corners,corners[1:]+corners[:1]):line(a,b)
        chain_points={v:gesture.current.get(v,section.chain.points[v]) if gesture else section.chain.points[v] for v in section.chain.vertices}
        painter.setPen(QtGui.QPen(QtGui.QColor(190,250,105,230),2))
        segments=getattr(section.chain,'segments',tuple(zip(section.chain.vertices,section.chain.vertices[1:]+(section.chain.vertices[:1] if section.chain.closed else ()))))
        for a,b in segments:line(chain_points[a],chain_points[b])
        if getattr(section.chain,'kind','edge')!='edge':
            painter.setBrush(QtGui.QColor(190,250,105,230))
            for point in chain_points.values():
                screen,visible=project(point)
                if visible:painter.drawEllipse(screen,2.,2.)
        # Control dots describe the helper, not new vertices in the mesh.
        for i,point in enumerate(section.controls):
            screen,visible=project(displayed.controls[i] if displayed is not None else section.moved(point,gesture,side))
            if not visible:continue
            source=point
            if displayed is not None and gesture.plane is not None:
                source=displayed.controls[i];direction=getattr(gesture,'helper_axis',gesture.guides[side]).normal()
                denominator=gesture.normal*direction
                if abs(denominator)>1e-10:source=source-direction*((gesture.normal*(source-gesture.center))/denominator)
            fixed=gesture is not None and gesture.plane is not None and abs(gesture.gradient*(source-gesture.center)+gesture.offset)<.01
            color=QtGui.QColor(255,195,80,230) if fixed else QtGui.QColor(235,95,65,230)
            painter.setPen(QtGui.QPen(color,1.5));painter.setBrush(QtGui.QColor(color.red(),color.green(),color.blue(),150))
            painter.drawEllipse(screen,5.5,5.5)
        for vertex in section.pinned:
            screen,visible=project(section.chain.points[vertex])
            if visible:
                painter.setPen(QtGui.QPen(QtGui.QColor(255,200,70),2));painter.setBrush(QtCore.Qt.NoBrush)
                painter.drawRect(QtCore.QRectF(screen.x()-4,screen.y()-4,8,8))
        if gesture is not None:
            grip=drag.get('grip')
            if grip is not None:
                screen,visible=project(section.moved(om.MVector(grip),gesture,side))
                if visible:
                    painter.setPen(QtGui.QPen(QtGui.QColor(255,255,225),2));painter.setBrush(QtCore.Qt.NoBrush)
                    painter.drawEllipse(screen,8,8)


class Overlay(QtCore.QObject):
    def __init__(self,mode='local'):
        super().__init__();self.sections=[];self.history=list(_RETAINED);self.drag=None;self.canvas=None;self.view=None
        self.mode=mode;self.up_axis=cmds.upAxis(q=True,axis=True)
        self.render_callback=None;self.panel=None;self.painting=False
        self.timer=QtCore.QTimer(self);self.timer.setInterval(33);self.timer.timeout.connect(self.tick)
        self.timer.start()

    def sync(self,chains):
        sections=[]
        for chain in chains:
            try:
                previous=next((s for s in reversed(self.history) if s.matches(chain) and s.mode==self.mode and
                    (self.mode!='global' or s.up_axis==self.up_axis)),None)
                if previous is not None:section=previous.rebind(chain)
                else:
                    source=next((s for s in reversed(self.history) if s.matches(chain)),None)
                    if source is None:
                        source=next((s for s in reversed(self.history) if s.chain.topology==chain.topology and
                            getattr(s.chain,'kind','edge')!=getattr(chain,'kind','edge') and
                            set(s.chain.vertices)==set(chain.vertices) and s.chain.points==chain.points and
                            (getattr(s.chain,'kind','edge')=='edge' or getattr(chain,'kind','edge')=='edge' or
                             s.chain.external==chain.external)),None)
                    section=source.rebind(chain).in_mode(self.mode,self.up_axis) if source is not None else Section(chain).in_mode(self.mode,self.up_axis)
                sections.append(section)
                if previous is None:self.remember(section)
            except (ValueError,RuntimeError):continue
        self.sections=sections
        self.tick()
        if self.canvas is not None:self.canvas.update()

    def remember(self,section):
        self.history=[s for s in self.history if not (s.matches(section.chain) and s.mode==section.mode and s.up_axis==section.up_axis)]+[section]
        self.history=self.history[-64:]

    def preserve(self,drag,chains):
        gesture=drag['gesture'];side=drag.get('side',next(iter(gesture.guides)))
        section=drag.get('section') or next((s for s in self.sections if s.chain is gesture.chain),None)
        chain=next((c for c in chains if c.topology==gesture.chain.topology and core.target_key(c)==core.target_key(gesture.chain)),None)
        if section is not None and chain is not None:self.remember(section.completed(gesture,side,chain))

    def unbind_render(self):
        if self.render_callback is not None:
            try:om.MMessage.removeCallback(self.render_callback)
            except RuntimeError:pass
        self.render_callback=None;self.panel=None

    def bind_render(self,pointer):
        if self.render_callback is not None:return
        try:
            for panel in cmds.getPanel(type='modelPanel') or []:
                view=omui.M3dView.getM3dViewFromModelPanel(panel)
                if int(view.widget())==int(pointer):
                    self.render_callback=omui.MUiMessage.add3dViewPostRenderMsgCallback(panel,self.rendered)
                    self.panel=panel;break
        except (AttributeError,RuntimeError):pass

    def rendered(self,panel,*_):
        if self.painting or panel!=self.panel or self.canvas is None:return
        try:
            view=omui.M3dView.getM3dViewFromModelPanel(panel)
            if not isValid(self.canvas) or int(view.widget())!=int(getattr(self,'pointer',0)):return
            self.view=view;self.painting=True
            # Repaint alongside the native frame, rather than at a separate
            # 30 Hz camera sampling rate. Never request another Maya refresh.
            if self.canvas.isVisible():self.canvas.repaint()
        except (AttributeError,RuntimeError):pass
        finally:self.painting=False

    def tick(self):
        try:
            self.view=omui.M3dView.active3dView();pointer=self.view.widget()
            if not pointer:self.clear_canvas();return
            parent=wrapInstance(int(pointer),QtWidgets.QWidget)
            if self.canvas is not None and (not isValid(self.canvas) or self.canvas.viewport!=parent):self.clear_canvas()
            if self.canvas is None:self.canvas=getattr(self,'canvas_class',Canvas)(parent,self)
            self.pointer=int(pointer);self.bind_render(pointer)
            if not parent.isVisible() or parent.window().isMinimized():self.canvas.hide();return
            geometry=QtCore.QRect(parent.mapToGlobal(QtCore.QPoint(0,0)),parent.size())
            changed=self.canvas.geometry()!=geometry or self.canvas.isHidden()
            if self.canvas.geometry()!=geometry:self.canvas.setGeometry(geometry)
            if self.canvas.isHidden():self.canvas.show();self.canvas.raise_()
            if changed or self.render_callback is None:self.canvas.update()
        except (AttributeError,RuntimeError):self.clear_canvas()

    def clear_canvas(self):
        self.unbind_render()
        if self.canvas is not None and isValid(self.canvas):self.canvas.hide();self.canvas.deleteLater()
        self.canvas=None

    def close(self):
        _RETAINED[:]=self.history[-64:]
        self.timer.stop();self.clear_canvas();self.sections=[];self.history=[];self.drag=None;self.deleteLater()
