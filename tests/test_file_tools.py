from factory.agents.tools import apply_edits, diff_since, diff_size, run_checks, snapshot_files


def test_write_and_replace(tmp_path):
    applied, errors = apply_edits(tmp_path, [{"op": "write", "path": "a/b.py", "content": "x = 1\n"}])
    assert applied == ["a/b.py"] and not errors
    _, errors = apply_edits(tmp_path, [{"op": "replace", "path": "a/b.py", "old": "x = 1", "new": "x = 2"}])
    assert not errors and (tmp_path / "a/b.py").read_text() == "x = 2\n"


def test_refuses_paths_outside_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _, errors = apply_edits(repo, [{"op": "write", "path": "../evil.py", "content": ""}])
    assert errors and not (tmp_path / "evil.py").exists()


def test_protected_paths(tmp_path):
    _, errors = apply_edits(tmp_path, [{"op": "write", "path": "tests/acceptance/test_ac1.py", "content": ""}],
                            protected=("tests/acceptance/",))
    assert errors and not (tmp_path / "tests/acceptance/test_ac1.py").exists()


def test_replace_requires_unique_match(tmp_path):
    (tmp_path / "f.py").write_text("a\na\n")
    _, errors = apply_edits(tmp_path, [{"op": "replace", "path": "f.py", "old": "a", "new": "b"}])
    assert errors


def test_diff_since_snapshot(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n")
    snap = snapshot_files(tmp_path)
    (tmp_path / "m.py").write_text("x = 2\n")
    (tmp_path / "n.py").write_text("y = 1\n")
    diff = diff_since(tmp_path, snap, ["m.py", "n.py"])
    assert "-x = 1" in diff and "+x = 2" in diff and "+y = 1" in diff
    assert diff_size(diff) == 3


def test_compile_check(tmp_path):
    (tmp_path / "ok.py").write_text("x = 1\n")
    assert run_checks(tmp_path, ["compile"])[0].passed
    (tmp_path / "bad.py").write_text("def f(:\n")
    result = run_checks(tmp_path, ["compile"])[0]
    assert not result.passed and result.fingerprints == ["compile|bad.py"]
