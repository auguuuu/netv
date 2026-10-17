"""Tests for main.py - FastAPI routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import json

import pytest

import cache as cache_module
import m3u as m3u_module


@pytest.fixture
def mock_deps():
    """Mock all external dependencies before importing main."""
    with (
        patch.dict(
            "sys.modules", {"defusedxml": MagicMock(), "defusedxml.ElementTree": MagicMock()}
        ),
        patch("cache.CACHE_DIR", Path("/tmp/test_cache")),
        patch("cache.SERVER_SETTINGS_FILE", Path("/tmp/test_cache/server_settings.json")),
        patch("cache.USERS_DIR", Path("/tmp/test_cache/users")),
    ):
        yield


@pytest.fixture
def client(tmp_path: Path, mock_deps):
    """Create test client with mocked dependencies."""
    from fastapi.testclient import TestClient

    # Patch paths before importing main
    with (
        patch("cache.CACHE_DIR", tmp_path),
        patch("cache.SERVER_SETTINGS_FILE", tmp_path / "server_settings.json"),
        patch("cache.USERS_DIR", tmp_path / "users"),
        patch("auth.CACHE_DIR", tmp_path),
        patch("auth.SERVER_SETTINGS_FILE", tmp_path / "server_settings.json"),
        patch("auth.USERS_DIR", tmp_path / "users"),
        patch("epg.init"),
        patch("ffmpeg_command.init"),
        patch("ffmpeg_session.cleanup_and_recover_sessions"),
    ):
        (tmp_path / "users").mkdir(exist_ok=True)
        import main

        # Disable background loading
        cache_module.get_cache().clear()
        yield TestClient(main.app)


@pytest.fixture
def auth_client(tmp_path: Path, mock_deps):
    """Create test client with a logged-in user."""
    from fastapi.testclient import TestClient

    with (
        patch("cache.CACHE_DIR", tmp_path),
        patch("cache.SERVER_SETTINGS_FILE", tmp_path / "server_settings.json"),
        patch("cache.USERS_DIR", tmp_path / "users"),
        patch("auth.CACHE_DIR", tmp_path),
        patch("auth.SERVER_SETTINGS_FILE", tmp_path / "server_settings.json"),
        patch("auth.USERS_DIR", tmp_path / "users"),
        patch("epg.init"),
        patch("ffmpeg_command.init"),
        patch("ffmpeg_session.cleanup_and_recover_sessions"),
    ):
        (tmp_path / "users").mkdir(exist_ok=True)
        import auth
        import main

        cache_module.get_cache().clear()
        client = TestClient(main.app)

        # Create user and get token
        auth.create_user("testuser", "testpass123")
        token = auth.create_token({"sub": "testuser"})
        client.cookies.set("token", token)

        yield client


def test_web_player_includes_resolution_badge(auth_client):
    from main import PlayerInfo

    info = PlayerInfo(url="https://example.test/live.m3u8", channel_name="Test channel")
    with patch("main._get_live_player_info", return_value=info):
        response = auth_client.get("/play/live/test")
    assert response.status_code == 200
    assert 'id="quality-badge"' in response.text
    assert response.text.index('id="player-container"') < response.text.index('id="quality-badge"')
    assert response.text.index("/static/js/player-quality.js") < response.text.index(
        "/static/js/player.js"
    )
    assert response.text.index("/static/js/live-playback.js") < response.text.index(
        "/static/js/player.js"
    )
    script = auth_client.get("/static/js/player-quality.js")
    assert script.status_code == 200
    assert "videoWidth" in script.text


def test_live_dvr_routes_auto_mode_through_server(auth_client):
    from main import PlayerInfo

    info = PlayerInfo(url="https://example.test/live.ts", channel_name="Test channel")
    with patch("main._get_live_player_info", return_value=info):
        response = auth_client.get("/play/live/test")
    assert response.status_code == 200
    assert 'transcodeMode: "always"' in response.text
    assert 'liveDvrMins: 60' in response.text


def test_live_dvr_disabled_keeps_direct_auto_mode(auth_client):
    from main import PlayerInfo

    settings = cache_module.load_server_settings()
    settings["live_dvr_mins"] = 0
    cache_module.save_server_settings(settings)
    info = PlayerInfo(url="https://example.test/live.ts", channel_name="Test channel")
    with patch("main._get_live_player_info", return_value=info):
        response = auth_client.get("/play/live/test")
    assert response.status_code == 200
    assert 'transcodeMode: "auto"' in response.text
    assert 'liveDvrMins: 0' in response.text


def test_live_player_passes_program_window_from_epg(auth_client):
    from main import PlayerInfo

    info = PlayerInfo(
        url="https://example.test/live.ts",
        channel_name="Test channel",
        program_title="News",
        program_start=1749998200.0,
        program_end=1750000000.0,
    )
    with patch("main._get_live_player_info", return_value=info):
        response = auth_client.get("/play/live/test")
    assert response.status_code == 200
    assert "programStart: 1749998200.0" in response.text
    assert "programEnd: 1750000000.0" in response.text
    assert 'streamId: "test"' in response.text
    assert 'id="program-remaining"' not in response.text


def test_live_player_info_reads_program_window_from_epg():
    from datetime import UTC, datetime, timedelta

    from epg import Program

    import main

    start = datetime.now(UTC) - timedelta(minutes=30)
    stop = datetime.now(UTC) + timedelta(minutes=30)
    program = Program("ch1", "News", start, stop)
    stream = {
        "stream_id": "1",
        "name": "Channel",
        "direct_url": "https://example.test/live.ts",
        "epg_channel_id": "ch1",
    }
    with (
        patch("main._ensure_live_cache"),
        patch("main.get_cache", return_value={"live_streams": [stream]}),
        patch("epg.get_programs_in_range", return_value=[program]),
    ):
        info = main._get_live_player_info("1")
    assert info.program_title == "News"
    assert info.program_start == start.timestamp()
    assert info.program_end == stop.timestamp()


def test_live_program_api_returns_current_program(auth_client):
    from datetime import UTC, datetime, timedelta

    from epg import Program

    start = datetime.now(UTC) - timedelta(minutes=10)
    stop = start + timedelta(minutes=45)
    program = Program("ch1", "News", start, stop, desc="Tonight's headlines")
    stream = {"stream_id": "1", "name": "Channel", "epg_channel_id": "ch1"}
    with (
        patch("main._ensure_live_cache"),
        patch("main.get_cache", return_value={"live_streams": [stream]}),
        patch("epg.get_programs_in_range", return_value=[program]),
    ):
        response = auth_client.get("/api/live/program/1")
    assert response.status_code == 200
    assert response.json() == {
        "title": "News",
        "desc": "Tonight's headlines",
        "start": start.timestamp(),
        "end": stop.timestamp(),
    }


def test_live_program_api_without_guide_data_returns_empty_program(auth_client):
    stream = {"stream_id": "1", "name": "Channel"}
    with (
        patch("main._ensure_live_cache"),
        patch("main.get_cache", return_value={"live_streams": [stream]}),
    ):
        response = auth_client.get("/api/live/program/1")
    assert response.status_code == 200
    assert response.json() == {"title": "", "desc": "", "start": 0.0, "end": 0.0}


def test_live_program_api_unknown_stream_is_not_found(auth_client):
    with (
        patch("main._ensure_live_cache"),
        patch("main.get_cache", return_value={"live_streams": []}),
    ):
        response = auth_client.get("/api/live/program/missing")
    assert response.status_code == 404


def test_adaptive_start_uses_shared_backend(auth_client):
    result = {
        "session_id": "shared",
        "playlist": "/transcode/shared/low.m3u8",
        "master_playlist": "/transcode/shared/master.m3u8",
    }
    with patch("ffmpeg_session.start_transcode", new=AsyncMock(return_value=result)) as start:
        response = auth_client.get(
            "/transcode/start",
            params={"url": "https://provider.example/live.m3u8", "fast_start": "true"},
        )
    assert response.status_code == 200
    assert response.json() == result
    assert start.call_args.kwargs["fast_start"] is True


def test_disconnected_adaptive_start_is_cleaned_up(auth_client):
    with (
        patch("ffmpeg_session.start_transcode", new=AsyncMock(return_value={"session_id": "new"})),
        patch("starlette.requests.Request.is_disconnected", new=AsyncMock(return_value=True)),
        patch("ffmpeg_session.stop_session") as stop,
    ):
        response = auth_client.get(
            "/transcode/start",
            params={"url": "https://provider.example/live.m3u8", "fast_start": "true"},
        )
    assert response.status_code == 499
    stop.assert_called_once_with("new", force=True)


@pytest.mark.parametrize("owner,status", [("testuser", 200), ("other", 404)])
def test_live_page_close_force_stop_checks_owner(auth_client, owner, status):
    with (
        patch("ffmpeg_session.get_session", return_value={"username": owner}),
        patch("ffmpeg_session.stop_session") as stop,
    ):
        response = auth_client.post("/transcode/live/stop?force=true")
    assert response.status_code == status
    if status == 200:
        stop.assert_called_once_with("live", force=True)
    else:
        stop.assert_not_called()


class TestSetup:
    """Tests for initial setup flow."""

    def test_setup_page_shown_when_no_users(self, client):
        resp = client.get("/setup", follow_redirects=False)
        assert resp.status_code == 200
        assert b"setup" in resp.content.lower() or b"Create" in resp.content

    def test_setup_redirects_when_users_exist(self, client, tmp_path):
        import auth

        auth.create_user("admin", "password123")

        resp = client.get("/setup", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

    def test_setup_creates_user(self, client):
        resp = client.post(
            "/setup",
            data={"username": "admin", "password": "password123", "confirm": "password123"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

        import auth

        assert auth.verify_password("admin", "password123")

    def test_setup_validates_username_length(self, client):
        resp = client.post(
            "/setup",
            data={"username": "ab", "password": "password123", "confirm": "password123"},
        )
        assert resp.status_code == 200
        assert b"at least 3" in resp.content

    def test_setup_validates_password_length(self, client):
        resp = client.post(
            "/setup",
            data={"username": "admin", "password": "short", "confirm": "short"},
        )
        assert resp.status_code == 200
        assert b"at least 8" in resp.content

    def test_setup_validates_password_match(self, client):
        resp = client.post(
            "/setup",
            data={"username": "admin", "password": "password123", "confirm": "different"},
        )
        assert resp.status_code == 200
        assert b"do not match" in resp.content


class TestLogin:
    """Tests for login flow."""

    def test_login_page_redirects_to_setup_when_no_users(self, client):
        resp = client.get("/login", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/setup"

    def test_login_page_shown_when_users_exist(self, client, tmp_path):
        import auth

        auth.create_user("admin", "password123")

        resp = client.get("/login")
        assert resp.status_code == 200

    def test_login_success_sets_cookie(self, client, tmp_path):
        import auth

        auth.create_user("admin", "password123")

        resp = client.post(
            "/login",
            data={"username": "admin", "password": "password123"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "token" in resp.cookies

    def test_login_failure_returns_401(self, client, tmp_path):
        import auth

        auth.create_user("admin", "password123")

        resp = client.post(
            "/login",
            data={"username": "admin", "password": "wrongpassword"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=invalid" in resp.headers["location"]


class TestLogout:
    """Tests for logout."""

    def test_logout_clears_cookie(self, auth_client):
        resp = auth_client.get("/logout", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"


class TestAuthRequired:
    """Tests for auth-protected routes."""

    def test_index_redirects_to_login(self, client, tmp_path):
        import auth

        auth.create_user("admin", "password123")

        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

    def test_guide_redirects_to_login(self, client, tmp_path):
        import auth

        auth.create_user("admin", "password123")

        resp = client.get("/guide", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

    def test_vod_redirects_to_login(self, client, tmp_path):
        import auth

        auth.create_user("admin", "password123")

        resp = client.get("/vod", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

    def test_series_redirects_to_login(self, client, tmp_path):
        import auth

        auth.create_user("admin", "password123")

        resp = client.get("/series", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"


class TestIndex:
    """Tests for index route."""

    def test_index_redirects_to_guide(self, auth_client):
        resp = auth_client.get("/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/guide"


class TestFavicon:
    """Tests for favicon."""

    def test_favicon_is_served_without_login(self, client):
        resp = client.get("/favicon.ico")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/svg+xml")
        assert b"<svg" in resp.content


class TestOfflineAssets:
    """Pages must work with no internet: scripts are vendored, only Cast stays remote (async)."""

    def test_templates_load_no_remote_script_but_cast(self):
        import re

        root = Path(__file__).parent
        for tpl in (root / "templates").glob("*.html"):
            for tag in re.findall(r"<script[^>]*\bsrc=\"https?://[^>]*>", tpl.read_text()):
                assert "gstatic.com/cv/js/sender" in tag, f"{tpl.name}: {tag}"
                assert re.search(r"\basync\b", tag), f"{tpl.name}: Cast must load async"

    def test_vendored_scripts_are_served(self, client):
        for name in ("tailwindcss-3.4.17.js", "htmx-2.0.4.min.js", "hls-1.7.3.min.js"):
            resp = client.get(f"/static/vendor/{name}")
            assert resp.status_code == 200, name
            assert len(resp.content) > 10_000, name


class TestGuide:
    """Tests for guide page."""

    def test_guide_shows_loading_when_no_cache(self, auth_client):
        with patch("main.load_file_cache", return_value=None):
            resp = auth_client.get("/guide")
            assert resp.status_code == 200
            # Should show loading state
            assert b"loading" in resp.content.lower() or b"Loading" in resp.content

    def test_guide_shows_channels_from_cache(self, auth_client):
        cache_module.get_cache()["live_categories"] = [
            {"category_id": "1", "category_name": "News"}
        ]
        cache_module.get_cache()["live_streams"] = [
            {"stream_id": 1, "name": "CNN", "category_ids": ["1"], "epg_channel_id": ""}
        ]

        with patch("main.epg.has_programs", return_value=True):
            resp = auth_client.get("/guide?cats=1")
            assert resp.status_code == 200

    def test_guide_limits_initial_virtual_rows(self, auth_client):
        cache_module.get_cache()["live_categories"] = [
            {"category_id": "1", "category_name": "News"}
        ]
        cache_module.get_cache()["live_streams"] = [
            {
                "stream_id": stream_id,
                "name": f"Channel {stream_id}",
                "category_ids": ["1"],
                "epg_channel_id": "",
                "stream_icon": f"https://example.com/{stream_id}.png",
            }
            for stream_id in range(60)
        ]

        with patch("main.epg.has_programs", return_value=True):
            resp = auth_client.get("/guide?cats=1")

        assert resp.status_code == 200
        assert resp.text.count('class="guide-row') == 50
        assert resp.text.count('loading="lazy"') == 50

    def test_guide_rows_include_category_metadata(self, auth_client):
        cache_module.get_cache()["live_categories"] = [
            {"category_id": "1", "category_name": "News"}
        ]
        cache_module.get_cache()["live_streams"] = [
            {
                "stream_id": 1,
                "name": "CNN",
                "category_ids": ["1"],
                "epg_channel_id": "",
            }
        ]

        resp = auth_client.get("/api/guide/rows?cats=1")

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["categories"] == [{"category_id": "1", "category_name": "News"}]
        assert payload["rows"][0]["channel"]["category_ids"] == ["1"]

    def test_guide_rows_categories_cover_all_pages(self, auth_client):
        categories = [
            {"category_id": "1", "category_name": "News"},
            {"category_id": "2", "category_name": "Sports"},
            {"category_id": "3", "category_name": "Unavailable"},
        ]
        cache_module.get_cache()["live_categories"] = categories
        cache_module.get_cache()["live_streams"] = [
            {
                "stream_id": index,
                "name": f"Channel {index}",
                "category_ids": ["1" if index < 500 else "2"],
            }
            for index in range(501)
        ] + [{"stream_id": 999, "name": "Hidden", "category_ids": ["3"]}]

        with patch("main.auth.get_user_limits", return_value={"unavailable_groups": ["cat:3"]}):
            first = auth_client.get("/api/guide/rows?cats=1,2,3&count=500").json()
            second = auth_client.get("/api/guide/rows?cats=1,2,3&start=500&count=500").json()
            reordered = auth_client.get("/api/guide/rows?cats=2,1,3&count=1").json()

        assert first["total"] == second["total"] == 501
        assert len(first["rows"]) == 500
        assert len(second["rows"]) == 1
        assert second["rows"][0]["channel"]["stream_id"] == 500
        assert first["categories"] == second["categories"] == categories[:2]
        assert reordered["categories"] == categories[1::-1]
        assert reordered["rows"][0]["channel"]["stream_id"] == 500

    def test_guide_rows_timestamps_anchor_local_timeline(self, auth_client):
        from epg import Program

        now = datetime(2026, 9, 14, 0, 15, tzinfo=UTC)
        window_start = now.replace(minute=0)
        program_start = window_start - timedelta(minutes=30)
        program_end = window_start + timedelta(hours=1)
        cache_module.get_cache()["live_streams"] = [
            {"stream_id": 1, "name": "News", "category_ids": ["1"], "epg_channel_id": "news"}
        ]
        program = Program("news", "Overnight news", program_start, program_end)

        with (
            patch("main.datetime") as clock,
            patch("main.epg.get_icons_batch", return_value={}),
            patch("main.epg.get_programs_batch", return_value={"news": [program]}),
        ):
            clock.now.return_value = now
            response = auth_client.get("/api/guide/rows?cats=1")

        assert response.status_code == 200
        payload = response.json()
        listing = payload["rows"][0]["programs"][0]
        assert payload["window_start_timestamp"] == window_start.timestamp()
        assert listing["start_timestamp"] == program_start.timestamp()
        assert listing["end_timestamp"] == program_end.timestamp()
        assert listing["start"] == "23:30"
        assert listing["end"] == "01:00"
        assert listing["left_pct"] == 0
        assert listing["width_pct"] == pytest.approx(100 / 3)

    def test_guide_uses_saved_filter(self, auth_client, tmp_path):
        user_dir = tmp_path / "users" / "testuser"
        user_dir.mkdir(parents=True, exist_ok=True)
        (user_dir / "settings.json").write_text(json.dumps({"guide_filter": ["1", "2"]}))

        cache_module.get_cache()["live_categories"] = []
        cache_module.get_cache()["live_streams"] = []

        # Guide now renders directly using saved filter (no redirect)
        with patch("main.epg.has_programs", return_value=True):
            resp = auth_client.get("/guide")
            assert resp.status_code == 200


class TestVod:
    """Tests for VOD page."""

    def test_vod_shows_loading_when_no_cache(self, auth_client):
        with patch("main.load_file_cache", return_value=None):
            resp = auth_client.get("/vod")
            assert resp.status_code == 200

    def test_vod_shows_movies_from_cache(self, auth_client):
        cache_module.get_cache()["vod_categories"] = [
            {"category_id": "10", "category_name": "Movies", "source_id": "src1"}
        ]
        cache_module.get_cache()["vod_streams"] = [
            {"stream_id": 100, "name": "Movie 1", "category_id": "10", "source_id": "src1"}
        ]

        resp = auth_client.get("/vod")
        assert resp.status_code == 200

    def test_vod_filters_by_category(self, auth_client):
        cache_module.get_cache()["vod_categories"] = [
            {"category_id": "10", "category_name": "Action", "source_id": "src1"},
            {"category_id": "20", "category_name": "Comedy", "source_id": "src1"},
        ]
        cache_module.get_cache()["vod_streams"] = [
            {"stream_id": 100, "name": "Action Movie", "category_id": "10", "source_id": "src1"},
            {"stream_id": 101, "name": "Comedy Movie", "category_id": "20", "source_id": "src1"},
        ]

        resp = auth_client.get("/vod?category=10")
        assert resp.status_code == 200

    def test_vod_sorts_by_alpha(self, auth_client):
        cache_module.get_cache()["vod_categories"] = []
        cache_module.get_cache()["vod_streams"] = [
            {"stream_id": 1, "name": "Zebra", "source_id": "src1"},
            {"stream_id": 2, "name": "Apple", "source_id": "src1"},
        ]

        resp = auth_client.get("/vod?sort=alpha")
        assert resp.status_code == 200


class TestSeries:
    """Tests for series page."""

    def test_series_shows_loading_when_no_cache(self, auth_client):
        with patch("main.load_file_cache", return_value=None):
            resp = auth_client.get("/series")
            assert resp.status_code == 200

    def test_series_shows_list_from_cache(self, auth_client):
        cache_module.get_cache()["series_categories"] = [
            {"category_id": "30", "category_name": "Drama", "source_id": "src1"}
        ]
        cache_module.get_cache()["series"] = [
            {"series_id": 200, "name": "Show 1", "category_id": "30", "source_id": "src1"}
        ]

        resp = auth_client.get("/series")
        assert resp.status_code == 200


class TestSearch:
    """Tests for search page."""

    def test_search_page_renders(self, auth_client):
        cache_module.get_cache()["live_streams"] = []
        cache_module.get_cache()["vod_streams"] = []
        cache_module.get_cache()["series"] = []

        resp = auth_client.get("/search")
        assert resp.status_code == 200

    def test_search_finds_live_streams(self, auth_client):
        cache_module.get_cache()["live_streams"] = [
            {"stream_id": 1, "name": "CNN News"},
            {"stream_id": 2, "name": "BBC World"},
        ]
        cache_module.get_cache()["live_categories"] = []
        cache_module.get_cache()["epg_urls"] = []
        cache_module.get_cache()["vod_streams"] = []
        cache_module.get_cache()["series"] = []

        resp = auth_client.get("/search?q=CNN&live=true")
        assert resp.status_code == 200

    def test_search_regex_mode(self, auth_client):
        cache_module.get_cache()["live_streams"] = [
            {"stream_id": 1, "name": "CNN News"},
            {"stream_id": 2, "name": "CNBC Finance"},
        ]
        cache_module.get_cache()["live_categories"] = []
        cache_module.get_cache()["epg_urls"] = []
        cache_module.get_cache()["vod_streams"] = []
        cache_module.get_cache()["series"] = []

        resp = auth_client.get("/search?q=CN.*&regex=true&live=true")
        assert resp.status_code == 200

    def test_search_rejects_long_regex(self, auth_client):
        cache_module.get_cache()["live_streams"] = []

        resp = auth_client.get(f"/search?q={'a' * 101}&regex=true&live=true")
        assert resp.status_code == 400


class TestSettings:
    """Tests for settings page."""

    def test_settings_page_renders(self, auth_client):
        cache_module.get_cache()["live_categories"] = []

        with patch("main.load_file_cache", return_value=None):
            resp = auth_client.get("/settings")
            assert resp.status_code == 200

    def test_settings_guide_filter(self, auth_client):
        resp = auth_client.post(
            "/settings/guide-filter",
            json={"cats": ["1", "2", "3"]},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_settings_captions(self, auth_client):
        resp = auth_client.post(
            "/settings/captions",
            data={"enabled": "on"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_settings_transcode(self, auth_client):
        resp = auth_client.post(
            "/settings/transcode",
            data={
                "transcode_mode": "auto",
                "transcode_hw": "nvidia",
                "vod_transcode_cache_mins": 60,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


class TestAddSource:
    """Tests for adding sources."""

    def test_add_xtream_source(self, auth_client):
        with patch("main.clear_all_caches"):
            resp = auth_client.post(
                "/settings/add",
                data={
                    "name": "Test Provider",
                    "source_type": "xtream",
                    "url": "http://example.com",
                    "username": "user",
                    "password": "pass",
                    "epg_timeout": 120,
                },
                follow_redirects=False,
            )
            assert resp.status_code == 303

    def test_add_m3u_source(self, auth_client):
        with patch("main.clear_all_caches"):
            resp = auth_client.post(
                "/settings/add",
                data={
                    "name": "M3U Playlist",
                    "source_type": "m3u",
                    "url": "http://example.com/playlist.m3u",
                    "epg_timeout": 120,
                },
                follow_redirects=False,
            )
            assert resp.status_code == 303

    def test_add_source_validates_type(self, auth_client):
        resp = auth_client.post(
            "/settings/add",
            data={
                "name": "Bad Source",
                "source_type": "invalid",
                "url": "http://example.com",
            },
        )
        assert resp.status_code == 400

    def test_add_source_validates_url_scheme(self, auth_client):
        resp = auth_client.post(
            "/settings/add",
            data={
                "name": "Bad Source",
                "source_type": "xtream",
                "url": "ftp://example.com",
            },
        )
        assert resp.status_code == 400

    def test_add_source_validates_name_length(self, auth_client):
        resp = auth_client.post(
            "/settings/add",
            data={
                "name": "x" * 201,
                "source_type": "xtream",
                "url": "http://example.com",
            },
        )
        assert resp.status_code == 400


class TestDeleteSource:
    """Tests for deleting sources."""

    def test_delete_source(self, auth_client, tmp_path):
        settings_file = tmp_path / "server_settings.json"
        settings_file.write_text(
            json.dumps(
                {
                    "sources": [
                        {
                            "id": "src_123",
                            "name": "Test",
                            "type": "xtream",
                            "url": "http://example.com",
                        }
                    ]
                }
            )
        )

        with patch("main.clear_all_caches"):
            resp = auth_client.post("/settings/delete/src_123", follow_redirects=False)
            assert resp.status_code == 303


class TestUserPrefs:
    """Tests for user preferences API."""

    def test_get_user_prefs(self, auth_client):
        resp = auth_client.get("/api/user-prefs")
        assert resp.status_code == 200
        data = resp.json()
        assert "favorites" in data
        assert "cc_lang" in data

    def test_save_user_prefs(self, auth_client):
        resp = auth_client.post(
            "/api/user-prefs",
            json={"cc_lang": "eng", "cast_host": "192.168.1.100"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


class TestWatchPosition:
    """Tests for watch position API."""

    def test_save_watch_position(self, auth_client):
        resp = auth_client.post(
            "/api/watch-position",
            json={"url": "http://example.com/movie.mkv", "position": 1234.5, "duration": 7200},
        )
        assert resp.status_code == 200

    def test_get_watch_position(self, auth_client):
        # Save first
        auth_client.post(
            "/api/watch-position",
            json={"url": "http://example.com/movie.mkv", "position": 1234.5, "duration": 7200},
        )

        resp = auth_client.get("/api/watch-position?url=http://example.com/movie.mkv")
        assert resp.status_code == 200
        data = resp.json()
        assert data["position"] == 1234.5
        assert data["duration"] == 7200

    def test_get_watch_position_not_found(self, auth_client):
        resp = auth_client.get("/api/watch-position?url=http://example.com/unknown.mkv")
        assert resp.status_code == 200
        data = resp.json()
        assert data["position"] == 0


class TestUserManagement:
    """Tests for user management endpoints."""

    def test_delete_user(self, auth_client, tmp_path):
        import auth

        auth.create_user("otheruser", "password123")

        resp = auth_client.post("/settings/users/delete/otheruser", follow_redirects=False)
        assert resp.status_code == 303

    def test_cannot_delete_self(self, auth_client):
        resp = auth_client.post("/settings/users/delete/testuser")
        assert resp.status_code == 400

    def test_change_password(self, auth_client):
        resp = auth_client.post(
            "/settings/users/password",
            data={"current_password": "testpass123", "new_password": "newpass456"},
        )
        assert resp.status_code == 200

    def test_change_password_wrong_current(self, auth_client):
        resp = auth_client.post(
            "/settings/users/password",
            data={"current_password": "wrongpass", "new_password": "newpass456"},
        )
        assert resp.status_code == 400


class TestPlaylistXspf:
    """Tests for XSPF playlist generation."""

    def test_playlist_xspf(self, auth_client):
        resp = auth_client.get("/playlist.xspf?url=http://example.com/stream.m3u8")
        assert resp.status_code == 200
        assert b"<?xml" in resp.content
        assert b"http://example.com/stream.m3u8" in resp.content
        assert resp.headers["content-type"] == "application/xspf+xml"


class TestApiSettings:
    """Tests for settings API."""

    def test_get_settings(self, auth_client):
        resp = auth_client.get("/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert "transcode_mode" in data

    def test_update_settings(self, auth_client):
        resp = auth_client.post(
            "/api/settings",
            json={"transcode_mode": "always"},
        )
        assert resp.status_code == 200


class TestTranscodeRoutes:
    """Tests for transcode routes (with mocked transcoding module)."""

    def test_transcode_file_not_found(self, auth_client):
        with patch("main.ffmpeg_session.get_session", return_value=None):
            resp = auth_client.get("/transcode/invalid-session/stream.m3u8")
            assert resp.status_code == 404

    def test_transcode_stop(self, auth_client):
        with patch("main.ffmpeg_session.stop_session"):
            resp = auth_client.delete("/transcode/test-session")
            assert resp.status_code == 200
            assert resp.json()["status"] == "stopped"

    def test_transcode_stop_post(self, auth_client):
        with patch("main.ffmpeg_session.stop_session"):
            resp = auth_client.post("/transcode/test-session/stop")
            assert resp.status_code == 200

    def test_transcode_progress_not_found(self, auth_client):
        with patch("main.ffmpeg_session.get_session_progress", return_value=None):
            resp = auth_client.get("/transcode/progress/invalid-session")
            assert resp.status_code == 404

    @pytest.mark.parametrize("filename", ["master.m3u8", "low.m3u8", "high.m3u8"])
    def test_adaptive_playlists_share_dates_but_master_is_not_rewritten(
        self, auth_client, tmp_path, filename
    ):
        content = "#EXTM3U\n"
        (tmp_path / filename).write_text(content)
        session = {
            "dir": str(tmp_path), "fast_start": True, "origin_pts": 10, "origin_time": 0,
        }
        with (
            patch("ffmpeg_session.get_session", return_value=session),
            patch("main.dated_playlist", return_value=content + "#dated\n") as dates,
        ):
            response = auth_client.get(f"/transcode/shared/{filename}")
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "*"
        if filename == "master.m3u8":
            dates.assert_not_called()
            assert response.text == content
        else:
            dates.assert_called_once_with(str(tmp_path), content, 10, 0)
            assert "#dated" in response.text


class TestSubtitleRoutes:
    """Tests for subtitle routes."""

    def test_subtitle_invalid_filename(self, auth_client):
        resp = auth_client.get("/subs/session/notavtt.txt")
        assert resp.status_code == 400

    def test_subtitle_session_not_found(self, auth_client):
        with patch("main.ffmpeg_session.get_session", return_value=None):
            resp = auth_client.get("/subs/invalid-session/sub0.vtt")
            assert resp.status_code == 404


class TestProbeCache:
    """Tests for probe cache endpoints."""

    def test_get_probe_cache(self, auth_client):
        with patch("main.ffmpeg_command.get_series_probe_cache_stats", return_value=[]):
            resp = auth_client.get("/settings/probe-cache")
            assert resp.status_code == 200
            assert "series" in resp.json()

    def test_clear_probe_cache(self, auth_client):
        with patch("main.ffmpeg_command.clear_all_probe_cache", return_value=5):
            resp = auth_client.post("/settings/probe-cache/clear")
            assert resp.status_code == 200
            assert resp.json()["cleared"] == 5

    def test_clear_series_probe_cache(self, auth_client):
        with patch("main.ffmpeg_command.invalidate_series_probe_cache"):
            resp = auth_client.post("/settings/probe-cache/clear/123")
            assert resp.status_code == 200


class TestRefreshStatus:
    """Tests for refresh status endpoints."""

    def test_guide_refresh_status(self, auth_client):
        m3u_module.get_refresh_in_progress().clear()

        resp = auth_client.get("/guide/refresh-status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["live"] is False
        assert data["epg"] is False

    def test_settings_refresh_status(self, auth_client):
        m3u_module.get_refresh_in_progress().clear()

        resp = auth_client.get("/settings/refresh-status")
        assert resp.status_code == 200


if __name__ == "__main__":
    from testing import run_tests

    run_tests(__file__)


class TestPlaybackHealth:
    def test_invalid_feedback_is_rejected(self, auth_client):
        response = auth_client.post("/transcode/missing/health", json={"buffer_seconds": -1, "waiting": True})
        assert response.status_code == 422

    def test_feedback_is_passed_to_backend(self, auth_client):
        with patch("ffmpeg_session.report_playback_health", return_value={"bandwidth_saver": True}) as report:
            response = auth_client.post("/transcode/live/health", json={"buffer_seconds": 0, "waiting": True})
        assert response.json() == {"bandwidth_saver": True}
        assert report.call_args.args[:2] == ("live", "testuser")

    def test_force_stop_releases_current_stream(self, auth_client):
        with (
            patch("ffmpeg_session.get_session", return_value={"username": "testuser"}),
            patch("ffmpeg_session.stop_session") as stop,
        ):
            response = auth_client.delete("/transcode/live?force=true")
        assert response.status_code == 200
        stop.assert_called_once_with("live", force=True)

    def test_force_stop_requires_owner(self, auth_client):
        with (
            patch("ffmpeg_session.get_session", return_value={"username": "other"}),
            patch("ffmpeg_session.stop_session") as stop,
        ):
            assert auth_client.delete("/transcode/live?force=true").status_code == 404
        stop.assert_not_called()


def _seed_lang_catalog():
    cache = cache_module.get_cache()
    cache["vod_categories"] = [
        {"category_id": "1", "category_name": "FR | CINEMA", "source_id": "s"},
        {"category_id": "2", "category_name": "NL | NETFLIX", "source_id": "s"},
    ]
    cache["vod_streams"] = [
        {"name": "[FR] Avatar", "stream_id": 1, "category_id": "1", "source_id": "s"},
        {"name": "Avatar 2", "stream_id": 2, "category_id": "1", "source_id": "s"},
        {"name": "[NL] Avatar", "stream_id": 3, "category_id": "2", "source_id": "s"},
        {"name": "[UK] Avatar", "stream_id": 4, "category_id": "2", "source_id": "s"},
    ]
    cache["series_categories"] = [{"category_id": "5", "category_name": "US | X", "source_id": "s"}]
    cache["series"] = [
        {"name": "Avatar TLA", "series_id": 9, "category_id": "5", "source_id": "s"},
    ]


def test_search_language_filter(auth_client):
    _seed_lang_catalog()
    base = "/search?q=avatar&vod=true&series=true"
    all_ = auth_client.get(base).text
    assert 'name="lang"' in all_ and "Toutes" in all_
    fr = auth_client.get(base + "&lang=FR").text
    assert "/movie/1" in fr and "/movie/2" in fr and "/movie/3" not in fr
    assert "/series/9" not in fr
    en = auth_client.get(base + "&lang=ANGLAIS").text
    assert "/movie/4" in en and "/series/9" in en and "/movie/1" not in en
    assert "/movie/4" not in auth_client.get(base + "&lang=US").text
    # Unknown value is ignored (no filter)
    assert "/movie/3" in auth_client.get(base + "&lang=BOGUS").text
    # Counts in the dropdown cover the whole matched set
    assert "Français (FR) (2)" in fr


def test_search_language_filter_applies_before_limit(auth_client):
    _seed_lang_catalog()
    r = auth_client.get("/search?q=avatar&vod=true&limit=1&lang=NL").text
    assert "/movie/3" in r


def test_vod_and_series_browse_language_filter(auth_client):
    _seed_lang_catalog()
    r = auth_client.get("/vod?sort=alpha&lang=NL").text
    assert "/movie/3" in r and "/movie/1" not in r
    assert 'id="lang-select"' in r
    r = auth_client.get("/vod?sort=alpha&category=1&lang=FR").text
    assert "/movie/1" in r and "/movie/2" in r
    r = auth_client.get("/series?sort=alpha&lang=US").text
    assert "/series/9" in r
    assert "/series/9" not in auth_client.get("/series?sort=alpha&lang=FR").text
