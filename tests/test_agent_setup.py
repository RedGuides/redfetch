"""Tests for the agent doc, `redfetch agent`, typed config coercion, and `server add`."""
import tomllib

import pytest
from typer.testing import CliRunner
from conftest import _install_settings, flat_output

from redfetch import config, main, net, servers, sync_discovery, utils

runner = CliRunner()


# ===== the shipped agent doc =====

def test_agent_docs_load_from_package_data():
    doc = main.load_agent_docs()
    assert "redfetch" in doc
    assert "settings.local.toml" in doc


def test_agent_docs_manifest_url_matches_net():
    """The doc's manifest URL and net.MANIFEST_URL can't drift apart."""
    assert net.MANIFEST_URL in main.load_agent_docs()


def test_agent_command_prints_doc():
    result = runner.invoke(main.app, ["agent"])
    assert result.exit_code == 0
    assert "resources-manifest" in result.output


# ===== coerce_setting_value =====

@pytest.fixture
def live_settings(tmp_path, monkeypatch):
    settings = _install_settings(tmp_path, monkeypatch, env="LIVE", current_env="LIVE")
    return settings, tmp_path


def test_lone_filename_becomes_list(live_settings):
    """The effective value is a list, so a bare entry wraps instead of clobbering."""
    value = config.coerce_setting_value(
        ["PROTECTED_FILES_BY_RESOURCE", "1974"], ["MyFile.ini"], env="LIVE")
    assert value == ["MyFile.ini"]


def test_variadic_values_become_list(live_settings):
    value = config.coerce_setting_value(
        ["PROTECTED_FILES_BY_RESOURCE", "1974"], ["a.ini", "b.ini"], env="LIVE")
    assert value == ["a.ini", "b.ini"]


def test_unknown_protected_id_is_still_a_list(live_settings):
    """An id with no shipped default still wraps a bare value; a stored string
    would be iterated per character downstream and protect nothing."""
    path = ["PROTECTED_FILES_BY_RESOURCE", "9999"]
    assert config.coerce_setting_value(path, ["x.ini"], env="LIVE") == ["x.ini"]
    assert config.coerce_setting_value(path, ['["x.ini"]'], env="LIVE") == ["x.ini"]


def test_explicit_array_heals_a_corrupted_list_setting(tmp_path, monkeypatch):
    """A lone-string corruption can be re-typed by passing the TOML array spelling."""
    _install_settings(tmp_path, monkeypatch, env="LIVE", current_env="LIVE",
                      local_toml='[LIVE.PROTECTED_FILES_BY_RESOURCE]\n1974 = "oops.ini"\n')
    value = config.coerce_setting_value(
        ["PROTECTED_FILES_BY_RESOURCE", "1974"], ['["MyFile.ini"]'], env="LIVE")
    assert value == ["MyFile.ini"]


def test_post_update_command_stays_string(live_settings):
    """A command may be a whole command line; wrapping it in a one-element argv
    list would make argv[0] the entire line and break launching."""
    value = config.coerce_setting_value(
        ["POST_UPDATE_LAUNCH", "command"], ["C:/tools/launch.exe --flag"], env="LIVE")
    assert value == "C:/tools/launch.exe --flag"


def test_post_update_targets_bare_value_stays_string(live_settings):
    """`targets` ships no default (a default would mask the legacy `target` key),
    so a bare value stores a string; get_post_update_targets normalizes those."""
    value = config.coerce_setting_value(
        ["POST_UPDATE_LAUNCH", "targets"], ["custom"], env="LIVE")
    assert value == "custom"


def test_unset_path_takes_toml_typed_value(live_settings):
    """Values are TOML fragments: quoting is the escape hatch for literal strings."""
    assert config.coerce_setting_value(["SOME_NEW_KEY"], ["42"], env="LIVE") == 42
    assert config.coerce_setting_value(["SOME_NEW_KEY"], ['"42"'], env="LIVE") == "42"


def test_date_like_values_stay_strings(live_settings):
    """TOML date/time literals would type into datetime objects; no setting wants
    those, so they stay the strings the user typed."""
    for raw in ("2024-01-01", "13:30:00", "2024-01-01T10:00:00"):
        assert config.coerce_setting_value(["SOME_NEW_KEY"], [raw], env="LIVE") == raw


