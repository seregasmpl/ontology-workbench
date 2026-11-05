"""Build archives from explicit allowlists. Never include research archives or databases."""
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "deliverables"
OUTPUT.mkdir(exist_ok=True)
portable = ROOT / "dist" / "OntologyWorkbench"
if not (portable / "OntologyWorkbench.exe").is_file():
    raise SystemExit("Build PyInstaller output before packaging")
if not (portable / "THIRD_PARTY_LICENSES" / "inventory.json").is_file():
    raise SystemExit("Run tools/collect_licenses.py before packaging")
for name in ("README.md", "SECURITY.md", "THIRD_PARTY.md", "QA.md"):
    shutil.copy2(ROOT / name, portable / name)
shutil.copytree(ROOT / "examples", portable / "examples", dirs_exist_ok=True)

def archive(paths, output, base):
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in paths:
            if path.is_file():
                if path.suffix in {".sqlite", ".sqlite3", ".log"} or path.name.startswith(".mcp"):
                    raise ValueError("Forbidden file in release: " + str(path))
                bundle.write(path, path.relative_to(base).as_posix())

archive(sorted(portable.rglob("*")), OUTPUT / "OntologyWorkbench-Windows-x64.zip", portable.parent)
allowed = [ROOT / n for n in ("pyproject.toml", "README.md", "SECURITY.md", "THIRD_PARTY.md", "QA.md", "requirements-lock.txt", "OntologyWorkbench.spec", ".gitignore", ".gitattributes")]
for folder in ("backend", "tests", "tools", "examples"):
    allowed.extend(p for p in (ROOT / folder).rglob("*") if p.is_file() and "__pycache__" not in p.parts and not any(part.endswith(".egg-info") for part in p.parts))
for name in ("package.json", "package-lock.json", "index.html", "tsconfig.json", "vite.config.ts"):
    allowed.append(ROOT / "frontend" / name)
allowed.extend((ROOT / "frontend" / "src").rglob("*"))
archive(sorted(allowed), OUTPUT / "OntologyWorkbench-source.zip", ROOT)
manifest = {}
for path in sorted(OUTPUT.iterdir()):
    if path.suffix in {".zip", ".docx"}:
        manifest[path.name] = {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
(OUTPUT / "checksums.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
print(json.dumps(manifest, indent=2, ensure_ascii=True))
