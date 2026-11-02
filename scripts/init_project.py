"""Turns the template into your project. setup.sh runs it once, then it deletes itself."""

import keyword
import re
import sys
from pathlib import Path

TEMPLATE_NAME = "python-template"
TEMPLATE_PACKAGE = "python_template"
ROOT = Path(__file__).resolve().parent.parent


def text_files() -> list[Path]:
    files = []
    for folder, subfolders, names in ROOT.walk():
        # Skip hidden and special folders
        subfolders[:] = [s for s in subfolders if s == ".github" or not s.startswith((".", "__"))]
        files += [folder / name for name in names if not (folder / name).is_symlink()]
    return files


def set_property(text: str, key: str, value: str) -> str:
    lines = [f"{key}={value}" if line.startswith(f"{key}=") else line for line in text.splitlines()]
    return "\n".join(lines) + "\n"


def main() -> None:
    typed = input("Project name, such as my-project (leave empty to skip): ")
    name = re.sub(r"[^a-z0-9]+", "-", typed.lower()).strip("-")
    if not name:
        print("Skipped: run ./scripts/setup.sh again to name the project.")
        return
    package = name.replace("-", "_")
    if not package.isidentifier() or keyword.iskeyword(package) or package in sys.stdlib_module_names:
        raise SystemExit(
            f"Error: '{package}' can't be a Python package name. Pick a project name that starts with a letter "
            "and isn't a Python keyword or standard library module."
        )
    owner = input("GitHub user or organization: ").strip()
    if not owner:
        raise SystemExit("Error: the GitHub user or organization is required.")

    for path in text_files():
        if path == Path(__file__).resolve():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        new_text = text.replace(TEMPLATE_NAME, name).replace(TEMPLATE_PACKAGE, package)
        if path.name == "sonar-project.properties":
            new_text = set_property(new_text, "sonar.organization", owner.lower())
            new_text = set_property(new_text, "sonar.projectKey", f"{owner}_{name}")
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")

    (ROOT / "src" / TEMPLATE_PACKAGE).rename(ROOT / "src" / package)
    Path(__file__).unlink()


if __name__ == "__main__":
    main()
