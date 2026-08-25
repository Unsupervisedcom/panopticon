"""Developer-command regression tests."""

from pathlib import Path

from panopticon.sessionservice.images import VERSION_LABEL


def test_make_build_stamps_checkout_wheel_with_package_version() -> None:
    makefile = (Path(__file__).parents[1] / "Makefile").read_text()
    build_recipe = makefile.split("build:  ##", 1)[1].split("\nclean:  ##", 1)[0]

    assert "uv build --wheel" in build_recipe
    assert "PANOPTICON_WHEEL=$$wheel" in build_recipe
    assert "version=$$(uv run python" in build_recipe
    assert "print(panopticon.__version__)" in build_recipe
    assert f"--label {VERSION_LABEL}=$$version" in build_recipe
