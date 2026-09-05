# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 — 生成单个 LLM-API-Gateway.exe"""

import importlib.util, os, sys

# ---- 自动生成 Windows 版本资源（从 backend/__init__.py 读取 __version__）----
_spec = importlib.util.spec_from_file_location("backend", os.path.join(os.getcwd(), "backend", "__init__.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_version = _mod.__version__
_parts = _version.split(".")
_major, _minor = int(_parts[0]), int(_parts[1])
_patch = int(_parts[2]) if len(_parts) > 2 else 0

_version_file = os.path.join(os.getcwd(), "file_version_info.txt")
with open(_version_file, "w", encoding="utf-8") as f:
    f.write(f"""\
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({_major}, {_minor}, {_patch}, 0),
    prodvers=({_major}, {_minor}, {_patch}, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          '080404B0',
          [
            StringStruct('CompanyName', 'LLM API Gateway'),
            StringStruct('FileDescription', 'LLM API Gateway'),
            StringStruct('FileVersion', '{_version}'),
            StringStruct('InternalName', 'LLM-API-Gateway'),
            StringStruct('OriginalFilename', 'LLM-API-Gateway.exe'),
            StringStruct('ProductName', 'LLM API Gateway'),
            StringStruct('ProductVersion', '{_version}'),
          ]
        )
      ]
    ),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
""")
print(f"[spec] Generated file_version_info.txt with version {_version}")

# ---- PyInstaller 配置 ----
a = Analysis(
    ['entry.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('frontend/dist', 'frontend'),
        ('.env', '.'),
        ('driver', 'driver'),
    ],
    hiddenimports=[
        'backend',
        'backend.main',
        'backend.config',
        'backend.db',
        'backend.server',
        'backend.proxy_core',
        'backend.tray',
        'backend.admin_api',
        'backend.updater',
        'pyodbc',
        'pystray._win32',
        'PIL._tkinter_finder',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noconfirm=True,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    name='LLM-API-Gateway',
    console=False,
    icon=None,
    onefile=True,
    version='file_version_info.txt',
)
