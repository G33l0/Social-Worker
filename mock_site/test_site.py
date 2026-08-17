"""Local mock website for exercising Social Worker end to end.

The mock reproduces the shape of an anonymous/temporary-identity forum:

* the site hands the visitor a set of avatars and temporary usernames
* the visitor picks one of each (never invents their own)
* several forums are available
* each forum lists initial posts and offers a composer
* each post page lists replies and offers a reply form

Run it with::

    python mock_site/test_site.py --port 8099

Optional switches make it easy to exercise the failure paths:

    --rate-limit-after 25   answer HTTP 429 after N write requests
    --latency-ms 120        add artificial server latency
    --error-rate 0.05       fail a fraction of write requests with HTTP 500
"""

from __future__ import annotations

import argparse
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

AVATARS = [
    ("avatar-1", "Fox"), ("avatar-2", "Otter"), ("avatar-3", "Heron"),
    ("avatar-4", "Moth"), ("avatar-5", "Ibis"), ("avatar-6", "Lynx"),
]

USERNAME_POOL = [
    "Anonymous_17", "QuietPine", "SilentHarbor", "PaleLantern", "DriftingKite",
    "NorthSignal", "AmberFrost", "HollowTide", "GlassMeadow", "SlowRiver",
]

FORUMS = [
    ("general", "General"),
    ("confessions", "Confessions"),
    ("questions", "Questions"),
    ("random", "Random"),
]