def test_bool_path_coerces_true_false(live_settings):
    path = ["SPECIAL_RESOURCES", "1974", "opt_in"]
    assert config.coerce_setting_value(path, ["false"], env="LIVE") is False
    assert config.coerce_setting_value(path, ["True"], env="LIVE") is True


def test_bool_path_rejects_garbage(live_settings):
    with pytest.raises(ValueError, match="true or false"):
        config.coerce_setting_value(
            ["SPECIAL_RESOURCES", "1974", "opt_in"], ["yeah"], env="LIVE")


def test_inline_table_value_rejected(live_settings):
    with pytest.raises(ValueError, match="table"):
        config.coerce_setting_value(["EQPATH"], ["{x=1}"], env="LIVE")


def test_string_path_keeps_numeric_looking_value_a_string(live_settings):
    value = config.coerce_setting_value(
        ["SPECIAL_RESOURCES", "151", "custom_path"], ["123"], env="LIVE")
    assert value == "123"


def test_bool_leaf_typed_without_any_stored_value(live_settings):
    assert config.coerce_setting_value(
        ["SPECIAL_RESOURCES", "9999", "opt_in"], ["true"], env="LIVE") is True


def test_string_path_stays_string(live_settings):
    value = config.coerce_setting_value(
        ["SPECIAL_RESOURCES", "151", "custom_path"], ["D:/MySEQ"], env="LIVE")
    assert value == "D:/MySEQ"


def test_table_path_rejected(live_settings):
    with pytest.raises(ValueError, match="table"):
        config.coerce_setting_value(["SPECIAL_RESOURCES", "1974"], ["x"], env="LIVE")


def test_multiple_values_on_scalar_path_rejected(live_settings):
    with pytest.raises(ValueError, match="single value"):
        config.coerce_setting_value(
            ["SPECIAL_RESOURCES", "151", "custom_path"], ["a", "b"], env="LIVE")


# ===== apply_list_edits (--add / --remove) =====

def test_add_appends_to_effective_list(live_settings):
    items = config.apply_list_edits(
        ["PROTECTED_FILES_BY_RESOURCE", "1974"], ["MyFile.ini"], [], env="LIVE")
    assert items[-1] == "MyFile.ini"
    assert "CharSelect.cfg" in items  # existing entries preserved


def test_add_dedupes_case_insensitively(live_settings):
    items = config.apply_list_edits(
        ["PROTECTED_FILES_BY_RESOURCE", "1974"], ["charselect.cfg"], [], env="LIVE")
    assert sum(1 for item in items if item.lower() == "charselect.cfg") == 1


def test_remove_drops_case_insensitively(live_settings):
    items = config.apply_list_edits(
        ["PROTECTED_FILES_BY_RESOURCE", "1974"], ["MyFile.ini"], ["myfile.ini"], env="LIVE")
    assert all(item.lower() != "myfile.ini" for item in items)


def test_remove_rejects_shipped_default(live_settings):
    """dynaconf merges the bundle back in on read, so removing one can't work."""
    with pytest.raises(ValueError, match="shipped default"):
        config.apply_list_edits(
            ["PROTECTED_FILES_BY_RESOURCE", "1974"], [], ["charselect.cfg"], env="LIVE")


def test_list_edits_rejected_on_scalar_path(live_settings):
    with pytest.raises(ValueError, match="list settings"):
        config.apply_list_edits(
            ["SPECIAL_RESOURCES", "151", "custom_path"], ["x"], [], env="LIVE")


# ===== read_setting and the config CLI =====

def test_read_setting_default_value(live_settings):
    assert config.read_setting(["SPECIAL_RESOURCES", "1974", "opt_in"], env="LIVE") is True


def test_read_setting_missing_is_sentinel(live_settings):
    assert config.read_setting(["NO_SUCH_SETTING"], env="LIVE") is config.MISSING


def test_read_setting_past_scalar_raises(live_settings):
    """Descending past a scalar leaf is a malformed path, not 'not set' --
    otherwise the write path would replace the scalar with a table."""
    with pytest.raises(ValueError, match="isn't a valid setting path"):
        config.read_setting(["SPECIAL_RESOURCES", "1974", "opt_in", "deeper"], env="LIVE")


def test_read_setting_named_index_into_list_raises(live_settings):
    with pytest.raises(ValueError, match="isn't a valid setting path"):
        config.read_setting(["PROTECTED_FILES_BY_RESOURCE", "1974", "foo"], env="LIVE")


