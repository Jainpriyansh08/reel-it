import pytest
from PIL import Image

from reel.agents import editor, packager
from reel.media import cover
from reel.util import edit_dir, read_json, write_json

from .fixtures.editor.jobkit import build_job, job


@pytest.fixture
def rendered(tmp_path, cfg, conn):
    cfg.reel["prerender_alternates"] = 0
    cfg.brand["banned_words"] = ["crypto"]
    j = build_job(tmp_path, conn, n_clips=1, pick_extra={
        "hashtags": ["#kaizen", "#CryptoTips", "habits", "#a", "#b", "#c", "#d", "#e", "#f",
                     "#g", "#h", "#kaizen"]})
    editor.scouted_step(conn, job(conn, j["id"]), cfg)
    return j


def test_clean_hashtags():
    tags = packager.clean_hashtags(["#Good", "#BADword", "plain", "#good", "#"], ["badword"])
    assert tags == ["#Good", "#plain"]
    assert len(packager.clean_hashtags([f"#t{i}" for i in range(20)], [])) == 8


def test_package_writes_cover_and_json(rendered, cfg):
    pkg = packager.package(rendered["workdir"], 1, cfg)
    out = edit_dir(rendered["workdir"], 1)
    assert read_json(out / "package.json") == pkg
    assert pkg["caption_version"] == 1 and pkg["source"] == "local_draft"
    assert not any("crypto" in t.lower() for t in pkg["hashtags"])
    assert pkg["hashtags"][:2] == ["#kaizen", "#habits"] and len(pkg["hashtags"]) == 8
    assert pkg["full_caption"] == pkg["caption"] + "\n\n" + " ".join(pkg["hashtags"])
    assert pkg["hook"] == "Small wins daily"
    assert pkg["cover"] == str((out / "cover.jpg").resolve())
    img = Image.open(out / "cover.jpg")
    assert img.format == "JPEG" and img.size == (1080, 1920)


def test_package_never_overwrites_edited_caption(rendered, cfg):
    out = edit_dir(rendered["workdir"], 1)
    packager.package(rendered["workdir"], 1, cfg)
    pkg = read_json(out / "package.json")
    pkg.update(caption="my edit", caption_version=2, full_caption="my edit")
    write_json(out / "package.json", pkg)
    (out / "cover.jpg").unlink()
    again = packager.package(rendered["workdir"], 1, cfg)
    assert again["caption"] == "my edit" and again["caption_version"] == 2
    assert read_json(out / "package.json")["caption_version"] == 2
    assert (out / "cover.jpg").exists()          # missing cover is still rebuilt


def test_font_fallback():
    assert cover.find_font("NoSuchFontXyz") is None
    font = cover.load_font("NoSuchFontXyz", 80)
    assert font is not None
    img = cover.draw_hook(Image.new("RGB", (1080, 1920)), "a fairly long hook that has to wrap",
                          "NoSuchFontXyz")
    # text stays out of the top 250 px and the right 140 px
    px = img.load()
    assert all(px[x, y] == (0, 0, 0) for y in range(0, 250, 5) for x in range(0, 1080, 5))
    assert all(px[x, y] == (0, 0, 0) for y in range(250, 1000, 5) for x in range(941, 1080, 3))
