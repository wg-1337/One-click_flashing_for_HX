"""打包成单文件 exe：python scripts/build_exe.py"""

from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEP = os.pathsep  # Windows 下是 ';'

ADD_DATA = [
    (os.path.join(ROOT, "web"), "web"),
    (os.path.join(ROOT, "assets"), "assets"),
    (os.path.join(ROOT, "tools"), "tools"),
]

def main() -> int:
    icon = os.path.join(ROOT, "assets", "icon", "app.ico")
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", "一键刷机",
        "--icon", icon,
        "--paths", os.path.join(ROOT, "app"),
        "--distpath", os.path.join(ROOT, "dist"),
        "--workpath", os.path.join(ROOT, "build"),
        "--specpath", os.path.join(ROOT, "build"),
        "--hidden-import", "serial.tools.list_ports",
        "--exclude-module", "tkinter",
        "--exclude-module", "PIL",
        "--exclude-module", "numpy",
        "--exclude-module", "pandas",
    ]
    for src, dst in ADD_DATA:
        args += ["--add-data", f"{src}{SEP}{dst}"]
    args.append(os.path.join(ROOT, "app", "server.py"))

    print(" ".join(args))
    return subprocess.call(args, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
