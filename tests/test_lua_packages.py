from __future__ import annotations

from pathlib import Path
import subprocess

from redfetch import lua_packages


def _make_tree(root: Path, version: str) -> Path:
    tree = root / "modules" / version / "luarocks"
    (tree / "lib" / "lua" / lua_packages.MQ_LUAROCKS_LUA_VERSION).mkdir(parents=True)
    return tree


def test_find_luarocks_tree_prefers_latest_versioned_directory(tmp_path: Path):
    mq_path = tmp_path / "MQ"
    modules_dir = mq_path / "modules"
    (modules_dir / "luarocks").mkdir(parents=True)
    _make_tree(mq_path, "2.1.1697887905")
    latest = _make_tree(mq_path, "2.1.1774638290")

    selected = lua_packages.find_luarocks_tree(mq_path)

    assert selected == latest


def test_find_luarocks_tree_ignores_nonnumeric_versioned_directory(tmp_path: Path):
    mq_path = tmp_path / "MQ"
    _make_tree(mq_path, "2.1.beta")
    latest = _make_tree(mq_path, "2.1.1774638290")

    selected = lua_packages.find_luarocks_tree(mq_path)

    assert selected == latest


def test_install_command_uses_tree_jit_version_repo(tmp_path: Path):
    tree = _make_tree(tmp_path / "MQ", "2.1.1774638290")
    command = lua_packages._package_install_command(Path("luarocks.exe"), tree, "lsqlite3")

    assert "https://luarocks.macroquest.org/2.1.1774638290/" in command


def test_ensure_common_lua_packages_installs_missing_packages(tmp_path: Path, monkeypatch):
    mq_path = tmp_path / "MQ"
    mq_path.mkdir()
    (mq_path / "luarocks.exe").write_text("")
    tree = _make_tree(mq_path, "2.1.1774638290")

    commands: list[list[str]] = []

    def fake_run(command, capture_output, text, check, timeout):
        commands.append(command)
        assert timeout == lua_packages.LUAROCKS_INSTALL_TIMEOUT_SECONDS
        package_name = command[-1]
        lua_dir = tree / "lib" / "lua" / lua_packages.MQ_LUAROCKS_LUA_VERSION
        if package_name == "lsqlite3":
            (lua_dir / "lsqlite3.dll").write_text("")
        elif package_name == "luafilesystem":
            (lua_dir / "lfs.dll").write_text("")
        return subprocess.CompletedProcess(command, 0, stdout=f"{package_name} is now installed", stderr="")

    monkeypatch.setattr(lua_packages.subprocess, "run", fake_run)

    result = lua_packages.ensure_common_lua_packages(mq_path)

    assert result.error is None
    assert result.target_tree == tree
    assert [status.package_name for status in result.statuses] == ["lsqlite3", "luafilesystem"]
    assert all(status.installed for status in result.statuses)
    assert all(status.install_attempted for status in result.statuses)
    assert commands[0][-1] == "lsqlite3"
    assert commands[1][-1] == "luafilesystem"


def test_install_common_lua_package_reports_subprocess_timeout(tmp_path: Path, monkeypatch):
    tree = _make_tree(tmp_path / "MQ", "2.1.1774638290")
    package = lua_packages.CommonLuaPackage("lsqlite3", "lsqlite3")

    def fake_run(command, capture_output, text, check, timeout):
        raise subprocess.TimeoutExpired(command, timeout, output="partial stdout", stderr="partial stderr")

    monkeypatch.setattr(lua_packages.subprocess, "run", fake_run)

    status = lua_packages.install_common_lua_package(
        luarocks_exe=Path("luarocks.exe"),
        tree=tree,
        package=package,
    )

    assert status.installed is False
    assert status.install_attempted is True
    assert status.install_succeeded is False
    assert "TimeoutExpired" in status.detail
    assert "partial stdout" in status.detail
    assert "partial stderr" in status.detail


def test_ensure_common_lua_packages_reports_missing_luarocks_exe(tmp_path: Path):
    mq_path = tmp_path / "MQ"
    mq_path.mkdir()
    _make_tree(mq_path, "2.1.1774638290")

    result = lua_packages.ensure_common_lua_packages(mq_path)

    assert result.error == f"luarocks.exe not found in {mq_path}"
    assert result.statuses == ()
