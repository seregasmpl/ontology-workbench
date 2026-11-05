"""Collect installed third-party notices into the generated portable artifact."""
from importlib.metadata import distributions
from pathlib import Path
import json
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "dist" / "OntologyWorkbench" / "THIRD_PARTY_LICENSES"
OUTPUT.mkdir(parents=True, exist_ok=True)
inventory = []
for distribution in sorted(distributions(), key=lambda d: d.metadata.get("Name", "")):
    name = distribution.metadata.get("Name", "unknown")
    if name == "ontology-workbench":
        continue
    destination = OUTPUT / name
    destination.mkdir(exist_ok=True)
    files = []
    for entry in distribution.files or []:
        path = Path(distribution.locate_file(entry))
        if path.is_file() and ("license" in path.name.lower() or path.name.lower() in {"copying", "notice"}):
            target = destination / (str(entry).replace("\\", "_").replace("/", "_"))
            shutil.copy2(path, target)
            files.append(target.name)
    (destination / "METADATA.txt").write_text(distribution.read_text("METADATA") or "", encoding="utf-8")
    inventory.append({"name": name, "version": distribution.version, "license_files": files})

for name in ("react", "react-dom", "scheduler", "lucide-react"):
    package = ROOT / "frontend" / "node_modules" / name
    destination = OUTPUT / ("npm-" + name)
    destination.mkdir(exist_ok=True)
    metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
    files = []
    for path in package.iterdir():
        if path.is_file() and ("license" in path.name.lower() or path.name.lower() in {"copying", "notice"}):
            shutil.copy2(path, destination / path.name)
            files.append(path.name)
    if not files:
        raise SystemExit("Missing license for " + name)
    inventory.append({"name": name, "version": metadata["version"], "license_files": files})

python_license = Path(sys.base_prefix) / "LICENSE.txt"
if not python_license.is_file():
    raise SystemExit("Python LICENSE.txt not found; check the build runtime")
shutil.copy2(python_license, OUTPUT / "Python-LICENSE.txt")
(OUTPUT / "inventory.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
print("Collected license notices for", len(inventory), "packages and Python")
