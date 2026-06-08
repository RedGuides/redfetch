"""Tests for the manifest-driven LuaRocks bootstrap module."""

from __future__ import annotations

import asyncio
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from redfetch import luarocks
from redfetch.luarocks import Rock


def _run(coro):
    return asyncio.run(coro)


# ---------- _parse_manifest ----------


def test_parse_manifest_reads_files():
    data = {
        "luafilesystem": {"files": ["lfs.dll"]},
        "luasocket": {"files": ["mime\\core.dll", "socket\\core.dll"]},
        "fun": {},
        "lsqlite3": {"files": ["lsqlite3.dll"], "external_deps": ["sqlite3"]},
    }
    rocks = {r.name: r for r in luarocks._parse_manifest(data)}

    assert rocks["luafilesystem"].files == ["lfs.dll"]
    assert rocks["luasocket"].files == ["mime\\core.dll", "socket\\core.dll"]
    assert rocks["fun"].files == []
    assert rocks["lsqlite3"].files == ["lsqlite3.dll"]


def test_parse_manifest_handles_null_entry():
    # A bare "argparse:" with no mapping parses to None in YAML.
    rocks = {r.name: r for r in luarocks._parse_manifest({"argparse": None})}
    assert rocks["argparse"].files == []


def test_parse_manifest_non_dict_returns_empty():
    assert luarocks._parse_manifest(None) == []
    assert luarocks._parse_manifest("nonsense") == []


