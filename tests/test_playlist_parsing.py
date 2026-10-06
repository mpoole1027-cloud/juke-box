"""parse_playlist_id must accept the shapes a host would actually paste."""
import pytest
import spotify_client as sc

VALID = 'A' * 10 + 'b3' + 'C' * 10   # 22 chars, the Spotify id length


@pytest.mark.parametrize('raw,expected', [
    (f'https://open.spotify.com/playlist/{VALID}', VALID),
    (f'https://open.spotify.com/playlist/{VALID}?si=deadbeef', VALID),
    (f'http://open.spotify.com/playlist/{VALID}/', VALID),
    (f'spotify:playlist:{VALID}', VALID),
    (VALID, VALID),
    (f'  {VALID}  ', VALID),
])
def test_accepts_playlist_forms(raw, expected):
    assert sc.parse_playlist_id(raw) == expected


@pytest.mark.parametrize('raw', [
    '', None, 'garbage', 'https://example.com/nope',
    f'https://open.spotify.com/track/{VALID}',    # a track, not a playlist
    f'spotify:album:{VALID}',
])
def test_rejects_non_playlists(raw):
    assert sc.parse_playlist_id(raw) is None
