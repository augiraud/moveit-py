"""Fixtures for the wrapper script tests: a stub `uv` and a stub `zip` first on the PATH."""

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

if os.name == "nt":
    pytest.skip("the wrapper scripts are Bash scripts", allow_module_level=True)

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

UV_STUB = """#!/bin/sh
n=0
[ -f "$STUB_DIR/count" ] && n=$(cat "$STUB_DIR/count")
n=$((n + 1))
echo "$n" > "$STUB_DIR/count"
{
  printf '%s\\0' "${MOVEIT_URL-<unset>}"
  for a in "$@"; do printf '%s\\0' "$a"; done
  printf '\\001'
} >> "$STUB_DIR/uv.log"
codes=${STUB_UV_CODES:-0}
i=0
code=0
for c in $codes; do
  i=$((i + 1))
  code=$c
  [ "$i" -eq "$n" ] && break
done
echo "stub uv call $n"
exit "$code"
"""

ZIP_STUB = """#!/bin/sh
[ -n "$STUB_ZIP_FAIL" ] && exit 1
# args: -q -r -s SIZE TARGET NAME
target=$5
base=${target%.zip}
i=1
while [ "$i" -lt "${STUB_ZIP_PARTS:-3}" ]; do
  printf 'x' > "$(printf '%s.z%02d' "$base" "$i")"
  i=$((i + 1))
done
printf 'x' > "$target"
"""

TOOLS = ("dirname", "basename", "grep", "cat", "sort", "cut", "rm", "mkdir", "env", "head", "sh")


@dataclass
class Result:
    code: int
    out: str
    err: str
    calls: list[list[str]] = field(default_factory=list)  # per uv call: [MOVEIT_URL, args...]


class Runner:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.stub_dir = tmp_path / "stub"
        self.stub_dir.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.minimal = tmp_path / "minbin"
        self.minimal.mkdir()
        self.env_file = tmp_path / "moveit.env"
        for name, text in (("uv", UV_STUB), ("zip", ZIP_STUB)):
            stub = self.bin / name
            stub.write_text(text)
            stub.chmod(0o755)
        for tool in TOOLS:
            found = shutil.which(tool)
            assert found
            (self.minimal / tool).symlink_to(found)
        self.work = tmp_path / "work"
        self.work.mkdir()

    def run(
        self,
        script: str,
        *args: str,
        env: dict[str, str] | None = None,
        url: str | None = "https://moveit.test",
        path: str | None = None,
    ) -> Result:
        full = {
            "PATH": path if path is not None else f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.tmp),
            "MOVEIT_ENV_FILE": str(self.env_file),
            "STUB_DIR": str(self.stub_dir),
        }
        if url is not None:
            full["MOVEIT_URL"] = url
        full.update(env or {})
        proc = subprocess.run(  # noqa: S603
            [shutil.which("bash") or "bash", str(SCRIPTS / script), *args],
            capture_output=True,
            text=True,
            env=full,
            cwd=self.work,
            check=False,
        )
        return Result(proc.returncode, proc.stdout, proc.stderr, self.calls())

    def calls(self) -> list[list[str]]:
        log = self.stub_dir / "uv.log"
        if not log.exists():
            return []
        records = log.read_bytes().decode().split("\x01")[:-1]
        return [r.split("\0")[:-1] for r in records]


PROJECT_ARGS = ["run", "--quiet", "--project", str(SCRIPTS.parent), "moveit"]
