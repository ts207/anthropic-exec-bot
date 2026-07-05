from __future__ import annotations

import pytest


def test_settings_accept_shared_project_env_names(monkeypatch) -> None:
    from polybot.config import load_settings

    for key in [
        "POLYBOT_PRIVATE_KEY",
        "POLYBOT_CLOB_API_KEY",
        "POLYBOT_CLOB_SECRET",
        "POLYBOT_CLOB_PASSPHRASE",
        "POLYBOT_FUNDER_ADDRESS",
        "POLYBOT_SIGNATURE_TYPE",
        "POLYBOT_CHAIN_ID",
        "POLYBOT_CLOB_HOST",
    ]:
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("PRIVATE_KEY", "0xabc")
    monkeypatch.setenv("CLOB_API_KEY", "key")
    monkeypatch.setenv("CLOB_SECRET", "secret")
    monkeypatch.setenv("CLOB_PASS_PHRASE", "pass")
    monkeypatch.setenv("DEPOSIT_WALLET_ADDRESS", "0xdef")
    monkeypatch.setenv("CHAIN_ID", "137")
    monkeypatch.setenv("CLOB_HOST", "https://clob.example")

    settings = load_settings()
    assert settings.private_key == "0xabc"
    assert settings.clob_api_key == "key"
    assert settings.clob_secret == "secret"
    assert settings.clob_passphrase == "pass"
    assert settings.funder_address == "0xdef"
    assert settings.signature_type == 3
    assert settings.chain_id == 137
    assert settings.clob_host == "https://clob.example"


def test_settings_defaults_legacy_funder_to_proxy_signature(monkeypatch) -> None:
    from polybot.config import load_settings

    monkeypatch.delenv("DEPOSIT_WALLET_ADDRESS", raising=False)
    monkeypatch.delenv("POLYBOT_SIGNATURE_TYPE", raising=False)
    monkeypatch.setenv("FUNDER_ADDRESS", "0xdef")

    settings = load_settings()
    assert settings.funder_address == "0xdef"
    assert settings.signature_type == 1


def test_settings_accepts_poly_1271_signature_type(monkeypatch) -> None:
    from polybot.config import load_settings

    monkeypatch.setenv("POLYBOT_SIGNATURE_TYPE", "3")

    assert load_settings().signature_type == 3


def test_settings_rejects_unsupported_signature_type(monkeypatch) -> None:
    from polybot.config import load_settings

    monkeypatch.setenv("POLYBOT_SIGNATURE_TYPE", "4")
    with pytest.raises(ValueError, match="POLYBOT_SIGNATURE_TYPE"):
        load_settings()


def test_main_dispatches_inspect_iran_position(monkeypatch, tmp_path) -> None:
    from polybot import main as main_module

    called = {}

    def fake_command(path):
        called["path"] = path
        return 0

    monkeypatch.setattr("polybot.iran.runner.inspect_iran_position_command", fake_command)

    assert main_module.main(["inspect-iran-position", "--config", str(tmp_path / "iran.yaml")]) == 0
    assert called["path"] == tmp_path / "iran.yaml"


def test_main_dispatches_probe_iran_clob_v2(monkeypatch, tmp_path) -> None:
    from polybot import main as main_module

    called = {}

    def fake_command(path, amount: float = 1.0, post: bool = False, price: float | None = None):
        called["path"] = path
        called["amount"] = amount
        called["post"] = post
        called["price"] = price
        return 0

    monkeypatch.setattr("polybot.iran.runner.probe_iran_clob_v2_command", fake_command)

    assert main_module.main(["probe-iran-clob-v2", "--config", str(tmp_path / "iran.yaml"), "--amount", "2.5", "--post", "--price", "0.99"]) == 0
    assert called == {"path": tmp_path / "iran.yaml", "amount": 2.5, "post": True, "price": 0.99}


def test_main_dispatches_smoke_iran_classifier(monkeypatch, tmp_path) -> None:
    from polybot import main as main_module

    called = {}

    def fake_command(path, *, url=None, text=None, title="classifier smoke", domain="reuters.com"):
        called.update({"path": path, "url": url, "text": text, "title": title, "domain": domain})
        return 0

    monkeypatch.setattr("polybot.iran.runner.smoke_iran_classifier_command", fake_command)

    assert main_module.main(["smoke-iran-classifier", "--config", str(tmp_path / "iran.yaml"), "--text", "hello", "--domain", "apnews.com"]) == 0
    assert called == {"path": tmp_path / "iran.yaml", "url": None, "text": "hello", "title": "classifier smoke", "domain": "apnews.com"}
