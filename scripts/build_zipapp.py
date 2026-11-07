"""Build the portable Python zip application, including offline GUI/model assets.

Run: python scripts/build_zipapp.py
Use: python dist/eljeffe.pyz --help
"""

from pathlib import Path
import zipfile


def build():
    root = Path(__file__).resolve().parents[1]
    target = root / "dist" / "eljeffe.pyz"
    target.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        def add(name, data):
            info = zipfile.ZipInfo(name, date_time=(2025, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
        add("__main__.py", b"from eljeffe.cli import main\nraise SystemExit(main())\n")
        for path in sorted((root / "eljeffe").rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                add(path.relative_to(root).as_posix(), path.read_bytes())
        for filename in ("LICENSE.md", "docs/FTM_MODEL.md"):
            add(filename, (root / filename).read_bytes())
    print(target)
    return target


if __name__ == "__main__":
    build()
