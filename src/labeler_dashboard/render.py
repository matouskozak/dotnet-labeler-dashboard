import json
import shutil
from pathlib import Path

from .metrics import report


def render(state, config, now, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    assets = Path(__file__).parent / "assets"
    for name in ("index.html", "style.css", "app.js"):
        shutil.copyfile(assets / name, output / name)
    (output / "data.json").write_text(
        json.dumps(report(state, config, now), ensure_ascii=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    (output / ".nojekyll").touch()
