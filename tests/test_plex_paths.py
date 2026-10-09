from ripaudit.integrations.plex import map_plex_path, parse_guids, parse_movies, validate_mappings

M = [{"plex": "/data", "container": "/media"}, {"plex": "/data/Movies4K", "container": "/media4k"}]


def test_longest_prefix_wins():
    assert map_plex_path("/data/Movies4K/A/A.mkv", M) == "/media4k/A/A.mkv"
    assert map_plex_path("/data/Movies/A.mkv", M) == "/media/Movies/A.mkv"


def test_prefix_must_match_whole_segment():
    assert map_plex_path("/database/A.mkv", M) is None


def test_windows_paths():
    mapping = [{"plex": "M:/Media", "container": "/media"}]
    assert map_plex_path(r"M:\Media\Movies\A.mkv", mapping) == "/media/Movies/A.mkv"


def test_traversal_rejected():
    assert map_plex_path("/data/../etc/passwd", M) is None


def test_validate_mappings():
    assert validate_mappings(M, ["/media", "/media4k"]) == []
    assert validate_mappings([{"plex": "/data", "container": "/etc"}], ["/media"])
    assert validate_mappings([{"plex": "", "container": "/media"}], ["/media"])


def test_guid_parsing_new_and_legacy():
    assert parse_guids({"Guid": [{"id": "imdb://tt0113277"}, {"id": "tmdb://949"}]})[1:] == (949, "tt0113277")
    assert parse_guids({"guid": "com.plexapp.agents.imdb://tt0103639?lang=en"})[1:] == (None, "tt0103639")


def test_parse_movies_versions_and_parts():
    data = {"MediaContainer": {"Metadata": [{"ratingKey": "5", "type": "movie", "title": "T", "year": 2000,
                                             "editionTitle": "Director's Cut", "Media": [
        {"id": 1, "Part": [{"id": 11, "file": "/data/a.mkv"}, {"id": 12, "file": "/data/b.mkv"}]},
        {"id": 2, "Part": [{"id": 21, "file": "/data/c.mkv"}]}]}]}}
    vs = parse_movies(data, "Movies")
    assert len(vs) == 2 and vs[0].edition == "Director's Cut"
    assert [(p.index, p.count) for p in vs[0].parts] == [(1, 2), (2, 2)]
