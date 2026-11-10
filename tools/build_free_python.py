import argparse
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile


def main():
    parser = argparse.ArgumentParser(description="Install a standalone Python runtime in a Free candidate")
    archive_arg = parser.add_mutually_exclusive_group(required=True)
    archive_arg.add_argument("--embed-zip", type=Path)
    archive_arg.add_argument("--nuget", type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--free-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.free_root.resolve()
    if not (root / "FREE_BUILD").is_file():
        parser.error("--free-root must point to a generated Free tree")
    runtime = root / "python"
    if runtime.exists():
        parser.error("python already exists; use a fresh Free tree")
    expected = f"python{sys.version_info.major}{sys.version_info.minor}._pth"
    with ZipFile(args.embed_zip or args.nuget) as archive:
        if args.nuget:
            if "tools/python.exe" not in archive.namelist():
                parser.error("Use the official Windows Python NuGet package")
            for member in archive.infolist():
                if member.is_dir() or not member.filename.startswith("tools/"):
                    continue
                dest = (runtime / member.filename.removeprefix("tools/")).resolve()
                if not dest.is_relative_to(runtime):
                    parser.error("Unexpected archive path")
                if any(part in {"site-packages", "Scripts", "ensurepip"} for part in dest.relative_to(runtime).parts):
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, dest.open("wb") as output:
                    shutil.copyfileobj(source, output)
        elif expected not in archive.namelist() or "python.exe" not in archive.namelist():
            parser.error("Use the official Windows embeddable ZIP matching the builder's Python version")
        else:
            archive.extractall(runtime)
    embedded = runtime / "python.exe"
    embedded_version = subprocess.check_output(
        [str(embedded), "-c", "import struct,sys; print(sys.version_info[:2], struct.calcsize('P'))"],
        text=True,
    ).strip()
    if embedded_version != f"{sys.version_info[:2]} {struct.calcsize('P')}":
        parser.error("Builder and embedded Python version/architecture do not match")
    packages = runtime / "Lib/site-packages"
    requirements = Path(__file__).with_name("requirements-free.txt")
    pip = [sys.executable, "-m", "pip", "install", "--only-binary=:all:",
           "--target", str(packages), "-r", str(requirements)]
    if args.wheelhouse:
        pip.extend(["--no-index", "--find-links", str(args.wheelhouse.resolve())])
    subprocess.run(pip, check=True)
    pth = runtime / expected
    pth.write_text(
        (f"{expected.replace('._pth', '.zip')}\n" if args.embed_zip else "Lib\nDLLs\n")
        + ".\n..\n../battle_stream\n../rkpp_clean\nLib/site-packages\n"
        "Lib/site-packages/win32\nLib/site-packages/win32/lib\nimport site\n",
        encoding="utf-8",
    )
    subprocess.run([str(embedded), "-c",
                    "import fastapi, uvicorn, win32gui, scapy, Crypto, msgpack, google.protobuf; "
                    "assert 5 <= int(google.protobuf.__version__.split('.')[0]) < 7; "
                    "from src.server.app import app; "
                    "import importlib.util; "
                    "assert all(importlib.util.find_spec(name) is None for name in "
                    "('cv2', 'numpy', 'onnxruntime', 'rapidocr_onnxruntime'))"],
                   cwd=root, check=True)


if __name__ == "__main__":
    main()