SEED_POSTS = [
    ("general", "Welcome to the general forum", "Say hello and introduce yourself."),
    ("general", "Weekly open thread", "Anything goes in this thread."),
    ("general", "What are you reading?", "Share what has your attention this week."),
    ("confessions", "I still use paper maps", "Something about them just works better."),
    ("confessions", "I have never seen the sea", "Living inland has its own charm."),
    ("questions", "How do you stay focused?", "Looking for practical routines."),
    ("questions", "Best way to learn a language?", "Immersion or structured study?"),
    ("random", "Post a picture of the sky", "Wherever you are right now."),
    ("random", "Strangest street name near you", "Mine is Pudding Lane."),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Post:
    """An initial post in a forum."""

    post_id: str
    forum: str
    title: str
    body: str
    author: str = "seed"
    avatar: str = "avatar-1"
    created_at: str = field(default_factory=_now)
    replies: list[dict[str, str]] = field(default_factory=list)


class SiteState:
    """Thread-safe in-memory store for the mock site."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.posts: dict[str, Post] = {}
        self.counter = 0
        self.write_requests = 0
        self.page_views = 0
        self.rate_limited_responses = 0
        self.identities_issued = 0
        self.reset()

    def reset(self) -> None:
        """Restore the seeded content."""
        with self.lock:
            self.posts.clear()
            self.counter = 0
            self.write_requests = 0
            self.page_views = 0
            self.rate_limited_responses = 0
            self.identities_issued = 0
            for forum, title, body in SEED_POSTS:
                self.counter += 1
                post_id = f"P{self.counter:05d}"
                self.posts[post_id] = Post(post_id=post_id, forum=forum, title=title,
                                           body=body)

    def add_post(self, forum: str, title: str, body: str, author: str,
                 avatar: str) -> Post:
        """Create a new initial post."""
        with self.lock:
            self.counter += 1
            post_id = f"P{self.counter:05d}"
            post = Post(post_id=post_id, forum=forum, title=title or body[:60],
                        body=body, author=author, avatar=avatar)
            self.posts[post_id] = post
            return post

    def add_reply(self, post_id: str, body: str, author: str) -> bool:
        """Append a reply to an existing post."""
        with self.lock:
            post = self.posts.get(post_id)
            if post is None:
                return False
            post.replies.append({"body": body, "author": author,
                                 "created_at": _now()})
            return True

    def by_forum(self, forum: str) -> list[Post]:
        """Posts in a forum, newest first."""
        with self.lock:
            return sorted((post for post in self.posts.values() if post.forum == forum),
                          key=lambda post: post.created_at, reverse=True)

    def stats(self) -> dict[str, Any]:
        """Counters used by the verification scripts and tests."""
        with self.lock:
            replies = sum(len(post.replies) for post in self.posts.values())
            per_forum: dict[str, dict[str, int]] = {}
            for key, _name in FORUMS:
                forum_posts = [post for post in self.posts.values() if post.forum == key]
                per_forum[key] = {
                    "posts": len(forum_posts),
                    "replies": sum(len(post.replies) for post in forum_posts),
                }
            return {
                "posts": len(self.posts),
                "replies": replies,
                "seed_posts": len(SEED_POSTS),
                "created_posts": len(self.posts) - len(SEED_POSTS),
                "page_views": self.page_views,
                "write_requests": self.write_requests,
                "rate_limited_responses": self.rate_limited_responses,
                "identities_issued": self.identities_issued,
                "per_forum": per_forum,
            }


STATE = SiteState()


class Options:
    """Runtime switches for failure-path testing."""

    rate_limit_after: int = 0
    latency_ms: int = 0
    error_rate: float = 0.0
    require_identity: bool = True


OPTIONS = Options()

_STYLE = """
body{font:15px/1.5 system-ui,Segoe UI,Roboto,Arial,sans-serif;margin:0;padding:1.5rem;
     background:#fbfbfd;color:#16181d}
.wrap{max-width:860px;margin:0 auto}
h1{font-size:1.4rem;margin:.2rem 0 1rem}
h2{font-size:1.05rem;margin:1.6rem 0 .6rem}
.row{display:flex;flex-wrap:wrap;gap:.5rem;margin:.5rem 0 1rem}
button,.link{font:inherit;padding:.45rem .8rem;border:1px solid #ccd2dc;background:#fff;
  border-radius:8px;cursor:pointer;text-decoration:none;color:inherit;display:inline-block}
button.selected,.avatar-option.selected{border-color:#2f6feb;background:#eaf1ff}
.post-list{list-style:none;padding:0;margin:0}
.post{border:1px solid #e3e6ec;border-radius:10px;padding:.8rem 1rem;margin-bottom:.6rem;
  background:#fff}
.post-title{font-weight:600;margin:0 0 .25rem}
.post-body{margin:0;color:#41474f}
.meta{color:#767d88;font-size:.8rem;margin-top:.4rem}
textarea,input[type=text]{width:100%;font:inherit;padding:.5rem;border:1px solid #ccd2dc;
  border-radius:8px;margin:.3rem 0}
.flash-success{background:#e7f6ec;border:1px solid #b7e0c6;padding:.5rem .8rem;
  border-radius:8px;margin:.6rem 0}
.rate-limit-notice{background:#fdecea;border:1px solid #f5c2bd;padding:.6rem .9rem;
  border-radius:8px}
.current-identity{color:#41474f;font-size:.9rem}
"""


def _page(title: str, body: str) -> HTMLResponse:
    STATE.page_views += 1
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} - Mock Forum</title><style>{_STYLE}</style></head>
<body><div class="wrap">{body}</div></body></html>""")


def _identity(request: Request) -> tuple[str, str]:
    return (request.cookies.get("sw_username", ""),
            request.cookies.get("sw_avatar", ""))


def _usernames_for(request: Request) -> list[str]:
    """The site offers a rotating subset of temporary usernames."""
    seed = request.cookies.get("sw_seed") or str(random.randint(1, 10_000))
    generator = random.Random(seed)
    return generator.sample(USERNAME_POOL, 5)


def _delay() -> None:
    if OPTIONS.latency_ms:
        time.sleep(OPTIONS.latency_ms / 1000.0)


def _write_guard() -> Response | None:
    """Apply the configured rate-limit / error-injection behaviour."""
    STATE.write_requests += 1
    if OPTIONS.rate_limit_after and STATE.write_requests > OPTIONS.rate_limit_after:
        STATE.rate_limited_responses += 1
        return HTMLResponse(
            '<div data-testid="rate-limited" class="rate-limit-notice">'
            "Too many requests. Please slow down.</div>",
            status_code=429, headers={"Retry-After": "30"})
    if OPTIONS.error_rate and random.random() < OPTIONS.error_rate:
        return HTMLResponse('<div data-testid="error" class="error-banner">'
                            "Internal error</div>", status_code=500)
    return None


def create_app() -> FastAPI:
    """Build the mock-site FastAPI application."""
    app = FastAPI(title="Social Worker Mock Forum", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def landing(request: Request) -> HTMLResponse:
        _delay()
        username, avatar = _identity(request)
        usernames = _usernames_for(request)

        avatar_buttons = "".join(
            f'<button type="submit" name="avatar" value="{key}" '
            f'class="avatar-option{" selected" if key == avatar else ""}" '
            f'data-testid="avatar-option" data-avatar="{key}">{label}</button>'
            for key, label in AVATARS)
        username_buttons = "".join(
            f'<button type="submit" name="username" value="{name}" '
            f'class="username-option{" selected" if name == username else ""}" '
            f'data-testid="username-option" data-username="{name}">{name}</button>'
            for name in usernames)
        forum_links = "".join(
            f'<a class="link forum-link" data-testid="forum-link" data-forum="{key}" '
            f'href="/forum/{key}"><span data-testid="forum-name">{name}</span></a>'
            for key, name in FORUMS)

        identity_line = (f'<p class="current-identity" data-testid="current-identity">'
                         f"Signed in as {username or '(no username yet)'} "
                         f"/ {avatar or '(no avatar yet)'}</p>")

        return _page("Pick an identity", f"""
<h1>Mock Anonymous Forum</h1>
{identity_line}
<h2>Choose an avatar</h2>
<form method="post" action="/identity/avatar"><div class="row">{avatar_buttons}</div></form>
<h2>Choose a temporary username</h2>
<form method="post" action="/identity/username"><div class="row">{username_buttons}</div></form>
<form method="post" action="/identity/refresh">
  <button type="submit" data-testid="username-refresh" id="refresh-usernames">
    Show different usernames</button>
</form>
<form method="post" action="/identity/enter">
  <button type="submit" data-testid="enter-site" id="enter">Enter the forums</button>
</form>
<h2>Forums</h2>
<nav class="forums row">{forum_links}</nav>
""")

    @app.post("/identity/avatar")
    def choose_avatar(avatar: str = Form(...)) -> RedirectResponse:
        _delay()
        response = RedirectResponse("/", status_code=303)
        response.set_cookie("sw_avatar", avatar, httponly=False, samesite="lax")
        return response

    @app.post("/identity/username")
    def choose_username(username: str = Form(...)) -> RedirectResponse:
        _delay()
        STATE.identities_issued += 1
        response = RedirectResponse("/", status_code=303)
        response.set_cookie("sw_username", username, httponly=False, samesite="lax")
        return response

    @app.post("/identity/refresh")
    def refresh_usernames() -> RedirectResponse:
        response = RedirectResponse("/", status_code=303)
        response.set_cookie("sw_seed", str(random.randint(1, 10_000)))
        return response

    @app.post("/identity/enter")
    def enter_site() -> RedirectResponse:
        return RedirectResponse("/", status_code=303)

    @app.get("/forum/{forum_key}", response_class=HTMLResponse)
    def forum_page(request: Request, forum_key: str,
                   created: int = 0) -> HTMLResponse:
        _delay()
        username, avatar = _identity(request)
        names = dict(FORUMS)
        if forum_key not in names:
            return _page("Unknown forum",
                         '<div data-testid="error" class="error-banner">'
                         "No such forum.</div>")

        posts = STATE.by_forum(forum_key)
        items = "".join(
            f'<li class="post" data-testid="post-item" data-post-id="{post.post_id}" '
            f'data-created-at="{post.created_at}">'
            f'<p class="post-title" data-testid="post-title">{post.title}</p>'
            f'<p class="post-body" data-testid="post-body">{post.body}</p>'
            f'<p class="meta">{post.author} &middot; {len(post.replies)} repl'
            f'{"y" if len(post.replies) == 1 else "ies"}</p>'
            f'<a class="link post-link" data-testid="post-link" '
            f'href="/post/{post.post_id}">Open</a></li>'
            for post in posts)

        composer = ""
        if username or not OPTIONS.require_identity:
            composer = f"""
<h2>New post</h2>
<form class="new-post" method="post" action="/forum/{forum_key}/post">
  <input type="text" name="title" placeholder="Title (optional)"
         data-testid="new-post-title">
  <textarea name="body" rows="4" placeholder="What is on your mind?"
            data-testid="new-post-body" id="new-post"></textarea>
  <button type="submit" data-testid="new-post-submit" id="create-post">Publish</button>
</form>"""
        else:
            composer = ('<p class="current-identity">Pick a username on the '
                        '<a href="/">home page</a> before posting.</p>')

        flash = ('<div class="flash-success" data-testid="post-created">'
                 "Your post was published.</div>" if created else "")

        return _page(names[forum_key], f"""
<h1 class="forum-title" data-testid="forum-heading">{names[forum_key]}</h1>
<p class="current-identity" data-testid="current-identity">
  {username or "anonymous visitor"} / {avatar or "no avatar"}</p>
{flash}
<a class="link" href="/">Back to all forums</a>
<h2>Initial posts</h2>
<ul class="post-list" data-testid="post-list">{items or "<li>No posts yet.</li>"}</ul>
{composer}
""")

    @app.post("/forum/{forum_key}/post")
    def create_post(request: Request, forum_key: str, body: str = Form(...),
                    title: str = Form("")) -> Response:
        _delay()
        guard = _write_guard()
        if guard is not None:
            return guard
        username, avatar = _identity(request)
        if OPTIONS.require_identity and not username:
            return HTMLResponse('<div data-testid="error" class="error-banner">'
                                "Choose a username first.</div>", status_code=403)
        STATE.add_post(forum_key, title.strip(), body.strip(), username or "anonymous",
                       avatar or "avatar-1")
        return RedirectResponse(f"/forum/{forum_key}?created=1", status_code=303)

    @app.get("/post/{post_id}", response_class=HTMLResponse)
    def post_page(request: Request, post_id: str, created: int = 0) -> HTMLResponse:
        _delay()
        username, _avatar = _identity(request)
        post = STATE.posts.get(post_id)
        if post is None:
            return _page("Unknown post",
                         '<div data-testid="error" class="error-banner">'
                         "No such post.</div>")

        replies = "".join(
            f'<li class="reply-item post" data-testid="reply-item">'
            f'<p class="post-body">{reply["body"]}</p>'
            f'<p class="meta">{reply["author"]}</p></li>'
            for reply in post.replies)

        form = ""
        if username or not OPTIONS.require_identity:
            form = f"""
<h2>Reply</h2>
<form class="reply-form" method="post" action="/post/{post.post_id}/reply">
  <textarea name="reply" rows="3" data-testid="reply-body" id="reply-body"
            placeholder="Write a reply"></textarea>
  <button type="submit" data-testid="reply-submit" id="create-reply">Send reply</button>
</form>"""

        flash = ('<div class="flash-success" data-testid="reply-created">'
                 "Your reply was posted.</div>" if created else "")

        return _page(post.title, f"""
<h1 class="post-title" data-testid="post-title">{post.title}</h1>
<p class="post-body" data-testid="post-body">{post.body}</p>
<p class="meta">{post.author} &middot; {post.created_at}</p>
{flash}
<a class="link" href="/forum/{post.forum}">Back to {post.forum}</a>
<h2>Replies</h2>
<ul class="post-list">{replies or "<li>No replies yet.</li>"}</ul>
{form}
""")

    @app.post("/post/{post_id}/reply")
    def create_reply(request: Request, post_id: str, reply: str = Form(...)) -> Response:
        _delay()
        guard = _write_guard()
        if guard is not None:
            return guard
        username, _avatar = _identity(request)
        if OPTIONS.require_identity and not username:
            return HTMLResponse('<div data-testid="error" class="error-banner">'
                                "Choose a username first.</div>", status_code=403)
        if not STATE.add_reply(post_id, reply.strip(), username or "anonymous"):
            return HTMLResponse('<div data-testid="error" class="error-banner">'
                                "No such post.</div>", status_code=404)
        return RedirectResponse(f"/post/{post_id}?created=1", status_code=303)

    @app.get("/api/stats")
    def api_stats() -> JSONResponse:
        """Counters used by the Social Worker verification scripts."""
        return JSONResponse(STATE.stats())

    @app.post("/api/reset")
    def api_reset() -> JSONResponse:
        """Restore the seeded content (used between test runs)."""
        STATE.reset()
        return JSONResponse({"status": "reset", **STATE.stats()})

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        """Liveness probe used by the CLI before starting a run."""
        return JSONResponse({"status": "ok"})

    return app


app = create_app()


def main() -> None:
    """Command-line entry point for the mock site."""
    parser = argparse.ArgumentParser(description="Social Worker mock forum website")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--rate-limit-after", type=int, default=0,
                        help="answer HTTP 429 after N write requests")
    parser.add_argument("--latency-ms", type=int, default=0,
                        help="add artificial server latency")
    parser.add_argument("--error-rate", type=float, default=0.0,
                        help="fraction of writes that fail with HTTP 500")
    parser.add_argument("--allow-anonymous-posting", action="store_true",
                        help="do not require an identity before posting")
    arguments = parser.parse_args()

    OPTIONS.rate_limit_after = arguments.rate_limit_after
    OPTIONS.latency_ms = arguments.latency_ms
    OPTIONS.error_rate = arguments.error_rate
    OPTIONS.require_identity = not arguments.allow_anonymous_posting

    import uvicorn

    uvicorn.run(app, host=arguments.host, port=arguments.port, log_level="warning")


if __name__ == "__main__":
    main()
