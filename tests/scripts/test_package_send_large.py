"""package-send-large.sh, with stub `uv` and `zip`."""

import shlex
from pathlib import Path

import pytest
from script_runner import PROJECT_ARGS, Runner

SCRIPT = "package-send-large.sh"
SMALL = {"FILES_PER_PACKAGE": "3"}


@pytest.fixture
def data(runner: Runner) -> Runner:
    d = runner.work / "data"
    d.mkdir()
    (d / "a.txt").write_text("x")
    return runner


def parts_dir(r: Runner) -> Path:
    return r.work / "data.parts"


def sent_files(r: Runner) -> list[list[str]]:
    # per call: the files after the subject
    return [[Path(a).name for a in c[len(PROJECT_ARGS) + 1 :] if a.startswith("/")] for c in r.calls()]


def test_url_exported_and_in_resume(data: Runner) -> None:
    r = data.run(SCRIPT, "--url", "https://o.test", "S", "a@b.c", "data", url=None, env={"STUB_UV_CODES": "1"})
    assert r.code == 1
    assert r.calls[0][0] == "https://o.test"
    assert "--url https://o.test S a@b.c" in r.err


def test_validations(data: Runner) -> None:
    cases = [
        (["S", "nobody", "data"], "EMAILS must be comma-separated emails, e.g. alice@example.com,bob@example.com"),
        (["S", "a@b.c", "nothing"], "'nothing' does not exist"),
        (["S", "a@b.c", "data", "0"], "FIRST_PACKAGE must be a package number, e.g. 3"),
        (["S", "a@b.c", "data", "01"], "FIRST_PACKAGE must be a package number, e.g. 3"),
        (["S", "a@b.c", "data", "x"], "FIRST_PACKAGE must be a package number, e.g. 3"),
        (["S", "a@b.c", "data", "-1"], "FIRST_PACKAGE must be a package number, e.g. 3"),
    ]
    for args, message in cases:
        r = data.run(SCRIPT, *args)
        assert (r.code, r.err) == (2, f"Error: {message}\n"), args
        assert r.calls == []
    assert not parts_dir(data).exists()


def test_zip_missing(data: Runner) -> None:
    (data.minimal / "uv").symlink_to(data.bin / "uv")
    r = data.run(SCRIPT, "S", "a@b.c", "data", path=str(data.minimal))
    assert r.code == 1
    assert r.err == "Error: zip is not installed, e.g. sudo apt install zip\n"


def test_no_server(data: Runner) -> None:
    r = data.run(SCRIPT, "S", "a@b.c", "data", url=None)
    assert r.code == 2
    assert "no MOVEit server" in r.err
    assert not parts_dir(data).exists()


def test_zip_and_send(data: Runner) -> None:
    r = data.run(SCRIPT, "My subject", "a@b.c,d@e.f", "data", env={**SMALL, "STUB_ZIP_PARTS": "7"})
    assert r.code == 0, r.err
    pd = parts_dir(data)
    assert r.out.splitlines()[0] == f"Zipping data into 50 MB parts, in {pd}..."
    assert "== Package 1/3: 3 files" in r.out
    assert "== Package 3/3: 1 files" in r.out
    assert r.out.endswith("Sent 3 packages.\n")
    assert not pd.exists()
    assert len(r.calls) == 3
    note = (
        "Package 1/3 of data, split into 7 zip parts. Download the files of all 3 packages into one "
        "directory, then extract data.zip with 7-Zip (7z x data.zip)."
    )
    c = r.calls[0]
    assert c[1:] == [
        *PROJECT_ARGS,
        "send",
        "--to",
        "a@b.c,d@e.f",
        "--note",
        note,
        "--",
        "My subject (1/3)",
        *[str(pd / n) for n in ("data.z01", "data.z02", "data.z03")],
    ]
    assert r.calls[2][-2:] == ["My subject (3/3)", str(pd / "data.zip")]
    assert sent_files(data)[1] == ["data.z04", "data.z05", "data.z06"]


