"""Maya autoload entry point for the fixed local Model Pack update bridge."""
from pathlib import Path
import sys
from maya import cmds, utils
from maya.api import OpenMaya as om

ROOT = str(Path(globals().get('__file__', sys._getframe().f_code.co_filename)).resolve().parent)


def maya_useNewAPI():pass


def initializePlugin(plugin):
    om.MFnPlugin(plugin, 'Model Pack', '1.0', 'Any')
    if ROOT in sys.path:sys.path.remove(ROOT)
    sys.path.insert(0, ROOT)
    if not cmds.about(batch=True):
        utils.executeDeferred('import model_pack_update_bridge; model_pack_update_bridge.startup()')


def uninitializePlugin(plugin):
    module = sys.modules.get('model_pack_update_bridge')
    if module is not None:module.stop()
