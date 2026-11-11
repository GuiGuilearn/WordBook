from wordbook.platform_win import DEFAULT_SETTINGS, get_api_key, sanitize_settings, save_settings, load_settings


def test_settings_ignore_secret_and_invalid_values(tmp_path):
    settings = sanitize_settings({"api_key": "not-for-disk", "offset": "invalid", "edge": "bottom", "hotkey": "bad", "autostart": "false"})
    assert "api_key" not in settings
    assert settings["offset"] == 0.5
    assert settings["edge"] == "right"
    assert settings["hotkey"] == "Ctrl+Alt+W"
    assert settings["autostart"] is True
    assert settings["mouse_side_button"] == DEFAULT_SETTINGS["mouse_side_button"]
    save_settings(tmp_path, {**settings, "api_key": "not-for-disk"})
    assert "not-for-disk" not in (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert load_settings(tmp_path) == settings


def test_mouse_side_button_setting_is_validated(tmp_path):
    for value in ("off", "x1", "x2", "both"):
        assert sanitize_settings({"mouse_side_button": value})["mouse_side_button"] == value
    assert sanitize_settings({"mouse_side_button": "left"})["mouse_side_button"] == "both"
    save_settings(tmp_path, {"mouse_side_button": "x2"})
    assert load_settings(tmp_path)["mouse_side_button"] == "x2"


def test_test_data_mode_never_reads_the_real_api_key(monkeypatch):
    monkeypatch.setenv("WORDBOOK_TEST_DATA_DIR", "isolated")
    assert get_api_key() == ""
