"""settings.local.toml is machine-managed: redfetch regenerates it deterministically
and stores only the user's deltas from the bundled settings.toml defaults."""

import tomllib

from redfetch import config


def _render(tmp_path, data, monkeypatch):
    # base defaults include an @format DOWNLOAD_FOLDER that resolves against this
    monkeypatch.setenv("REDFETCH_DATA_DIR", str(tmp_path))
    out = tmp_path / "settings.local.toml"
    config.save_config(str(out), data)
    return out.read_text(encoding="utf-8")


def test_drops_values_equal_to_defaults(tmp_path, monkeypatch):
    # KissAssist (4) defaults to opt_in=true, so storing it again is redundant.
    # MySEQ (151) defaults to opt_in=false, so opt_in=true is a real change.
    data = {"LIVE": {"SPECIAL_RESOURCES": {
        "4": {"opt_in": True},
        "151": {"opt_in": True},
    }}}
    parsed = tomllib.loads(_render(tmp_path, data, monkeypatch))
    sr = parsed["LIVE"]["SPECIAL_RESOURCES"]
    assert "4" not in sr
    assert sr["151"] == {"opt_in": True}


def test_keeps_opt_out_that_differs_from_default(tmp_path, monkeypatch):
    # Opting out of a staff pick (default true) writes false, which is a real delta.
    data = {"LIVE": {"SPECIAL_RESOURCES": {"4": {"opt_in": False}}}}
    parsed = tomllib.loads(_render(tmp_path, data, monkeypatch))
    assert parsed["LIVE"]["SPECIAL_RESOURCES"]["4"] == {"opt_in": False}


def test_emit_is_idempotent(tmp_path, monkeypatch):
    data = {"LIVE": {"EQPATH": "C:/EQ", "SPECIAL_RESOURCES": {"151": {"opt_in": True}}}}
    first = _render(tmp_path, data, monkeypatch)
    second = _render(tmp_path, tomllib.loads(first), monkeypatch)
    assert first == second


def test_accepts_tomlkit_document(tmp_path, monkeypatch):
    import tomlkit
    doc = tomlkit.parse('[LIVE.SPECIAL_RESOURCES.151]\nopt_in = true\n')
    parsed = tomllib.loads(_render(tmp_path, doc, monkeypatch))
    assert parsed["LIVE"]["SPECIAL_RESOURCES"]["151"] == {"opt_in": True}
