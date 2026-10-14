"""Paths compatible with Maya's native Windows module and Python plug-in loaders."""
import os
from pathlib import Path


def maya_directory(folder):
    location=Path(folder).resolve().as_posix()
    if os.name=='nt' and not location.isascii():
        import ctypes
        function=ctypes.windll.kernel32.GetShortPathNameW
        function.argtypes=(ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_uint32)
        function.restype=ctypes.c_uint32
        buffer=ctypes.create_unicode_buffer(32768)
        if not function(str(Path(folder).resolve()),buffer,len(buffer)) or not buffer.value.isascii():
            raise RuntimeError('Model Pack: для загрузчика Maya нужна папка с латинским путём. Распакуйте пакет, например, в C:/MayaTools/ModelPack и повторите установку.')
        location=buffer.value.replace('\\','/')
    return location


def plugin_path(path):
    path=Path(path)
    # Keep the original ASCII filename, so saved scenes use stable plug-in names.
    return str(Path(maya_directory(path.parent))/path.name)
