import importlib
import time


def get_bot(monkeypatch):
    monkeypatch.setenv("CI", "true")
    monkeypatch.delenv("TWELVE_DATA_API_KEY", raising=False)
    monkeypatch.delenv("TWELVEDATA_API_KEY", raising=False)
    return importlib.import_module("bot")


def test_model_capability_filter_rejects_non_text_families(monkeypatch):
    bot = get_bot(monkeypatch)
    for name in (
        "gemini-3.1-flash-live-preview",
        "gemini-3.1-flash-image",
        "gemini-2.5-flash-native-audio-latest",
        "gemini-robotics-er-2-preview",
        "deep-research-pro-preview-12-2025",
        "gemini-2.5-computer-use-preview-10-2025",
    ):
        assert bot.is_compatible_generate_content_model(name) is False


def test_model_capability_filter_accepts_text_models(monkeypatch):
    bot = get_bot(monkeypatch)
    assert bot.is_compatible_generate_content_model("gemini-2.5-pro") is True
    assert bot.is_compatible_generate_content_model("gemini-3.5-flash") is True


def test_priority_candidates_are_bounded_and_exclude_incompatible_models(monkeypatch):
    bot = get_bot(monkeypatch)
    monkeypatch.setattr(
        bot,
        "discover_available_models",
        lambda force_refresh=False: [
            "gemini-3.7-flash",
            "gemini-3.1-flash-image-preview",
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "deep-research-max-preview-04-2026",
            "gemini-3-flash-preview",
            "gemini-3.5-flash",
            "gemini-3.6-flash",
        ],
    )
    candidates = bot.prioritize_models_for_task()
    assert len(candidates) <= bot.MAX_GEMINI_CANDIDATES
    assert all(bot.is_compatible_generate_content_model(name) for name in candidates)


def test_invalid_400_marks_model_blacklisted_and_moves_on(monkeypatch):
    bot = get_bot(monkeypatch)
    bot.SESSION_BLACKLIST_404.clear()
    bot.SESSION_BLACKLIST_INCOMPATIBLE.clear()
    bot.MODEL_COOLDOWNS_429.clear()

    monkeypatch.setattr(bot, "prioritize_models_for_task", lambda task_type="vetting": ["bad-model", "good-model"])

    class Models:
        def generate_content(self, model, contents, config):
            if model == "bad-model":
                raise Exception("400 INVALID_ARGUMENT: This model only supports Interactions API.")
            return type("Response", (), {"text": "{\"ok\":true}"})()

    client = type("Client", (), {"models": Models()})()
    monkeypatch.setattr(bot, "gemini_client", client)

    text, selected = bot.execute_gemini_dynamic_request("ping")
    assert selected == "good-model"
    assert "bad-model" in bot.SESSION_BLACKLIST_INCOMPATIBLE


def test_generic_400_is_not_marked_as_model_incompatible(monkeypatch):
    bot = get_bot(monkeypatch)
    bot.SESSION_BLACKLIST_404.clear()
    bot.SESSION_BLACKLIST_INCOMPATIBLE.clear()
    bot.MODEL_COOLDOWNS_429.clear()

    monkeypatch.setattr(bot, "prioritize_models_for_task", lambda task_type="vetting": ["config-error-model", "good-model"])

    class Models:
        def generate_content(self, model, contents, config):
            if model == "config-error-model":
                raise Exception("400 INVALID_ARGUMENT: malformed request")
            return type("Response", (), {"text": "ok"})()

    client = type("Client", (), {"models": Models()})()
    monkeypatch.setattr(bot, "gemini_client", client)

    text, selected = bot.execute_gemini_dynamic_request("ping")
    assert selected == "good-model"
    assert "config-error-model" not in bot.SESSION_BLACKLIST_INCOMPATIBLE


def test_404_marks_model_blacklisted_and_moves_on(monkeypatch):
    bot = get_bot(monkeypatch)
    bot.SESSION_BLACKLIST_404.clear()
    bot.SESSION_BLACKLIST_INCOMPATIBLE.clear()
    bot.MODEL_COOLDOWNS_429.clear()

    monkeypatch.setattr(bot, "prioritize_models_for_task", lambda task_type="vetting": ["missing-model", "good-model"])

    class Models:
        def generate_content(self, model, contents, config):
            if model == "missing-model":
                raise Exception("404 NOT_FOUND")
            return type("Response", (), {"text": "ok"})()

    client = type("Client", (), {"models": Models()})()
    monkeypatch.setattr(bot, "gemini_client", client)

    text, selected = bot.execute_gemini_dynamic_request("ping")
    assert selected == "good-model"
    assert "missing-model" in bot.SESSION_BLACKLIST_404


def test_429_cools_down_model_without_retrying_same_model(monkeypatch):
    bot = get_bot(monkeypatch)
    bot.SESSION_BLACKLIST_404.clear()
    bot.SESSION_BLACKLIST_INCOMPATIBLE.clear()
    bot.MODEL_COOLDOWNS_429.clear()

    monkeypatch.setattr(bot, "prioritize_models_for_task", lambda task_type="vetting": ["limited-model", "good-model"])
    calls = []

    class Models:
        def generate_content(self, model, contents, config):
            calls.append(model)
            if model == "limited-model":
                raise Exception("429 RESOURCE_EXHAUSTED")
            return type("Response", (), {"text": "ok"})()

    client = type("Client", (), {"models": Models()})()
    monkeypatch.setattr(bot, "gemini_client", client)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    text, selected = bot.execute_gemini_dynamic_request("ping")
    assert selected == "good-model"
    assert calls == ["limited-model", "good-model"]
    assert bot.MODEL_COOLDOWNS_429["limited-model"] > time.monotonic()


def test_priority_returns_no_candidate_when_all_compatible_models_are_on_cooldown(monkeypatch):
    bot = get_bot(monkeypatch)
    now = time.monotonic()
    bot.SESSION_BLACKLIST_404.clear()
    bot.SESSION_BLACKLIST_INCOMPATIBLE.clear()
    bot.MODEL_COOLDOWNS_429.clear()
    bot.MODEL_COOLDOWNS_429.update({"gemini-3.5-flash": now + 60, "gemini-2.5-pro": now + 60})

    monkeypatch.setattr(
        bot,
        "discover_available_models",
        lambda force_refresh=False: ["gemini-3.5-flash", "gemini-2.5-pro"],
    )
    assert bot.prioritize_models_for_task() == []

# CI trigger marker: quota-aware routing patch must push its generated commit back to the PR branch.
