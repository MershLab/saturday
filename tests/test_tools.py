from __future__ import annotations

from pathlib import Path

from saturday.tools.files import EditFile, GlobTool, GrepTool, ListDir, ReadFile, WriteFile
from saturday.tools.python_repl import PythonREPL
from saturday.tools.shell import ShellTool


def test_write_read_edit(tmp_path: Path):
    root = str(tmp_path)
    w = WriteFile(root=root)
    ok, out = w.run({"path": "sub/a.txt", "content": "hello world"})
    assert ok
    r = ReadFile(root=root)
    ok, text = r.run({"path": "sub/a.txt"})
    assert ok and "1: hello world" in text

    e = EditFile(root=root)
    ok, out = e.run({"path": "sub/a.txt", "old_string": "world", "new_string": "forge"})
    assert ok
    ok, text = r.run({"path": "sub/a.txt"})
    assert "hello forge" in text


def test_edit_rejects_ambiguous_match(tmp_path: Path):
    root = str(tmp_path)
    WriteFile(root=root).run({"path": "b.txt", "content": "x x x"})
    ok, err = EditFile(root=root).run({"path": "b.txt", "old_string": "x", "new_string": "y"})
    assert not ok and "3 times" in err


def test_path_escape_blocked(tmp_path: Path):
    r = ReadFile(root=str(tmp_path))
    ok, err = r.run({"path": "../../etc/passwd"})
    assert not ok and "escapes" in err


def test_glob_and_grep(tmp_path: Path):
    root = str(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("VALUE = 42\n")
    (tmp_path / "notes.md").write_text("the VALUE here\n")
    g = GlobTool(root=root)
    ok, out = g.run({"pattern": "**/*.py"})
    assert ok and "src/m.py" in out
    gp = GrepTool(root=root)
    ok, out = gp.run({"pattern": r"VALUE\s*=\s*42", "include": "**/*"})
    assert ok and "src/m.py:1" in out


def test_listdir_and_shell(tmp_path: Path):
    ld = ListDir(root=str(tmp_path))
    ok, out = ld.run({})
    assert ok
    sh = ShellTool(root=str(tmp_path))
    ok, out = sh.run({"command": "echo saturday"})
    assert ok and "saturday" in out


def test_python_repl_persistence():
    repl = PythonREPL()
    try:
        ok, _ = repl.run({"code": "z = 6 * 7"})
        assert ok
        ok, out = repl.run({"code": "print(z)"})
        assert ok and "42" in out
        ok, err = repl.run({"code": "1/0"})
        assert not ok and "ZeroDivisionError" in err
    finally:
        repl.close()
