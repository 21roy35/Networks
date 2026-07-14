from flight_bot import config as config_module


def test_load_config_does_not_mutate_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "missing.json")
    monkeypatch.setenv("FLIGHTBOT_IMAP_USER", "first@example.com")
    monkeypatch.setenv("FLIGHTBOT_IMAP_PASSWORD", "secret")
    first = config_module.load_config()
    assert first["user"]["email"] == "first@example.com"

    monkeypatch.delenv("FLIGHTBOT_IMAP_USER")
    monkeypatch.delenv("FLIGHTBOT_IMAP_PASSWORD")
    second = config_module.load_config()
    assert second["imap"]["user"] == ""
    assert second["user"]["email"] == ""
    assert config_module.DEFAULTS["imap"]["user"] == ""


def test_telegram_environment_credentials_enable_integration(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "missing.json")
    monkeypatch.setenv("FLIGHTBOT_TELEGRAM_BOT_TOKEN", "123:test-token")
    monkeypatch.setenv("FLIGHTBOT_TELEGRAM_CHAT_ID", "987654")
    config = config_module.load_config()
    assert config["telegram"]["enabled"] is True
    assert config["telegram"]["bot_token"] == "123:test-token"
    assert config["telegram"]["chat_id"] == "987654"


def test_anthropic_environment_key_enables_ghala(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "missing.json")
    monkeypatch.setenv("FLIGHTBOT_ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("FLIGHTBOT_AI_NAME", "Ghala-200")
    monkeypatch.setenv("FLIGHTBOT_AI_MODEL", "claude-sonnet-5")
    config = config_module.load_config()
    assert config["ai"]["enabled"] is True
    assert config["ai"]["api_key"] == "test-key"
    assert config["ai"]["name"] == "Ghala-200"
    assert config["ai"]["model"] == "claude-sonnet-5"


def test_2captcha_environment_key_enables_solver(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "missing.json")
    monkeypatch.setenv("FLIGHTBOT_2CAPTCHA_API_KEY", "test-captcha-key")
    config = config_module.load_config()
    assert config["captcha"]["enabled"] is True
    assert config["captcha"]["provider"] == "2captcha"
    assert config["captcha"]["api_key"] == "test-captcha-key"
