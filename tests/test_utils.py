"""The DX9 notice's predicate: one file decides."""
from redfetch import utils


def test_dx9_missing_when_the_dll_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(utils, "DX9_DLL_PATH", str(tmp_path / "d3dx9_43.dll"))
    assert utils.dx9_missing()


def test_dx9_present_when_the_dll_exists(tmp_path, monkeypatch):
    dll = tmp_path / "d3dx9_43.dll"
    dll.write_bytes(b"")
    monkeypatch.setattr(utils, "DX9_DLL_PATH", str(dll))
    assert not utils.dx9_missing()


def test_ensure_cooked_console_tolerates_no_console():
    utils.ensure_cooked_console()  # piped stdin: must be a silent no-op
