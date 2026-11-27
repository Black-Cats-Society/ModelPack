"""Drag this file from Explorer into a Maya viewport."""
import os
import sys
import json
import traceback
import maya.cmds as cmds


def _install():
    folder = os.path.dirname(os.path.abspath(__file__))
    canonical = lambda path: os.path.normcase(os.path.realpath(path))
    # An older folder may precede this one even when this folder is already
    # present. reload() searches sys.path again, so always give this copy priority.
    sys.path[:] = [entry for entry in sys.path if canonical(entry or os.curdir)!=canonical(folder)]
    sys.path.insert(0,folder)
    plugin = os.path.join(folder,'recurve.py')
    # Maya identifies a loaded Python plug-in by its registered name. A query
    # using our full path can report false when another copy is already loaded.
    registered = 'recurve'
    loaded = cmds.pluginInfo(registered,query=True,loaded=True)
    if loaded and cmds.pluginInfo(registered,query=True,version=True) not in ('0.6.0','0.6.1','0.6.2','0.6.3','0.6.4','0.6.5','0.6.6','0.6.7','0.6.8','0.6.9','0.7.0','0.8.0','0.8.1','0.8.2','0.8.3','0.8.4','0.8.5'):
        raise RuntimeError('An older Recurve command is loaded. Restart Maya, then run this installer again. Reloading Python alone does not replace the registered Maya command.')
    if not loaded:
        cmds.loadPlugin(plugin)
    # Keep this revision manual until it has been validated in the user's viewport.
    cmds.pluginInfo(registered,edit=True,autoload=False)
    import recurve
    import importlib
    if hasattr(recurve,'shutdown'):
        recurve.shutdown()
    elif recurve.SESSION:
        recurve.SESSION.finish(False)
    importlib.invalidate_caches()
    importlib.reload(recurve)
    for helper in ('recurve_strip', 'recurve_bulk', 'recurve_uv', 'recurve_colors', 'recurve_normals', 'recurve_controls', 'recurve_faces', 'recurve_panel', 'recurve_saved_curves'):
        if helper in sys.modules:
            importlib.reload(sys.modules[helper])
        else:
            importlib.import_module(helper)
    for name in ('recurve','recurve_strip','recurve_bulk','recurve_uv','recurve_colors','recurve_normals','recurve_controls','recurve_faces','recurve_panel','recurve_saved_curves'):
        if canonical(sys.modules[name].__file__)!=canonical(os.path.join(folder,name+'.py')):
            raise RuntimeError('Recurve loaded an unexpected module: '+str(sys.modules[name].__file__))
    if recurve.install():
        recurve.status()
        state={'version':recurve.VERSION,'model_pack_build':recurve.MODEL_PACK_BUILD,'plugin_path':cmds.pluginInfo(registered,query=True,path=True),
               'batch':cmds.about(batch=True),'process_id':os.getpid(),
               'modules':{name:sys.modules[name].__file__ for name in
                          ('recurve','recurve_strip','recurve_bulk','recurve_uv','recurve_colors','recurve_normals','recurve_controls','recurve_faces','recurve_panel','recurve_saved_curves')}}
        target=os.path.join(folder,'Recurve_install_state.json')
        with open(target,'w',encoding='utf-8') as stream:
            json.dump(state,stream,ensure_ascii=False,indent=2)
        print('RECURVE INSTALL DETAILS: '+target)
        if not cmds.about(batch=True) and cmds.ls(selection=True):
            try:
                import runpy
                capture=runpy.run_path(os.path.join(folder,'capture_recurve_scene.py'),run_name='_recurve_capture')
                capture['capture']()
            except Exception as exc:
                print('Recurve selection capture: '+str(exc))


def onMayaDroppedPythonFile(*args):
    try:
        _install()
    except Exception as exc:
        target=os.path.join(os.path.dirname(os.path.abspath(__file__)),'Recurve_install_state.json')
        try:
            with open(target,'w',encoding='utf-8') as stream:
                json.dump({'error':str(exc),'traceback':traceback.format_exc()},stream,ensure_ascii=False,indent=2)
            print('RECURVE INSTALL DETAILS: '+target)
        except OSError:
            pass
        raise


if __name__ == '__main__':
    onMayaDroppedPythonFile()
