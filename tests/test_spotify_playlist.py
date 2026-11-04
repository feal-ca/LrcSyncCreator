"""Tests for lrc_studio/spotify_playlist.py's is_spotify_link() — routes
the unified Find Music input between a direct Soulseek search and a
Spotify tracklist import.
"""

from spotify_playlist import is_spotify_link


def test_recognizes_a_track_link():
    assert is_spotify_link("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC") is True


def test_recognizes_a_playlist_uri():
    assert is_spotify_link("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M") is True


def test_rejects_plain_search_text():
    assert is_spotify_link("Bohemian Rhapsody Queen") is False


def test_rejects_a_malformed_partial_link():
    assert is_spotify_link("spotify.com/track/") is False


def test_rejects_text_that_merely_mentions_spotify():
    assert is_spotify_link("I found this on spotify somewhere") is False


def test_rejects_empty_string():
    assert is_spotify_link("") is False
