"""Drop this file into Maya's viewport to install or update Model Pack."""
from pathlib import Path
import hashlib
import json
import sys

VERSION='0.1.0-beta'
ANNOTATION='Model Pack by Black Cats Society'
REQUIRED=('launch_model_pack.py','tube_resection.py','model_pack_modifier_nodes.py',
          'model_pack_fast_mirror_node.py','model_pack_paths.py','slide_for_maya/core.py',
          'model_pack_bevel/unBevel1.54.py','model_pack_linear_shift/ui.py',
          'model_pack_selection/ui.py','icons/Model_pack.png')


def validate_package(folder):
    folder=Path(folder).resolve()
    missing=[name for name in REQUIRED if not (folder/name).is_file()]
    if missing:raise RuntimeError('Model Pack: распакуйте весь архив. Не найдены: '+', '.join(missing))
    manifest=folder/'package_manifest.json'
    if manifest.is_file() and not (folder/'.git').exists():
        info=json.loads(manifest.read_text(encoding='utf-8'))
        for name,digest in info['files'].items():
            path=(folder/name).resolve()
            if not path.is_relative_to(folder):raise RuntimeError('Model Pack: неверный путь в манифесте.')
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
                raise RuntimeError('Model Pack: файл повреждён или изменён: '+name+'. Распакуйте архив заново.')
    return folder


def shelf_command(folder):
    folder=Path(folder).resolve()
    return ('import sys, runpy\n'
            f'p = {str(folder)!r}\n'
            'if p in sys.path: sys.path.remove(p)\n'
            'sys.path.insert(0, p)\n'
            f'runpy.run_path({str(folder/"launch_model_pack.py")!r}, run_name="__main__")')


def module_text(folder):
    location=Path(folder).resolve().as_posix()
    # Maya can load a Python plug-in by a short absolute path, but its scene
    # loader cannot reliably find it by name through that same directory.
    if not location.isascii():
        raise RuntimeError('Model Pack: для открытия сцен с узлами истории нужна папка с латинским путём. Распакуйте пакет в C:/MayaTools/ModelPack и повторите установку.')
    # The rest of the first line is the path; do not quote paths with spaces.
    return (f'+ ModelPack {VERSION.split("-")[0]} {location}\n'
            'scripts: .\n'
            'plug-ins: .\n'
            'PYTHONPATH +:= .\n'
            'MAYA_PLUG_IN_PATH +:= model_pack_bevel\n')


def install(folder=None,open_panel=True):
    import maya.cmds as cmds
    import maya.mel as mel
    if cmds.about(batch=True):raise RuntimeError('Model Pack: установщик нужно запустить в окне Maya.')
    try:
        import PySide6,shiboken6,numpy  # Maya supplies these dependencies.
    except ImportError as error:
        raise RuntimeError('Model Pack требует Maya с PySide6, shiboken6 и NumPy. Проверенная версия: Maya 2027.') from error
    folder=validate_package(folder or Path(__file__).resolve().parent)
    shelf_top=mel.eval('$tmpModelPackShelf = $gShelfTopLevel')
    shelf=cmds.tabLayout(shelf_top,q=True,selectTab=True)
    if not shelf:raise RuntimeError('Model Pack: выберите вкладку Shelf перед установкой.')
    children=cmds.shelfLayout(shelf,q=True,childArray=True) or []
    button=next((item for item in children if cmds.objectTypeUI(item)=='shelfButton'
                 and cmds.shelfButton(item,q=True,annotation=True)==ANNOTATION),None)
    module=Path(cmds.internalVar(userAppDir=True))/'modules'/'ModelPack.mod'
    text=module_text(folder)
    module.parent.mkdir(parents=True,exist_ok=True)
    if not module.is_file() or module.read_text(encoding='utf-8')!=text:
        temporary=module.with_suffix('.mod.tmp');temporary.write_text(text,encoding='utf-8');temporary.replace(module)
    options=dict(label='Model Pack',imageOverlayLabel='MP',annotation=ANNOTATION,
                 image=str(folder/'icons'/'Model_pack.png'),sourceType='python',command=shelf_command(folder))
    if button:cmds.shelfButton(button,e=True,**options)
    else:button=cmds.shelfButton(parent=shelf,**options)
    if open_panel:exec(options['command'],{'__name__':'__main__'})
    cmds.inViewMessage(amg='Model Pack установлен. Сохраните полку: Shelf → Save All Shelves.',pos='topCenter',fade=True)
    return button


def onMayaDroppedPythonFile(*args):
    return install()


if __name__=='__main__':onMayaDroppedPythonFile()
