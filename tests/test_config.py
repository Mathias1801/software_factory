from factory.config import PROFILES_DIR, ProfileLayers, load_layers, resolve


def test_mode_overrides_default_but_user_wins():
    layers = ProfileLayers(load_layers().base, {"outputs": {"dashboard": True}})
    simple = resolve(layers, "simple")
    assert simple["outputs"]["dashboard"] is True            # user beats mode
    assert simple["stages"]["deploy"]["enabled"] is False    # mode beats default
    assert simple["mode"] == "simple"


def test_auto_mode_leaves_defaults():
    profile = resolve(load_layers(), None)
    assert profile["mode"] == "auto" and profile["outputs"]["dashboard"] is True


def test_profile_can_force_mode():
    assert load_layers(PROFILES_DIR / "internal_tool.yaml").mode() == "simple"
    assert load_layers(PROFILES_DIR / "internal_tool.yaml").mode("fullstack") == "fullstack"