@pytest.fixture
def config_cli(live_settings, monkeypatch):
    """Run the config CLI against the temp settings."""
    settings, tmp_path = live_settings
    monkeypatch.setattr(config, "initialize_config", lambda: settings)
    return settings, tmp_path


def test_config_read_prints_json_bool(config_cli):
    result = runner.invoke(main.app, ["config", "SPECIAL_RESOURCES.1974.opt_in"])
    assert result.exit_code == 0
    assert result.output.strip() == "true"


def test_config_read_unset_exits_nonzero(config_cli):
    result = runner.invoke(main.app, ["config", "NO_SUCH_SETTING"])
    assert result.exit_code == 1


def test_config_write_naive_filename_persists_as_toml_array(config_cli):
    _, tmp_path = config_cli
    result = runner.invoke(
        main.app, ["config", "PROTECTED_FILES_BY_RESOURCE.1974", "MyFile.ini"])
    assert result.exit_code == 0
    with open(tmp_path / "settings.local.toml", "rb") as f:
        data = tomllib.load(f)
    assert data["LIVE"]["PROTECTED_FILES_BY_RESOURCE"]["1974"] == ["MyFile.ini"]


def test_config_bare_target_still_resolves_through_reader(config_cli):
    """agent_docs documents `config POST_UPDATE_LAUNCH.targets custom`; with no
    default to type against it stores a string, which the reader normalizes."""
    result = runner.invoke(main.app, ["config", "POST_UPDATE_LAUNCH.targets", "custom"])
    assert result.exit_code == 0
    assert utils.get_post_update_targets("LIVE") == ["custom"]


def test_config_add_stores_only_the_delta(config_cli):
    """The bundle merges back in on read, so storing it too would show every
    shipped entry twice."""
    _, tmp_path = config_cli
    result = runner.invoke(
        main.app, ["config", "PROTECTED_FILES_BY_RESOURCE.1974", "--add", "MyFile.ini"])
    assert result.exit_code == 0
    with open(tmp_path / "settings.local.toml", "rb") as f:
        data = tomllib.load(f)
    assert data["LIVE"]["PROTECTED_FILES_BY_RESOURCE"]["1974"] == ["MyFile.ini"]
    effective = config.read_setting(["PROTECTED_FILES_BY_RESOURCE", "1974"], env="LIVE")
    assert effective.count("CharSelect.cfg") == 1 and "MyFile.ini" in effective


def test_config_remove_of_shipped_default_is_a_usage_error(config_cli):
    result = runner.invoke(
        main.app, ["config", "PROTECTED_FILES_BY_RESOURCE.1974", "--remove", "CharSelect.cfg"])
    assert result.exit_code == 2
    assert "shipped default" in flat_output(result)


def test_config_write_unknown_id_persists_list_and_protects(config_cli):
    _, tmp_path = config_cli
    result = runner.invoke(main.app, ["config", "PROTECTED_FILES_BY_RESOURCE.9999", "x.ini"])
    assert result.exit_code == 0
    with open(tmp_path / "settings.local.toml", "rb") as f:
        data = tomllib.load(f)
    assert data["LIVE"]["PROTECTED_FILES_BY_RESOURCE"]["9999"] == ["x.ini"]
    assert sync_discovery._get_protected_files("9999", "LIVE") == ["x.ini"]


def test_config_remove_drops_user_added_entry_effectively(config_cli):
    """--remove must change the *effective* list, not just the stored delta.
    Shipped-default entries are permanent (dynaconf merges them back in)."""
    runner.invoke(
        main.app, ["config", "PROTECTED_FILES_BY_RESOURCE.1974", "--add", "MyFile.ini"])
    result = runner.invoke(
        main.app, ["config", "PROTECTED_FILES_BY_RESOURCE.1974", "--remove", "MyFile.ini"])
    assert result.exit_code == 0
    effective = config.read_setting(["PROTECTED_FILES_BY_RESOURCE", "1974"], env="LIVE")
    assert "MyFile.ini" not in effective
    assert "CharSelect.cfg" in effective


