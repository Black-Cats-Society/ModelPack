"""Mirror Cut's live edit session and its native Maya Undo group."""
import uuid
from maya import cmds
from maya.api import OpenMaya as om
from PySide6 import QtCore,QtGui,QtWidgets

COMMAND='modelPackMirrorEdit'
APPLY_COMMAND='modelPackApplyMirror'
PENDING=globals().get('PENDING',{})
ACTIVE=globals().get('ACTIVE',{})
CHUNKS=globals().get('CHUNKS',set())
UNDO_FILTER=globals().get('UNDO_FILTER',None)
SCENE_CALLBACKS=globals().get('SCENE_CALLBACKS',[])


class UndoFilter(QtCore.QObject):
    def eventFilter(self,obj,event):
        if event.type() in (QtCore.QEvent.KeyPress,QtCore.QEvent.ShortcutOverride):
            is_undo=event.matches(QtGui.QKeySequence.Undo)
            is_redo=event.matches(QtGui.QKeySequence.Redo)
            if not (is_undo or is_redo):return False
            if ACTIVE:
                if event.type()==QtCore.QEvent.ShortcutOverride:
                    event.accept();return True
                undo() if is_undo else cmds.redo()
                return True
        return False


def undo():
    """Undo one native action while the session's outer group remains open."""
    cmds.undo()


def install_undo_filter():
    global UNDO_FILTER
    app=QtWidgets.QApplication.instance()
    if app is not None and UNDO_FILTER is None:
        UNDO_FILTER=UndoFilter(app);app.installEventFilter(UNDO_FILTER)
    if not SCENE_CALLBACKS:
        for kind in (om.MSceneMessage.kBeforeNew,om.MSceneMessage.kBeforeOpen,om.MSceneMessage.kMayaExiting):
            SCENE_CALLBACKS.append(om.MSceneMessage.addCallback(kind,lambda *_:finish_all()))


def remove_undo_filter():
    global UNDO_FILTER
    if UNDO_FILTER is not None and not ACTIVE:
        app=QtWidgets.QApplication.instance()
        if app is not None:app.removeEventFilter(UNDO_FILTER)
        UNDO_FILTER.deleteLater();UNDO_FILTER=None
    if not ACTIVE:
        for callback in SCENE_CALLBACKS:om.MMessage.removeCallback(callback)
        SCENE_CALLBACKS.clear()


class MirrorEditCommand(om.MPxCommand):
    @staticmethod
    def creator():return MirrorEditCommand()
    def isUndoable(self):return bool(self.operations)
    def doIt(self,args):
        self.key=args.asString(0);self.operations=[];self.nodes=[];self.active=True
        self.record(PENDING[self.key])
        ACTIVE[self.key]=self
        self.setResult(self.nodes)
    def record(self,action):
        modifier=om.MDGModifier()
        modifier.pythonCommandToExecute(action)
        try:modifier.doIt()
        except Exception:
            modifier.undoIt();raise
        self.operations.append(modifier)
    def undoIt(self):
        if self.key not in CHUNKS:
            self.active=False;ACTIVE.pop(self.key,None)
        for operation in reversed(self.operations):operation.undoIt()
    def redoIt(self):
        for operation in self.operations:operation.doIt()
        if self.key in CHUNKS:self.active=True;ACTIVE[self.key]=self


class MirrorApplyCommand(om.MPxCommand):
    @staticmethod
    def creator():return MirrorApplyCommand()
    def isUndoable(self):return True
    def doIt(self,args):
        from model_pack_modifiers import related,_mesh_fn,apply_mirrors
        from slide_for_maya.edit_session import materials,assign_materials
        nodes=PENDING[args.asString(0)];self.snapshots=[];shading=[]
        for node in nodes:
            if not cmds.objExists(node):continue
            target=related(node)
            if target is None:continue
            fn=_mesh_fn(target);data=om.MFnMeshData().create()
            output=om.MFnDependencyNode(fn.object()).findPlug('outMesh',False).asMObject()
            om.MFnMesh().copy(output,data)
            self.snapshots.append((fn.fullPathName(),data))
            values=materials(target)
            if values[0]:shading.append((target,values))
        self.native=om.MDGModifier()
        self.native.pythonCommandToExecute(lambda:apply_mirrors(nodes))
        self.shading=om.MDGModifier() if shading else None
        def restore_shading():
            for target,values in shading:assign_materials(target,*values)
        if self.shading is not None:self.shading.pythonCommandToExecute(restore_shading)
        try:self.redoIt()
        except Exception:
            if self.shading is not None:self.shading.undoIt()
            self.native.undoIt();raise
    def redoIt(self):
        from model_pack_modifiers import _mesh_fn
        self.native.doIt()
        # Maya's history deletion may replay its old cached input on Redo.
        # Restore the evaluated result captured before deleting that history.
        for shape,data in self.snapshots:
            # Keep the template private: mesh shapes can mutate their input
            # data while native history deletion is undone.
            restored=om.MFnMeshData().create();om.MFnMesh().copy(data,restored)
            _mesh_fn(shape).copyInPlace(restored)
        if self.shading is not None:self.shading.doIt()
    def undoIt(self):
        if self.shading is not None:self.shading.undoIt()
        self.native.undoIt()


def apply(nodes):
    from model_pack_modifiers import ensure_plugin,PLUGIN,related
    nodes=[node for node in nodes if cmds.objExists(node) and related(node) is not None]
    if not nodes:return
    ensure_plugin(PLUGIN)
    key=uuid.uuid4().hex;PENDING[key]=list(nodes)
    try:getattr(cmds,APPLY_COMMAND)(key)
    finally:PENDING.pop(key,None)


def start(action):
    finish_all()
    from model_pack_modifiers import ensure_plugin,PLUGIN
    ensure_plugin(PLUGIN)
    key=uuid.uuid4().hex
    def initial():
        nodes=action()
        # Existing histories also need one undoable anchor for later edits.
        for node in nodes:cmds.setAttr(node+'.axis',cmds.getAttr(node+'.axis'))
        PENDING[key+'_nodes']=nodes
    PENDING[key]=initial
    # Include native vertex/pivot edits made in the viewport in this session.
    # Keep native commands in their execution order for accurate Redo.
    cmds.undoInfo(openChunk=True,chunkName='Mirror Cut');CHUNKS.add(key)
    try:
        getattr(cmds,COMMAND)(key)
        owner=ACTIVE[key];owner.nodes=PENDING[key+'_nodes']
        install_undo_filter()
        return key,owner.nodes
    except Exception:
        finish(key,freeze=False)
        raise
    finally:
        PENDING.pop(key,None);PENDING.pop(key+'_nodes',None)


def update(key,action):
    owner=ACTIVE.get(key)
    if owner is None or not owner.active:return False
    action();return True


def close_chunk(key):
    if key in CHUNKS:
        CHUNKS.remove(key);cmds.undoInfo(closeChunk=True)


def finish(key,freeze=True):
    owner=ACTIVE.pop(key,None)
    try:
        if owner is not None and owner.active:
            if freeze:
                apply(owner.nodes)
            owner.active=False
    finally:
        close_chunk(key);remove_undo_filter()


def finish_all():
    for key in list(ACTIVE):finish(key)
    for key in list(CHUNKS):close_chunk(key)