def test_fetch_manifest_failure_warns_and_returns_none(monkeypatch, capsys):
    class _FailingClient:
        def __init__(self, *_a, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

        async def get(self, _url, **_kw):
            raise luarocks.httpx.ConnectError("down")

    monkeypatch.setattr(luarocks.httpx, "AsyncClient", _FailingClient)

    assert _run(luarocks.fetch_manifest()) is None
    out = capsys.readouterr().out
    assert "could not fetch" in out
    assert "skipping module bootstrap" in out


# ---------- _find_luarocks_tree ----------


TEST_JIT_VERSION = "2.1.1734626439"


def _mq2lua_bytes(version: str, machine: int) -> bytes:
    """Build a minimal valid PE that also contains the LuaJIT version string."""
    pe_offset = 0x80
    buf = bytearray(pe_offset + 8)
    buf[0:2] = b"MZ"
    buf[0x3C:0x40] = pe_offset.to_bytes(4, "little")
    buf[pe_offset:pe_offset + 4] = b"PE\x00\x00"
    buf[pe_offset + 4:pe_offset + 6] = machine.to_bytes(2, "little")
    buf += b"\x00LuaJIT " + version.encode("ascii") + b"\x00more noise"
    return bytes(buf)


def _write_mq2lua(
    root: str,
    version: str = TEST_JIT_VERSION,
    machine: int = luarocks.IMAGE_FILE_MACHINE_AMD64,
) -> str:
    path = os.path.join(root, "plugins", "MQ2Lua.dll")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(_mq2lua_bytes(version, machine))
    return path


def _make_tree(root: str, version: str) -> str:
    tree = os.path.join(root, "modules", version, "luarocks")
    os.makedirs(tree)
    return tree


def test_read_jit_version_from_mq2lua(tmp_path):
    _write_mq2lua(str(tmp_path), "2.1.1800000000")

    assert luarocks._read_jit_version_from_mq2lua(str(tmp_path)) == "2.1.1800000000"


def test_detect_mq_is_64bit_reads_pe_machine(tmp_path):
    root64 = tmp_path / "x64"
    root32 = tmp_path / "x86"
    _write_mq2lua(str(root64), machine=luarocks.IMAGE_FILE_MACHINE_AMD64)
    _write_mq2lua(str(root32), machine=luarocks.IMAGE_FILE_MACHINE_I386)

    assert luarocks._detect_mq_is_64bit(str(root64)) is True
    assert luarocks._detect_mq_is_64bit(str(root32)) is False


def test_detect_mq_is_64bit_unknown_returns_none(tmp_path):
    # Missing DLL, and a non-PE file, both yield None (undetectable).
    assert luarocks._detect_mq_is_64bit(str(tmp_path)) is None
    _write(os.path.join(str(tmp_path), "plugins", "MQ2Lua.dll"))
    assert luarocks._detect_mq_is_64bit(str(tmp_path)) is None


def test_vc_redist_url_matches_arch_and_defaults_to_x64(tmp_path):
    root64 = tmp_path / "x64"
    root32 = tmp_path / "x86"
    _write_mq2lua(str(root64), machine=luarocks.IMAGE_FILE_MACHINE_AMD64)
    _write_mq2lua(str(root32), machine=luarocks.IMAGE_FILE_MACHINE_I386)

    assert luarocks._vc_redist_url(str(root64)) == luarocks.VC_REDIST_X64
    assert luarocks._vc_redist_url(str(root32)) == luarocks.VC_REDIST_X86
    # Undetectable -> safe default of x64.
    assert luarocks._vc_redist_url(str(tmp_path / "missing")) == luarocks.VC_REDIST_X64


def test_find_luarocks_tree_uses_mq2lua_version_without_existing_tree(tmp_path):
    _write_mq2lua(str(tmp_path), TEST_JIT_VERSION)
    expected = os.path.join(str(tmp_path), "modules", TEST_JIT_VERSION, "luarocks")

    result = luarocks._find_luarocks_tree(str(tmp_path))
    assert result is not None
    version, tree = result
    assert version == TEST_JIT_VERSION
    assert os.path.normpath(tree) == os.path.normpath(expected)


def test_find_luarocks_tree_ignores_existing_trees_without_mq2lua(tmp_path):
    _make_tree(str(tmp_path), TEST_JIT_VERSION)

    assert luarocks._find_luarocks_tree(str(tmp_path)) is None


def test_find_luarocks_tree_none_returns_none(tmp_path):
    assert luarocks._find_luarocks_tree(str(tmp_path)) is None
    os.makedirs(os.path.join(str(tmp_path), "plugins"))
    open(os.path.join(str(tmp_path), "plugins", "MQ2Lua.dll"), "wb").close()
    assert luarocks._find_luarocks_tree(str(tmp_path)) is None


# ---------- file / satisfied checks ----------


def _write(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").close()


def test_file_in_tree_normalizes_backslashes(tmp_path):
    resolved = luarocks._file_in_tree(str(tmp_path), "socket\\core.dll")
    assert os.path.normpath(resolved) == os.path.normpath(
        os.path.join(str(tmp_path), "lib", "lua", "5.1", "socket", "core.dll")
    )


def test_rock_files_present_requires_all_files(tmp_path):
    tree = str(tmp_path)
    rock = Rock(name="luasocket", files=["mime\\core.dll", "socket\\core.dll"])
    assert luarocks._rock_files_present(tree, rock) is False

    _write(luarocks._file_in_tree(tree, "mime\\core.dll"))
    assert luarocks._rock_files_present(tree, rock) is False  # still missing one

    _write(luarocks._file_in_tree(tree, "socket\\core.dll"))
    assert luarocks._rock_files_present(tree, rock) is True


def test_is_rock_satisfied_binary_present(tmp_path):
    tree = str(tmp_path)
    binary = Rock(name="luafilesystem", files=["lfs.dll"])

    assert luarocks._is_rock_satisfied(tree, binary) is False
    _write(luarocks._file_in_tree(tree, "lfs.dll"))
    assert luarocks._is_rock_satisfied(tree, binary) is True


def test_is_rock_satisfied_pure_lua_uses_rocks_db(tmp_path):
    # Pure-Lua rocks have no DLL to verify, so we trust luarocks' own record in
    # the tree (lib/luarocks/rocks-5.1/{name}) to avoid a slow reinstall each run.
    tree = str(tmp_path)
    pure = Rock(name="fun")

    assert luarocks._is_rock_satisfied(tree, pure) is False  # not recorded yet
    os.makedirs(os.path.join(tree, "lib", "luarocks", "rocks-5.1", "fun"))
    assert luarocks._is_rock_satisfied(tree, pure) is True


# ---------- _build_install_command ----------


def test_install_command_matches_packageman_shape():
    cmd = luarocks._build_install_command(
        luarocks_exe=r"C:\MQ\luarocks.exe",
        tree_path=r"C:\MQ\modules\2.1.x\luarocks",
        jit_version="2.1.x",
        package="luafilesystem",
    )
    assert cmd[0] == r"C:\MQ\luarocks.exe"
    assert cmd[cmd.index("--lua-version") + 1] == "5.1"
    assert "--skip-config-warning" in cmd
    assert cmd[cmd.index("--only-server") + 1] == "https://luarocks.macroquest.org/2.1.x/"
    assert "install" in cmd
    assert cmd[cmd.index("--deps-mode") + 1] == "none"
    assert cmd[cmd.index("--tree") + 1] == r"C:\MQ\modules\2.1.x\luarocks"
    assert cmd[-1] == "luafilesystem"


# ---------- _run_install ----------


class _FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_run_install_success_keyed_on_exit_code():
    out = "luafilesystem 1.8.0-1 is now installed in C:\\MQ\\modules\\..."
    with patch.object(luarocks.subprocess, "run", return_value=_FakeCompleted(stdout=out, returncode=0)):
        status, _ = luarocks._run_install(["luarocks.exe"], cwd=".")
    assert status == "installed"


def test_run_install_already_installed_is_skipped():
    out = "luafilesystem 1.8.0-1 is already installed in tree."
    with patch.object(luarocks.subprocess, "run", return_value=_FakeCompleted(stdout=out, returncode=0)):
        status, _ = luarocks._run_install(["luarocks.exe"], cwd=".")
    assert status == "skipped"


def test_run_install_dash_e_noise_does_not_fail():
    # The cosmetic "-e flag" warning doesn't change luarocks' exit code.
    out = "-e flag does not exist.\nluafilesystem 1.8.0-1 is now installed in tree.\n"
    with patch.object(luarocks.subprocess, "run", return_value=_FakeCompleted(stdout=out, returncode=0)):
        status, _ = luarocks._run_install(["luarocks.exe"], cwd=".")
    assert status == "installed"


def test_run_install_failure():
    out = "Error: something else went wrong"
    with patch.object(luarocks.subprocess, "run", return_value=_FakeCompleted(stderr=out, returncode=1)):
        status, _ = luarocks._run_install(["luarocks.exe"], cwd=".")
    assert status == "error"


def test_run_install_handles_timeout():
    exc = subprocess.TimeoutExpired(cmd=["luarocks.exe"], timeout=1, output="", stderr="")
    with patch.object(luarocks.subprocess, "run", side_effect=exc):
        status, out = luarocks._run_install(["luarocks.exe"], cwd=".")
    assert status == "error"
    assert "timed out" in out


def test_run_install_handles_oserror():
    with patch.object(luarocks.subprocess, "run", side_effect=OSError("not found")):
        status, out = luarocks._run_install(["luarocks.exe"], cwd=".")
    assert status == "error"
    assert "Failed to launch" in out


# ---------- bootstrap_modules: skip conditions ----------


def _no_call(*_a, **_kw):
    raise AssertionError("should not be called")


def _enable_win(monkeypatch, vvmq):
    monkeypatch.setattr(luarocks.sys, "platform", "win32")
    monkeypatch.setattr(luarocks, "is_luarocks_enabled", lambda: True)
    monkeypatch.setattr(luarocks, "get_vvmq_path", lambda: vvmq)
    # In production config is initialized before the sync reaches bootstrap; the
    # bootstrap path reads config.settings.ENV for the failure hints.
    monkeypatch.setattr(luarocks.config, "settings", SimpleNamespace(ENV="LIVE"))


def _patch_manifest(monkeypatch, rocks):
    async def _fake():
        return rocks
    monkeypatch.setattr(luarocks, "fetch_manifest", _fake)




def test_bootstrap_skipped_on_non_windows(monkeypatch):
    monkeypatch.setattr(luarocks.sys, "platform", "linux")
    monkeypatch.setattr(luarocks, "get_vvmq_path", _no_call)
    assert _run(luarocks.bootstrap_modules()) is True


def test_bootstrap_skipped_when_disabled(monkeypatch):
    monkeypatch.setattr(luarocks.sys, "platform", "win32")
    monkeypatch.setattr(luarocks, "is_luarocks_enabled", lambda: False)
    monkeypatch.setattr(luarocks, "get_vvmq_path", _no_call)
    assert _run(luarocks.bootstrap_modules()) is True


def test_bootstrap_skipped_when_no_vvmq_path(monkeypatch):
    monkeypatch.setattr(luarocks.sys, "platform", "win32")
    monkeypatch.setattr(luarocks, "is_luarocks_enabled", lambda: True)
    monkeypatch.setattr(luarocks, "get_vvmq_path", lambda: None)
    assert _run(luarocks.bootstrap_modules()) is True


def test_bootstrap_skipped_when_no_luarocks_exe(tmp_path, monkeypatch):
    _enable_win(monkeypatch, str(tmp_path))
    assert _run(luarocks.bootstrap_modules()) is True


def test_bootstrap_skipped_when_no_mq2lua_dll(tmp_path, monkeypatch):
    _enable_win(monkeypatch, str(tmp_path))
    _write(os.path.join(str(tmp_path), "luarocks.exe"))
    assert _run(luarocks.bootstrap_modules()) is True


def test_bootstrap_skipped_when_manifest_unavailable(tmp_path, monkeypatch):
    vvmq = str(tmp_path)
    _enable_win(monkeypatch, vvmq)
    _write(os.path.join(vvmq, "luarocks.exe"))
    _write_mq2lua(vvmq)
    _patch_manifest(monkeypatch, None)
    monkeypatch.setattr(luarocks.subprocess, "run", _no_call)
    assert _run(luarocks.bootstrap_modules()) is True


# ---------- bootstrap_modules: install path ----------


def test_bootstrap_skips_already_installed(tmp_path, monkeypatch, capsys):
    vvmq = str(tmp_path)
    _write(os.path.join(vvmq, "luarocks.exe"))
    _write_mq2lua(vvmq)
    tree = _make_tree(vvmq, TEST_JIT_VERSION)
    _write(luarocks._file_in_tree(tree, "lfs.dll"))
    _write(luarocks._file_in_tree(tree, "lsqlite3.dll"))

    _enable_win(monkeypatch, vvmq)
    _patch_manifest(monkeypatch, [
        Rock(name="luafilesystem", files=["lfs.dll"]),
        Rock(name="lsqlite3", files=["lsqlite3.dll"]),
    ])
    monkeypatch.setattr(luarocks.subprocess, "run", _no_call)

    assert _run(luarocks.bootstrap_modules()) is True
    assert "up-to-date" in capsys.readouterr().out


def test_bootstrap_installs_missing_and_verifies(tmp_path, monkeypatch, capsys):
    vvmq = str(tmp_path)
    _write(os.path.join(vvmq, "luarocks.exe"))
    _write_mq2lua(vvmq)
    tree = os.path.join(vvmq, "modules", TEST_JIT_VERSION, "luarocks")

    rocks = [
        Rock(name="luafilesystem", files=["lfs.dll"]),
        Rock(name="lsqlite3", files=["lsqlite3.dll"]),
    ]
    _enable_win(monkeypatch, vvmq)
    _patch_manifest(monkeypatch, rocks)

    name_to_files = {r.name: r.files for r in rocks}

    def _fake_run(cmd, *_a, **_kw):
        name = cmd[-1]
        # Simulate luarocks dropping the DLLs so verification passes.
        for f in name_to_files[name]:
            _write(luarocks._file_in_tree(tree, f))
        return _FakeCompleted(stdout=f"{name} 1.0-1 is now installed")

    monkeypatch.setattr(luarocks.subprocess, "run", _fake_run)

    events: list[tuple] = []
    assert _run(luarocks.bootstrap_modules(on_event=events.append)) is True

    kinds = [e[0] for e in events]
    assert kinds.count("add_total") == 1
    assert kinds.count("start") == 2
    done = [e for e in events if e[0] == "done"]
    assert len(done) == 2
    assert all(e[2] == "downloaded" for e in done)
    out = capsys.readouterr().out
    assert "installing 2 module(s)" in out  # heads-up before the slow part
    assert "can take several minutes" in out
    assert "installed 2 module(s)" in out


def test_bootstrap_marks_already_installed_as_skipped(tmp_path, monkeypatch, capsys):
    vvmq = str(tmp_path)
    _write(os.path.join(vvmq, "luarocks.exe"))
    _write_mq2lua(vvmq)

    _enable_win(monkeypatch, vvmq)
    _patch_manifest(monkeypatch, [Rock(name="fun")])

    def _fake_run(cmd, *_a, **_kw):
        return _FakeCompleted(stdout="fun 0.1-1 is already installed in tree.", returncode=0)

    monkeypatch.setattr(luarocks.subprocess, "run", _fake_run)

    events: list[tuple] = []
    assert _run(luarocks.bootstrap_modules(on_event=events.append)) is True

    assert [e for e in events if e[0] == "done"] == [("done", "fun", "skipped")]
    out = capsys.readouterr().out
    # Skipped (already-present) rocks stay quiet; the run just reports up-to-date.
    assert "fun already installed" not in out
    assert "up-to-date" in out


def test_bootstrap_skips_recorded_pure_lua_without_running(tmp_path, monkeypatch, capsys):
    # A pure-Lua rock already in the tree's rocks db must not re-run the slow install.
    vvmq = str(tmp_path)
    _write(os.path.join(vvmq, "luarocks.exe"))
    _write_mq2lua(vvmq)
    tree = _make_tree(vvmq, TEST_JIT_VERSION)
    os.makedirs(os.path.join(tree, "lib", "luarocks", "rocks-5.1", "fun"))

    _enable_win(monkeypatch, vvmq)
    _patch_manifest(monkeypatch, [Rock(name="fun")])
    monkeypatch.setattr(luarocks.subprocess, "run", _no_call)

    assert _run(luarocks.bootstrap_modules()) is True
    assert "up-to-date" in capsys.readouterr().out


def test_bootstrap_flags_missing_dll_after_install(tmp_path, monkeypatch, capsys):
    """luarocks reports success but the DLL never lands (antivirus case)."""
    vvmq = str(tmp_path)
    _write(os.path.join(vvmq, "luarocks.exe"))
    _write_mq2lua(vvmq)

    _enable_win(monkeypatch, vvmq)
    _patch_manifest(monkeypatch, [Rock(name="luafilesystem", files=["lfs.dll"])])
    # Reports success but writes nothing.
    monkeypatch.setattr(
        luarocks.subprocess, "run",
        lambda *a, **kw: _FakeCompleted(stdout="luafilesystem 1.0-1 is now installed"),
    )

    events: list[tuple] = []
    assert _run(luarocks.bootstrap_modules(on_event=events.append)) is False

    done = [e for e in events if e[0] == "done"]
    assert done == [("done", "luafilesystem", "error")]
    out = capsys.readouterr().out
    assert "missing" in out
    assert "Hints:" in out


def test_bootstrap_reports_failure_and_returns_false(tmp_path, monkeypatch, capsys):
    vvmq = str(tmp_path)
    _write(os.path.join(vvmq, "luarocks.exe"))
    _write_mq2lua(vvmq)

    _enable_win(monkeypatch, vvmq)
    _patch_manifest(monkeypatch, [Rock(name="lyaml", files=["yaml.dll"])])

    def _fake_run(cmd, *_a, **_kw):
        return _FakeCompleted(stderr="Error: download failed", returncode=1)

    monkeypatch.setattr(luarocks.subprocess, "run", _fake_run)

    events: list[tuple] = []
    assert _run(luarocks.bootstrap_modules(on_event=events.append)) is False

    done = [e for e in events if e[0] == "done"]
    assert done == [("done", "lyaml", "error")]
    out = capsys.readouterr().out
    assert "failed to install" in out
    assert "Hints:" in out


def test_bootstrap_emits_luarocks_failed_event_with_redist_url(tmp_path, monkeypatch):
    """On failure the UI gets a luarocks_failed event carrying the matching redist URL."""
    vvmq = str(tmp_path)
    _write(os.path.join(vvmq, "luarocks.exe"))
    _write_mq2lua(vvmq, machine=luarocks.IMAGE_FILE_MACHINE_I386)  # 32-bit build

    _enable_win(monkeypatch, vvmq)
    _patch_manifest(monkeypatch, [Rock(name="lyaml", files=["yaml.dll"])])
    monkeypatch.setattr(
        luarocks.subprocess, "run",
        lambda *a, **kw: _FakeCompleted(stderr="Error: download failed", returncode=1),
    )

    events: list[tuple] = []
    assert _run(luarocks.bootstrap_modules(on_event=events.append)) is False

    # Bitness comes from the DLL's PE header, not the env.
    assert ("luarocks_failed", luarocks.VC_REDIST_X86, None) in events


def test_bootstrap_no_failure_event_on_success(tmp_path, monkeypatch):
    """A clean run must not emit a luarocks_failed event."""
    vvmq = str(tmp_path)
    _write(os.path.join(vvmq, "luarocks.exe"))
    _write_mq2lua(vvmq)
    tree = _make_tree(vvmq, TEST_JIT_VERSION)

    _enable_win(monkeypatch, vvmq)
    _patch_manifest(monkeypatch, [Rock(name="luafilesystem", files=["lfs.dll"])])

    def _fake_run(cmd, *_a, **_kw):
        _write(luarocks._file_in_tree(tree, "lfs.dll"))
        return _FakeCompleted(stdout="luafilesystem 1.0-1 is now installed")

    monkeypatch.setattr(luarocks.subprocess, "run", _fake_run)

    events: list[tuple] = []
    assert _run(luarocks.bootstrap_modules(on_event=events.append)) is True
    assert not any(e[0] == "luarocks_failed" for e in events)


# ---------- is_luarocks_enabled ----------


def test_is_luarocks_enabled_default_on(monkeypatch):
    settings = SimpleNamespace(ENV="LIVE", from_env=lambda env: {})
    monkeypatch.setattr(luarocks.config, "settings", settings)
    assert luarocks.is_luarocks_enabled() is True


def test_is_luarocks_enabled_explicit_false(monkeypatch):
    settings = SimpleNamespace(ENV="LIVE", from_env=lambda env: {"LUAROCKS_ENABLED": False})
    monkeypatch.setattr(luarocks.config, "settings", settings)
    assert luarocks.is_luarocks_enabled() is False