def test_part_size_tunable(data: Runner) -> None:
    r = data.run(SCRIPT, "S", "a@b.c", "data", env={"PART_SIZE_MB": "5"})
    assert "Zipping data into 5 MB parts" in r.out


@pytest.mark.parametrize("n", [13, 120])
def test_natural_order(data: Runner, n: int) -> None:
    r = data.run(SCRIPT, "S", "a@b.c", "data", env={"FILES_PER_PACKAGE": "1000", "STUB_ZIP_PARTS": str(n)})
    assert r.code == 0, r.err
    expected = [f"data.z{i:02d}" for i in range(1, n)] + ["data.zip"]
    assert sent_files(data) == [expected]
    assert expected.index("data.z10") == expected.index("data.z09") + 1


def test_default_files_per_package(data: Runner) -> None:
    r = data.run(SCRIPT, "S", "a@b.c", "data", env={"STUB_ZIP_PARTS": "23"})
    assert "== Package 1/3: 10 files" in r.out
    assert "== Package 3/3: 3 files" in r.out


def test_zip_failure(data: Runner) -> None:
    r = data.run(SCRIPT, "S", "a@b.c", "data", env={"STUB_ZIP_FAIL": "1"})
    assert r.code == 1
    assert r.err == "Error: could not zip data\n"
    assert not parts_dir(data).exists()
    assert r.calls == []


def test_file_path_and_trailing_slash(data: Runner) -> None:
    r = data.run(SCRIPT, "S", "a@b.c", "data/a.txt", env=SMALL)
    assert r.code == 0, r.err
    assert "Zipping a.txt into" in r.out
    assert r.out.endswith("Sent 1 packages.\n")
    r = data.run(SCRIPT, "S", "a@b.c", "data/", env=SMALL)
    assert "Zipping data into" in r.out


def test_reuse_parts(data: Runner) -> None:
    pd = parts_dir(data)
    pd.mkdir()
    for n in ("data.z01", "data.z02", "data.zip"):
        (pd / n).write_text("x")
    (pd / "unrelated.txt").write_text("x")
    (pd / "data.zdir").mkdir()
    r = data.run(SCRIPT, "S", "a@b.c", "data", env={**SMALL, "STUB_ZIP_FAIL": "1"})
    assert r.code == 0, r.err
    assert r.out.startswith(f"Using the parts already in {pd}\n")
    assert sent_files(data) == [["data.z01", "data.z02", "data.zip"]]
    assert not pd.exists()


def test_no_parts_to_resume_from(data: Runner) -> None:
    r = data.run(SCRIPT, "S", "a@b.c", "data", "2", env=SMALL)
    assert r.code == 2
    assert r.err == f"Error: no parts to resume from in {parts_dir(data)}\n"
    assert r.calls == []


def test_no_parts_found(data: Runner) -> None:
    parts_dir(data).mkdir()
    r = data.run(SCRIPT, "S", "a@b.c", "data")
    assert r.code == 2
    assert r.err == f"Error: no parts found in {parts_dir(data)}\n"


def test_only_n_packages(data: Runner) -> None:
    ok = data.run(SCRIPT, "S", "a@b.c", "data", env={**SMALL, "STUB_UV_CODES": "1"})
    assert ok.code == 1
    r = data.run(SCRIPT, "S", "a@b.c", "data", "4", env={**SMALL, "STUB_ZIP_PARTS": "7"})
    assert r.code == 2
    assert r.err == "Error: there are only 1 packages\n"
    assert parts_dir(data).exists()


def test_resume_from_package(data: Runner) -> None:
    data.run(SCRIPT, "S", "a@b.c", "data", env={**SMALL, "STUB_ZIP_PARTS": "7", "STUB_UV_CODES": "1"})
    (data.stub_dir / "uv.log").unlink()
    (data.stub_dir / "count").unlink()
    r = data.run(SCRIPT, "S", "a@b.c", "data", "2", env=SMALL)
    assert r.code == 0, r.err
    assert "Package 1/3" not in r.out
    assert r.out.splitlines()[1] == "== Package 2/3: 3 files"
    assert (
        r.calls[0][-4:]
        == [
            "S (2/3)",
            *[str(parts_dir(data) / n) for n in ("data.z04", "data.z05", "data.z06")],
        ][:1]
        + [str(parts_dir(data) / n) for n in ("data.z04", "data.z05", "data.z06")][:3]
    )
    assert r.out.endswith("Sent 3 packages.\n")


