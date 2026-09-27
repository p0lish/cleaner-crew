import subprocess
from pathlib import Path

import pytest

from cleaner_crew.adapters.jira import adf, adf_to_text
from cleaner_crew.detect import detect_stack
from cleaner_crew.gitutil import commit_all, diff_stats, parse_remote
from cleaner_crew.models import Category, Finding, extract_fingerprint, fingerprint_marker


@pytest.mark.parametrize("url,host,path,kind", [
    ("git@github.com:acme/api.git", "github.com", "acme/api", "github"),
    ("https://github.com/acme/api", "github.com", "acme/api", "github"),
    ("https://gitlab.com/group/sub/proj.git", "gitlab.com", "group/sub/proj", "gitlab"),
    ("ssh://git@gitlab.acme.io:2222/team/proj.git", "gitlab.acme.io", "team/proj", "gitlab"),
    ("https://token@github.com/acme/api.git", "github.com", "acme/api", "github"),
])
def test_parse_remote(url, host, path, kind):
    r = parse_remote(url)
    assert (r.host, r.path, r.kind) == (host, path, kind)


def test_fingerprint_stable_and_extractable():
    a = Finding("Fix off-by-one in pager", Category.BUGFIX, "x", ["b.py", "a.py"])
    b = Finding("fix off by one in pager!", Category.BUGFIX, "y", ["a.py", "b.py"])
    assert a.fingerprint == b.fingerprint
    text = f"some description\n\n{fingerprint_marker(a.fingerprint)}"
    assert extract_fingerprint(text) == a.fingerprint
    assert extract_fingerprint("nothing here") is None


def test_adf_roundtrip():
    assert adf_to_text(adf("one\n\ntwo")) == "one\n\ntwo"


def test_detect_stack(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n")
    (tmp_path / "uv.lock").write_text("")
    s = detect_stack(tmp_path)
    assert (s.name, s.test, s.lint) == ("python", "uv run pytest -q", "uv run ruff check .")


def test_diff_stats(tmp_path: Path):
    def sh(*a):
        subprocess.run(a, cwd=tmp_path, check=True, capture_output=True)

    sh("git", "init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("1\n")
    sh("git", "add", ".")
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    sh("git", "update-ref", "refs/remotes/origin/main", "HEAD")
    (tmp_path / "a.txt").write_text("1\n2\n")
    (tmp_path / "b.txt").write_text("x\n")
    assert commit_all(tmp_path, "change")
    stats = diff_stats(tmp_path, "main")
    assert sorted(stats.paths) == ["a.txt", "b.txt"]
    assert stats.total_lines == 2
