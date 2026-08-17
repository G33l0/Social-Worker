"""Mock-site behaviour tests (no browser required)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mock_site.test_site import OPTIONS, STATE, app


@pytest.fixture
def client():
    """A test client against a freshly seeded mock site."""
    STATE.reset()
    OPTIONS.rate_limit_after = 0
    OPTIONS.latency_ms = 0
    OPTIONS.error_rate = 0.0
    OPTIONS.require_identity = True
    with TestClient(app) as test_client:
        yield test_client


def test_landing_page_offers_avatars_usernames_and_forums(client):
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert body.count('data-testid="avatar-option"') == 6
    assert body.count('data-testid="username-option"') == 5
    assert body.count('data-testid="forum-link"') == 4
    assert 'data-testid="enter-site"' in body


def test_identity_is_supplied_by_the_site(client):
    response = client.post("/identity/username", data={"username": "QuietPine"},
                           follow_redirects=False)
    assert response.status_code == 303
    assert "sw_username" in response.cookies
    client.post("/identity/avatar", data={"avatar": "avatar-2"})
    landing = client.get("/")
    assert "QuietPine" in landing.text


def test_forum_lists_initial_posts(client):
    response = client.get("/forum/general")
    assert response.status_code == 200
    assert response.text.count('data-testid="post-item"') >= 3
    assert 'data-testid="post-list"' in response.text


def test_composer_requires_an_identity(client):
    anonymous = client.get("/forum/general")
    assert 'data-testid="new-post-body"' not in anonymous.text

    client.post("/identity/username", data={"username": "QuietPine"})
    identified = client.get("/forum/general")
    assert 'data-testid="new-post-body"' in identified.text


def test_creating_a_post_and_a_reply(client):
    client.post("/identity/username", data={"username": "QuietPine"})
    client.post("/identity/avatar", data={"avatar": "avatar-2"})

    created = client.post("/forum/questions/post",
                          data={"title": "Load test", "body": "Synthetic post"})
    assert created.status_code == 200
    assert 'data-testid="post-created"' in created.text

    posts = STATE.by_forum("questions")
    assert posts[0].title == "Load test"

    replied = client.post(f"/post/{posts[0].post_id}/reply",
                          data={"reply": "Synthetic reply"})
    assert replied.status_code == 200
    assert 'data-testid="reply-created"' in replied.text
    assert STATE.posts[posts[0].post_id].replies[0]["body"] == "Synthetic reply"


def test_rate_limit_switch_returns_429(client):
    client.post("/identity/username", data={"username": "QuietPine"})
    OPTIONS.rate_limit_after = 1
    first = client.post("/forum/general/post", data={"body": "one"})
    assert first.status_code == 200
    second = client.post("/forum/general/post", data={"body": "two"})
    assert second.status_code == 429
    assert 'data-testid="rate-limited"' in second.text
    assert second.headers.get("Retry-After") == "30"


def test_error_injection_switch(client):
    client.post("/identity/username", data={"username": "QuietPine"})
    OPTIONS.error_rate = 1.0
    response = client.post("/forum/general/post", data={"body": "boom"})
    assert response.status_code == 500
    assert 'data-testid="error"' in response.text


def test_stats_and_reset_endpoints(client):
    client.post("/identity/username", data={"username": "QuietPine"})
    client.post("/forum/general/post", data={"body": "counted"})
    stats = client.get("/api/stats").json()
    assert stats["created_posts"] == 1
    assert stats["write_requests"] == 1

    reset = client.post("/api/reset").json()
    assert reset["created_posts"] == 0
    assert client.get("/healthz").json() == {"status": "ok"}


def test_unknown_forum_and_post_are_handled(client):
    assert 'data-testid="error"' in client.get("/forum/nope").text
    assert 'data-testid="error"' in client.get("/post/P99999").text


def test_default_selectors_match_the_mock_site(client):
    """The shipped selector catalogue must work against the mock site."""
    from app.browser.selectors import SelectorSet

    selectors = SelectorSet()
    landing = client.get("/").text
    for path in ("identity.avatar_options", "identity.username_options",
                 "identity.enter_button", "forums.forum_links"):
        first = selectors.candidates(path)[0]
        attribute = first.split("'")[1] if "'" in first else first
        assert attribute in landing, f"{path} not found in the landing page"

    client.post("/identity/username", data={"username": "QuietPine"})
    forum = client.get("/forum/general").text
    for path in ("forums.forum_ready", "posts.post_items", "posts.new_post_input",
                 "posts.new_post_submit"):
        first = selectors.candidates(path)[0]
        attribute = first.split("'")[1] if "'" in first else first
        assert attribute in forum, f"{path} not found in the forum page"

    post_id = STATE.by_forum("general")[0].post_id
    detail = client.get(f"/post/{post_id}").text
    for path in ("replies.reply_input", "replies.reply_submit"):
        first = selectors.candidates(path)[0]
        attribute = first.split("'")[1] if "'" in first else first
        assert attribute in detail, f"{path} not found in the post page"
