"""Tests for search_groups — Soulseek results grouped into song versions
(one row per version, its copies underneath) and ranked so the version you
most likely want is on top."""

from search_groups import VersionIndex, owned_stems
from soulseek import SoulseekFile


def _f(path, user="u", size=8_000_000, bitrate=320, length=200, free=True, queue=0, speed=1_000_000):
    return SoulseekFile(user, path, size, bitrate, length, queue, free, speed)


def test_copies_with_the_same_name_and_length_form_one_version():
    index = VersionIndex("sabrina carpenter espresso")
    index.add([
        _f("Music\\Sabrina Carpenter - Espresso.mp3", user="a", length=175),
        _f("share/pop/Sabrina Carpenter - Espresso.mp3", user="b", length=176),
        _f("x\\03 - Sabrina Carpenter - Espresso.flac", user="c", bitrate=None, length=175, size=25_000_000),
    ])

    [version] = index.ranked()

    assert len(version.copies) == 3
    assert version.title == "Sabrina Carpenter - Espresso"


def test_same_name_but_different_length_is_a_different_version():
    index = VersionIndex("espresso")
    index.add([_f("a\\Espresso.mp3", length=175), _f("b\\Espresso.mp3", user="b", length=230)])

    assert len(index.ranked()) == 2


def test_versions_you_did_not_ask_for_drop_below_the_original():
    index = VersionIndex("sabrina carpenter espresso")
    karaoke = [_f(f"k{i}\\Sabrina Carpenter - Espresso (Karaoke Version).mp3", user=f"k{i}") for i in range(5)]
    original = [_f("o\\Sabrina Carpenter - Espresso.mp3", user="o")]
    index.add(karaoke + original)

    ranked = index.ranked()

    assert ranked[0].title == "Sabrina Carpenter - Espresso"
    assert ranked[1].unwanted


def test_asking_for_a_remix_does_not_demote_remixes():
    index = VersionIndex("espresso remix")
    index.add([_f("a\\Espresso (Remix).mp3")])

    assert not index.ranked()[0].unwanted


def test_versions_whose_name_has_every_query_word_come_first():
    index = VersionIndex("rosalia malamente")
    index.add([
        _f("Rosalía\\El Mal Querer\\01 - Malamente.mp3", user="a"),       # artist only in folder
        _f("x\\Rosalía - Malamente.mp3", user="b", length=150),
    ])

    assert index.ranked()[0].title == "Rosalía - Malamente"


def test_more_copies_rank_higher_among_good_matches():
    index = VersionIndex("queen bohemian rhapsody")
    one = [_f("a\\Queen - Bohemian Rhapsody.mp3", user="a", length=354)]
    many = [_f(f"b{i}\\Queen - Bohemian Rhapsody (Remastered 2011).mp3", user=f"b{i}", length=355) for i in range(4)]
    index.add(one + many)

    assert len(index.ranked()[0].copies) == 4


def test_best_copy_prefers_free_slot_and_good_quality():
    index = VersionIndex("x")
    slow = _f("a\\X.mp3", user="slow", free=False, queue=40)
    low = _f("b\\X.mp3", user="low", bitrate=96)
    good = _f("c\\X.mp3", user="good")
    index.add([slow, low, good])

    [version] = index.ranked()

    assert version.best is good
    assert index.alternatives(good)[0] is not good
    assert {f.username for f in index.alternatives(good)} == {"slow", "low"}


def test_filter_hides_versions_without_a_matching_copy():
    index = VersionIndex("espresso")
    index.add([_f("pop\\Espresso.mp3", user="a"), _f("jazz\\Espresso Macchiato.flac", user="b", bitrate=None)])

    index.set_filter("-macchiato", lossless_only=False)
    assert [v.title for v in index.ranked()] == ["Espresso"]

    index.set_filter("", lossless_only=True)
    assert [v.title for v in index.ranked()] == ["Espresso Macchiato"]


def test_frozen_order_keeps_existing_rows_in_place_and_appends_new_ones():
    index = VersionIndex("song")
    index.add([_f("a\\Song.mp3", user="a")])
    first = [v.id for v in index.ranked()]
    index.freeze_order()

    index.add([_f(f"b{i}\\Song (Live).mp3", user=f"b{i}", length=260) for i in range(3)]
              + [_f(f"c{i}\\Song.mp3", user=f"c{i}", length=330) for i in range(6)])

    ids = [v.id for v in index.ranked()]
    assert ids[0] == first[0]
    assert len(ids) == 3


def test_owned_stems_mark_versions_already_in_the_library():
    owned = owned_stems(["/m/Rosalía - Malamente.opus", "/m/Queen - Bohemian Rhapsody.mp3"])
    index = VersionIndex("malamente", owned=owned)
    index.add([
        _f("Rosalía\\El Mal Querer\\01 - Malamente.mp3", user="a"),
        _f("x\\Rosalia - Malamente.flac", user="b", bitrate=None, length=151),
        _f("y\\Malamente (Live).mp3", user="c", length=240),
    ])

    owned_titles = {v.title for v in index.ranked() if v.in_library}

    assert owned_titles == {"Malamente", "Rosalia - Malamente"}


def test_a_bare_track_number_does_not_split_a_version_but_artist_names_survive():
    index = VersionIndex("espresso")
    index.add([
        _f("a\\03 Sabrina Carpenter - Espresso.mp3", user="a"),
        _f("b\\Sabrina Carpenter - Espresso.mp3", user="b"),
    ])
    [version] = index.ranked()
    assert len(version.copies) == 2

    other = VersionIndex("in da club")
    other.add([_f("c\\50 Cent - In Da Club.mp3")])
    assert other.ranked()[0].title == "50 Cent - In Da Club"