def test_lowercase_path_cannot_fork_shadow_table(config_cli):
    """Reads case-fold, so a second write in different casing must reuse the first table."""
    _, tmp_path = config_cli
    for path in ("PROTECTED_FILES_BY_RESOURCE.1974", "protected_files_by_resource.1974"):
        result = runner.invoke(main.app, ["config", path, "--add", "MyFile.ini"])
        assert result.exit_code == 0
    with open(tmp_path / "settings.local.toml", "rb") as f:
        data = tomllib.load(f)
    tables = [key for key in data["LIVE"] if key.lower() == "protected_files_by_resource"]
    assert len(tables) == 1
    assert data["LIVE"][tables[0]]["1974"].count("MyFile.ini") == 1


def test_update_reuses_existing_local_casing(tmp_path, monkeypatch):
    """A hand-edited table with non-canonical casing keeps receiving the writes."""
    _install_settings(tmp_path, monkeypatch, env="LIVE", current_env="LIVE",
                      local_toml='[LIVE.protected_files_by_resource]\n1974 = ["User.ini"]\n')
    config.update_setting(["PROTECTED_FILES_BY_RESOURCE", "1974"],
                          ["User.ini", "New.ini"], env="LIVE")
    with open(tmp_path / "settings.local.toml", "rb") as f:
        data = tomllib.load(f)
    tables = [key for key in data["LIVE"] if key.lower() == "protected_files_by_resource"]
    assert len(tables) == 1


def test_config_values_and_add_are_exclusive(config_cli):
    result = runner.invoke(
        main.app,
        ["config", "PROTECTED_FILES_BY_RESOURCE.1974", "a.ini", "--add", "b.ini"])
    assert result.exit_code == 2
    assert "not both" in flat_output(result)


def test_config_bool_garbage_is_a_usage_error(config_cli):
    result = runner.invoke(main.app, ["config", "SPECIAL_RESOURCES.1974.opt_in", "yeah"])
    assert result.exit_code == 2
    assert "true or false" in flat_output(result)


# ===== redfetch server add =====

@pytest.fixture
def add_env(monkeypatch):
    calls = []
    monkeypatch.setattr(config, "initialize_config", lambda: None)
    monkeypatch.setattr(servers, "list_servers", lambda env: {})
    monkeypatch.setattr(
        servers, "add_server",
        lambda slug, **kwargs: calls.append((slug, kwargs)),
    )
    return calls


def _eq_folder(tmp_path):
    folder = tmp_path / "EverQuest"
    folder.mkdir()
    (folder / "eqgame.exe").write_bytes(b"MZ")
    return folder


def test_server_add_configures_server(add_env, tmp_path):
    folder = _eq_folder(tmp_path)
    result = runner.invoke(main.app, ["server", "add", "myserver",
                                      "--eqpath", str(folder), "--label", "My Server"])
    assert result.exit_code == 0
    assert add_env == [("myserver", {
        "env": "EMU", "eqpath": str(folder), "label": "My Server",
        "patcher_url": None, "patcher_exe": None, "guide": None, "shortname": None,
    })]


def test_server_add_passes_guide_shortname_and_patcher(add_env, tmp_path):
    folder = _eq_folder(tmp_path)
    result = runner.invoke(main.app, [
        "server", "add", "myserver", "--eqpath", str(folder),
        "--patcher-url", "https://example.com/patcher.zip",
        "--patcher-exe", "ThePatcher.exe",
        "--guide", "https://example.com/guide", "--shortname", "myserver"])
    assert result.exit_code == 0
    assert add_env == [("myserver", {
        "env": "EMU", "eqpath": str(folder), "label": None,
        "patcher_url": "https://example.com/patcher.zip",
        "patcher_exe": "ThePatcher.exe",
        "guide": "https://example.com/guide", "shortname": "myserver",
    })]


@pytest.fixture
def real_add(tmp_path, monkeypatch):
    """The real writer against a scratch config, for the gates inside add_server."""
    _install_settings(tmp_path, monkeypatch, env="EMU", current_env="EMU")
    def add(**kwargs):
        servers.add_server("myserver", env="EMU", eqpath="D:/EQ-MyServer", **kwargs)
    return add


def test_add_server_requires_the_file_name_with_a_link(real_add):
    """A lone link is useless: the name is what it's saved as, or picked out of the zip."""
    with pytest.raises(ValueError, match="file name too"):
        real_add(patcher_url="https://example.com/patcher.zip")