SUBJECT = 'It\'s "big" data'


def resume_tokens(r: Runner, p: int, with_url: str | None = None) -> list[str]:
    url = ["--url", with_url] if with_url else []
    return [SCRIPTS_PATH, *url, SUBJECT, "a@b.c,d@e.f", str(r.work / "data"), str(p)]


def check_err(err: str, r: Runner, head: list[str], p: int, with_url: str | None = None) -> None:
    lines = err.split("\n")
    assert lines[:-2] == ["", *head]
    command, end = lines[-2:]
    assert end == ""
    assert command.startswith("  ")
    # shell-quoted: reading it back gives the original arguments
    assert shlex.split(command) == resume_tokens(r, p, with_url)
    assert '"big"' not in command.replace('\\"', "")


SCRIPTS_PATH = str(Path(__file__).resolve().parents[2] / "scripts" / SCRIPT)


def test_failure_first_package(data: Runner) -> None:
    r = data.run(SCRIPT, SUBJECT, "a@b.c,d@e.f", "data", env={**SMALL, "STUB_ZIP_PARTS": "7", "STUB_UV_CODES": "1"})
    assert r.code == 1
    fail = "Package 1/3 failed, see above. To send it and the next ones, run:"
    check_err(r.err, data, [fail], 1)
    assert len(r.calls) == 1
    assert parts_dir(data).exists()


def test_failure_after_one_sent(data: Runner) -> None:
    r = data.run(SCRIPT, SUBJECT, "a@b.c,d@e.f", "data", env={**SMALL, "STUB_ZIP_PARTS": "7", "STUB_UV_CODES": "0 1"})
    assert r.code == 1
    fail = "Package 2/3 failed, see above. To send it and the next ones, run:"
    check_err(r.err, data, ["Package 1/3 was sent.", fail], 2)


def test_failure_after_several_sent_with_url(data: Runner) -> None:
    r = data.run(
        SCRIPT,
        "--url",
        "https://o.test",
        SUBJECT,
        "a@b.c,d@e.f",
        "data",
        env={**SMALL, "STUB_ZIP_PARTS": "10", "STUB_UV_CODES": "0 0 0 1"},
        url=None,
    )
    assert r.code == 1
    fail = "Package 4/4 failed, see above. To send it and the next ones, run:"
    check_err(r.err, data, ["Packages 1-3 of 4 were sent.", fail], 4, "https://o.test")


def test_resume_line_when_started_later(data: Runner) -> None:
    data.run(SCRIPT, "S", "a@b.c", "data", env={**SMALL, "STUB_ZIP_PARTS": "10", "STUB_UV_CODES": "1"})
    (data.stub_dir / "count").unlink()
    r = data.run(SCRIPT, "S", "a@b.c", "data", "2", env={**SMALL, "STUB_UV_CODES": "0 1"})
    assert r.code == 1
    assert "\nPackage 2/4 was sent.\nPackage 3/4 failed" in r.err


def test_exit_3_propagated(data: Runner) -> None:
    r = data.run(SCRIPT, "S", "a@b.c", "data", env={**SMALL, "STUB_ZIP_PARTS": "7", "STUB_UV_CODES": "0 3"})
    assert r.code == 3
    assert "Package 1/3 was sent." in r.err
    assert "Package 2/3 failed" in r.err


@pytest.mark.parametrize("code", [2, 130])
def test_other_failure_codes_become_1(data: Runner, code: int) -> None:
    r = data.run(SCRIPT, "S", "a@b.c", "data", env={**SMALL, "STUB_UV_CODES": str(code)})
    assert r.code == 1
