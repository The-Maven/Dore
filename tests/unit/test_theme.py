from sca import theme


def test_all_icons_resolve():
    for name in theme.ICONS:
        assert theme.icon(name)
    assert theme.icon("unknown-name")  # falls back, no crash


def test_paint_is_plaintext_when_disabled(monkeypatch):
    monkeypatch.setattr(theme, "_enabled", lambda: False)
    assert theme.paint("hello", theme.GOLD) == "hello"
    assert theme.on_gold("RS") == "RS"


def test_coverage_badge_buckets(monkeypatch):
    monkeypatch.setattr(theme, "_enabled", lambda: False)
    assert "100.00%" in theme.coverage_badge(1.0)
    assert "98.50%" in theme.coverage_badge(0.985)
    assert "n/a" in theme.coverage_badge(None)


def test_kv_contains_label_and_value(monkeypatch):
    monkeypatch.setattr(theme, "_enabled", lambda: False)
    row = theme.kv("coverage", "100%")
    assert "coverage" in row and "100%" in row
