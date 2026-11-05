"""Check the staged publication set without printing possible secret values.

This bounded check catches common mistakes, not every possible secret.
"""
from io import BytesIO
from pathlib import Path
import re
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
files = subprocess.check_output(["git", "diff", "--cached", "--name-only", "-z"], cwd=ROOT).decode("utf-8").split("\0")
patterns = [
    re.compile(rb"(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|AIza[A-Za-z0-9_-]{25,})"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"https?://[^\s/]+:[^\s/@]+@", re.I),
    re.compile(rb"C:" + rb"(?:\\\\|/)Users(?:\\\\|/)", re.I),
]
for name in filter(None, files):
    path = Path(name)
    blocked = {"data", "qa", "cases", ".venv", "node_modules", "__pycache__", "build", "dist"}
    if any(p in blocked or p.startswith("qa-") or p.endswith(".egg-info") for p in path.parts):
        raise SystemExit("Private or generated path staged: " + name)
    if path.name.startswith((".env", ".mcp", "~$")) or path.suffix in {".sqlite", ".sqlite3", ".db", ".log", ".pem", ".key", ".p12", ".pfx"}:
        raise SystemExit("Forbidden file staged: " + name)
    content = subprocess.check_output(["git", "show", ":" + name], cwd=ROOT)
    parts = [(name, content)]
    if path.suffix == ".docx":
        with zipfile.ZipFile(BytesIO(content)) as document:
            parts = [(name + ":" + entry.filename, document.read(entry)) for entry in document.infolist() if entry.filename.endswith((".xml", ".rels"))]
    for label, data in parts:
        if any(pattern.search(data) for pattern in patterns):
            raise SystemExit("Review potential secret or private path in " + label)
print("Checked", len(list(filter(None, files))), "staged paths; no blocked files or common secret patterns found")
