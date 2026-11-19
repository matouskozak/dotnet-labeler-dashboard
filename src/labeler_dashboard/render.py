import hashlib
import json
import shutil
from pathlib import Path

from .metrics import report


def render(state, config, now, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    assets = Path(__file__).parent / "assets"
    html = (assets / "index.html").read_text(encoding="utf-8")
    for name in ("style.css", "app.js"):
        shutil.copyfile(assets / name, output / name)
        digest = hashlib.sha256((output / name).read_bytes()).hexdigest()[:16]
        html = html.replace(f'"{name}"', f'"{name}?v={digest}"')
    (output / "index.html").write_text(html, encoding="utf-8")
    payload = (
        json.dumps(report(state, config, now), ensure_ascii=True, separators=(",", ":")) + "\n"
    )
    # Keep the existing v2 URL working for already deployed clients.
    for name in ("data.json", "data-v2.json"):
        (output / name).write_text(payload, encoding="utf-8")
    (output / ".nojekyll").touch()
