"""Apply explicitly published local Model Pack builds when Maya is idle."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
STATE = ROOT.parent / '.modelpack_updates'
REQUEST = STATE / 'request.json'
_SERVICE = None


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def validate(digest):
    manifest = ROOT / 'package_manifest.json'
    if hashlib.sha256(manifest.read_bytes()).hexdigest() != digest:
        raise RuntimeError('Published manifest does not match the request.')
    info = json.loads(manifest.read_text(encoding='utf-8'))
    for name, expected in info['files'].items():
        path = (ROOT / name).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():
            raise RuntimeError('Invalid release path: ' + name)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError('Incomplete release: ' + name)
    return info


def source_path():
    location = str(ROOT)
    if location in sys.path:sys.path.remove(location)
    sys.path.insert(0, location)
    # Already imported packages otherwise keep searching the previous ZIP folder.
    for name in ('model_pack_bevel', 'model_pack_draft', 'model_pack_selection',
                 'model_pack_linear_shift', 'slide_for_maya'):
        package = sys.modules.get(name)
        if package is not None:
            package.__path__ = [str(ROOT / name)]
            if package.__spec__ is not None:
                package.__spec__.submodule_search_locations = package.__path__
    importlib.invalidate_caches()


def windows():
    from PySide6 import QtWidgets
    return [w for w in QtWidgets.QApplication.topLevelWidgets()
            if w.objectName() == 'ModelPackToolboxUI']


def busy_reason():
    from maya import cmds
    from PySide6 import QtCore, QtWidgets
    if cmds.play(q=True, state=True):return 'playback'
    if QtWidgets.QApplication.mouseButtons() != QtCore.Qt.NoButton:return 'mouse gesture'
    if QtWidgets.QApplication.activeModalWidget() or QtWidgets.QApplication.activePopupWidget():return 'dialog'
    focus = QtWidgets.QApplication.focusWidget()
    if isinstance(focus, (QtWidgets.QLineEdit, QtWidgets.QAbstractSpinBox,
                          QtWidgets.QTextEdit, QtWidgets.QPlainTextEdit)):return 'text editing'
    for window in windows():
        kind = getattr(window, '_active_tool', None)
        if kind not in (None, 'fast_draft'):return str(kind)
    for name, attribute in (('model_pack_extrudex_runtime', '_SESSION'), ('recurve', 'SESSION'), ('model_pack_bevel.ui', '_SESSION'),
                            ('slide_for_maya.ui', '_SESSION'), ('model_pack_linear_shift.ui', '_DRAG')):
        module = sys.modules.get(name)
        if module is not None and getattr(module, attribute, None) is not None:return name
    module = sys.modules.get('model_pack_draft.tool')
    engine = getattr(module, '_active', None) if module is not None else None
    if engine is not None and engine.footprint is not None:return 'Fast Draft drawing'
    return None


def shelf_command():
    return ('import sys\n'
            'p = ' + repr(str(ROOT)) + '\n'
            'if p in sys.path: sys.path.remove(p)\n'
            'sys.path.insert(0,p)\n'
            'import model_pack_update_bridge\n'
            'model_pack_update_bridge.open_toolbox()')


def repair_shelves(save=False):
    """Update only existing MP buttons; never create a duplicate shelf item."""
    from maya import cmds, mel
    if cmds.about(batch=True):return []
    top = mel.eval('$tmpModelPackShelf = $gShelfTopLevel')
    changed = []
    for shelf in cmds.tabLayout(top, q=True, childArray=True) or []:
        for item in cmds.shelfLayout(shelf, q=True, childArray=True) or []:
            if cmds.objectTypeUI(item) != 'shelfButton':continue
            if cmds.shelfButton(item, q=True, annotation=True) != 'Model Pack by Black Cats Society':continue
            if cmds.shelfButton(item, q=True, command=True) != shelf_command():
                cmds.shelfButton(item, e=True, command=shelf_command(), sourceType='python',
                                 image=str(ROOT / 'icons' / 'Model_pack.png'))
                changed.append(shelf)
    if save:
        directory = Path(cmds.internalVar(userShelfDir=True))
        for shelf in set(changed):cmds.saveShelf(shelf, str(directory / ('shelf_' + shelf)))
    return changed


def open_toolbox():
    source_path()
    start()
    if busy_reason():return  # An open live tool retains its current geometry/session.
    module = importlib.import_module('launch_model_pack')
    importlib.reload(module).launch()


def install():
    """One-time native installation; requires permission to write Maya preferences."""
    from maya import cmds
    source_path()
    installer = importlib.import_module('install_model_pack')
    importlib.reload(installer).validate_package(ROOT)
    module = Path(cmds.internalVar(userAppDir=True)) / 'modules' / 'ModelPack.mod'
    backup = STATE / 'backups' / ('install-' + time.strftime('%Y%m%d-%H%M%S'))
    if module.is_file():
        backup.mkdir(parents=True, exist_ok=True)
        (backup / module.name).write_bytes(module.read_bytes())
    module.parent.mkdir(parents=True, exist_ok=True)
    temporary = module.with_suffix('.mod.tmp')
    temporary.write_text(installer.module_text(ROOT), encoding='utf-8');temporary.replace(module)
    # Back up the currently used shelf files before Maya saves changed MP commands.
    shelf_directory = Path(cmds.internalVar(userShelfDir=True))
    for path in shelf_directory.glob('shelf_*.mel'):
        if 'Model Pack by Black Cats Society' in path.read_text(encoding='utf-8', errors='replace'):
            backup.mkdir(parents=True, exist_ok=True);(backup / path.name).write_bytes(path.read_bytes())
    preferences = Path(cmds.internalVar(userPrefDir=True)) / 'pluginPrefs.mel'
    if preferences.is_file():
        backup.mkdir(parents=True, exist_ok=True);(backup / preferences.name).write_bytes(preferences.read_bytes())
    repaired = repair_shelves(save=True)
    plugin = ROOT / 'model_pack_update_plugin.py'
    if not cmds.pluginInfo(plugin.stem, q=True, loaded=True):cmds.loadPlugin(str(plugin), quiet=True)
    cmds.pluginInfo(plugin.stem, e=True, autoload=True)
    cmds.pluginInfo(savePluginPrefs=True)
    start()
    return {'module': str(module), 'shelves': repaired, 'root': str(ROOT), 'pid': os.getpid()}


class UpdateService:
    def __init__(self):
        from PySide6 import QtCore, QtWidgets
        self.last = None
        self.failed = None
        self.status = STATE / ('status-' + str(os.getpid()) + '.json')
        self.timer = QtCore.QTimer(QtWidgets.QApplication.instance())
        self.timer.setInterval(1500);self.timer.timeout.connect(self.tick);self.timer.start()
        self.signature = None
        self.report('ready', None)

    def report(self, state, request, detail=None):
        data = {'pid': os.getpid(), 'state': state, 'request': request, 'detail': detail,
                'root': str(ROOT), 'timestamp': time.time()}
        signature = (state, request, detail)
        if signature != self.signature:
            write_json(self.status, data);self.signature = signature

    def tick(self):
        try:
            if not REQUEST.is_file():return
            request = json.loads(REQUEST.read_text(encoding='utf-8'))
            revision = request['id']
            if revision in (self.last, self.failed):return
            reason = busy_reason()
            if reason:self.report('waiting', revision, reason);return
            info = validate(request['manifest_sha256'])
            visible = [w for w in windows() if w.isVisible()]
            position = visible[0].pos() if visible else None
            resume_draft = bool(visible and getattr(visible[0], '_active_tool', None) == 'fast_draft')
            source_path()
            if visible:
                module = importlib.import_module('launch_model_pack')
                importlib.reload(module).launch()
                current = [w for w in windows() if w.isVisible()]
                if current and position is not None:current[-1].move(position)
                if current and resume_draft:current[-1].tool_fast_draft()
            self.last = revision
            self.report('applied' if visible else 'available', revision, info['version'])
        except Exception as error:
            revision = locals().get('revision')
            self.failed = revision
            self.report('error', revision, str(error))


def start():
    global _SERVICE
    from maya import cmds
    from PySide6 import QtWidgets
    if cmds.about(batch=True) or QtWidgets.QApplication.instance() is None:return None
    if _SERVICE is None:_SERVICE = UpdateService()
    return _SERVICE


def startup():
    source_path()
    repair_shelves(save=False)
    return start()


def stop():
    global _SERVICE
    if _SERVICE is not None:
        _SERVICE.timer.stop();_SERVICE.timer.deleteLater();_SERVICE = None