def test_add_server_accepts_a_patcher_without_a_link(real_add):
    """A patcher the user already has (from the client, or a friend) has nothing to fetch."""
    real_add(patcher_exe="ThePatcher.exe")
    entry = servers.list_servers("EMU")["myserver"]
    assert (entry.get("patcher_url", ""), entry["patcher_exe"]) == ("", "ThePatcher.exe")


def test_add_server_rejects_bad_patcher_url(real_add):
    with pytest.raises(ValueError, match="web link"):
        real_add(patcher_url="ftp://example.com/patcher.zip", patcher_exe="ThePatcher.exe")


def test_add_server_rejects_bad_patcher_exe(real_add):
    with pytest.raises(ValueError, match="bare Windows file name"):
        real_add(patcher_url="https://example.com/patcher.zip", patcher_exe="tools\\ThePatcher.exe")


def test_add_server_rejects_bad_guide_url(real_add):
    with pytest.raises(ValueError, match="web link"):
        real_add(guide="not a url")


def test_add_server_persists_guide_and_shortname(tmp_path, monkeypatch):
    """Through the real writer: custom servers keep guide/shortname, bundle owns known ones."""
    _install_settings(tmp_path, monkeypatch, env="EMU", current_env="EMU")
    servers.add_server("myserver", env="EMU", eqpath="D:/EQ-MyServer",
                       label="My Server", guide="https://example.com/guide",
                       shortname="myserver")
    entry = servers.list_servers("EMU")["myserver"]
    assert entry["guide"] == "https://example.com/guide"
    assert entry["shortname"] == "myserver"

    servers.add_server("lazarus", env="EMU", eqpath="D:/EQ-Laz",
                       guide="https://example.com/not-the-real-guide", shortname="nope")
    entry = servers.list_servers("EMU")["lazarus"]
    assert entry["guide"] == "https://www.lazaruseq.com/pages/getting-started-guide"
    assert entry["shortname"] == "Project Lazarus"


def test_server_add_requires_name(add_env):
    result = runner.invoke(main.app, ["server", "add"])
    assert result.exit_code == 2
    assert "server add" in flat_output(result)
    assert add_env == []


def test_server_add_requires_eqpath(add_env):
    result = runner.invoke(main.app, ["server", "add", "myserver"])
    assert result.exit_code == 2
    assert "--eqpath" in flat_output(result)
    assert add_env == []


def test_server_add_rejects_folder_without_eqgame(add_env, tmp_path):
    not_eq = tmp_path / "NotEverQuest"
    not_eq.mkdir()
    result = runner.invoke(main.app, ["server", "add", "myserver", "--eqpath", str(not_eq)])
    assert result.exit_code == 2
    assert "eqgame.exe" in flat_output(result)
    assert add_env == []


def test_server_add_rejects_reserved_names(tmp_path, monkeypatch):
    """Through the real writer: add_server's reserved-name refusal reaches the CLI as exit 2."""
    _install_settings(tmp_path, monkeypatch, env="EMU", current_env="EMU")
    monkeypatch.setattr(config, "initialize_config", lambda: None)
    folder = _eq_folder(tmp_path)
    result = runner.invoke(main.app, ["server", "add", "none", "--eqpath", str(folder)])
    assert result.exit_code == 2
    assert "reserved" in flat_output(result)
    assert "none" not in servers.list_servers("EMU")


def test_add_is_a_reserved_slug():
    """A server literally named 'add' could never be switched to."""
    with pytest.raises(ValueError, match="reserved"):
        servers.validate_server_slug("add")


def test_patcher_pair_shared_by_cli_and_tui():
    """One validator serves both surfaces, so the gates can't drift."""
    assert servers.validate_patcher_pair("", "") == ("", "")
    assert servers.validate_patcher_pair("", " ThePatcher.exe ") == ("", "ThePatcher.exe")
    assert servers.validate_patcher_pair(
        " https://example.com/p.zip ", " ThePatcher.exe ",
    ) == ("https://example.com/p.zip", "ThePatcher.exe")


def test_switch_args_rejected_without_add(add_env, tmp_path):
    folder = _eq_folder(tmp_path)
    result = runner.invoke(main.app, ["server", "lazarus", "--eqpath", str(folder)])
    assert result.exit_code == 2
    assert "server add" in flat_output(result)
    assert add_env == []
