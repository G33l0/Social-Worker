"""Configurable selector catalogue.

Selectors live in one place (``config/selectors.yaml``) instead of being
scattered through the automation code, so pointing Social Worker at a different
authorized site is a configuration change rather than a code change.  Each
logical element accepts a list of candidate selectors that are tried in order,
which keeps the adapter working across minor markup changes on the target site.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.utils.config import ConfigPaths, load_yaml_document, save_yaml_document
from app.utils.logger import get_logger

LOGGER = get_logger("browser.selectors")


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class IdentitySelectors(Base):
    """Elements used to obtain the site-provided avatar and username.

    Social Worker never invents an identity: it reads whatever choices the site
    offers and picks one of them.
    """

    avatar_options: list[str] = Field(default_factory=lambda: [
        "[data-testid='avatar-option']", ".avatar-option", "button.avatar", "img.avatar-choice",
    ])
    avatar_label_attribute: str = "data-avatar"
    avatar_selected: list[str] = Field(default_factory=lambda: [
        "[data-testid='avatar-option'].selected", ".avatar-option.selected",
    ])
    username_options: list[str] = Field(default_factory=lambda: [
        "[data-testid='username-option']", ".username-option", "select[name='username'] option",
    ])
    username_label_attribute: str = "data-username"
    username_selected: list[str] = Field(default_factory=lambda: [
        "[data-testid='username-option'].selected", ".username-option.selected",
    ])
    username_refresh: list[str] = Field(default_factory=lambda: [
        "[data-testid='username-refresh']", "#refresh-usernames",
    ])
    enter_button: list[str] = Field(default_factory=lambda: [
        "[data-testid='enter-site']", "#enter", "button[type='submit']",
    ])
    identity_banner: list[str] = Field(default_factory=lambda: [
        "[data-testid='current-identity']", ".current-identity",
    ])


class ForumSelectors(Base):
    """Elements used to discover and enter forums."""

    forum_links: list[str] = Field(default_factory=lambda: [
        "[data-testid='forum-link']", "a.forum-link", "nav.forums a",
    ])
    forum_key_attribute: str = "data-forum"
    forum_name: list[str] = Field(default_factory=lambda: [
        "[data-testid='forum-name']", ".forum-name",
    ])
    forum_heading: list[str] = Field(default_factory=lambda: [
        "[data-testid='forum-heading']", "h1.forum-title",
    ])
    forum_ready: list[str] = Field(default_factory=lambda: [
        "[data-testid='post-list']", ".post-list",
    ])


class PostSelectors(Base):
    """Elements used to read the forum feed and create a post."""

    post_items: list[str] = Field(default_factory=lambda: [
        "[data-testid='post-item']", "article.post", "li.post",
    ])
    post_id_attribute: str = "data-post-id"
    post_link: list[str] = Field(default_factory=lambda: [
        "[data-testid='post-link']", "a.post-link",
    ])
    post_title: list[str] = Field(default_factory=lambda: [
        "[data-testid='post-title']", ".post-title", "h2",
    ])
    post_body: list[str] = Field(default_factory=lambda: [
        "[data-testid='post-body']", ".post-body",
    ])
    post_timestamp_attribute: str = "data-created-at"
    new_post_input: list[str] = Field(default_factory=lambda: [
        "[data-testid='new-post-body']", "textarea[name='body']", "#new-post",
    ])
    new_post_title_input: list[str] = Field(default_factory=lambda: [
        "[data-testid='new-post-title']", "input[name='title']",
    ])
    new_post_submit: list[str] = Field(default_factory=lambda: [
        "[data-testid='new-post-submit']", "button#create-post", "form.new-post button[type='submit']",
    ])
    post_success_flag: list[str] = Field(default_factory=lambda: [
        "[data-testid='post-created']", ".flash-success",
    ])


class ReplySelectors(Base):
    """Elements used to reply to an existing initial post."""

    reply_items: list[str] = Field(default_factory=lambda: [
        "[data-testid='reply-item']", ".reply-item",
    ])
    reply_input: list[str] = Field(default_factory=lambda: [
        "[data-testid='reply-body']", "textarea[name='reply']", "#reply-body",
    ])
    reply_submit: list[str] = Field(default_factory=lambda: [
        "[data-testid='reply-submit']", "button#create-reply", "form.reply-form button[type='submit']",
    ])
    reply_success_flag: list[str] = Field(default_factory=lambda: [
        "[data-testid='reply-created']", ".flash-success",
    ])


class RateLimitSelectors(Base):
    """Markers the target site uses to signal throttling.

    When any of these appear the worker records the condition and backs off; it
    never tries to work around the restriction.
    """

    rate_limited_banner: list[str] = Field(default_factory=lambda: [
        "[data-testid='rate-limited']", ".rate-limit-notice",
    ])
    error_banner: list[str] = Field(default_factory=lambda: [
        "[data-testid='error']", ".error-banner",
    ])


class SelectorSet(Base):
    """The complete selector catalogue for one target site."""

    site: str = "generic"
    identity: IdentitySelectors = Field(default_factory=IdentitySelectors)
    forums: ForumSelectors = Field(default_factory=ForumSelectors)
    posts: PostSelectors = Field(default_factory=PostSelectors)
    replies: ReplySelectors = Field(default_factory=ReplySelectors)
    rate_limit: RateLimitSelectors = Field(default_factory=RateLimitSelectors)

    def candidates(self, path: str) -> list[str]:
        """Return the candidate selectors for a dotted *path*.

        ``selectors.candidates("posts.post_items")`` -> list of CSS selectors.
        """
        node: Any = self
        for part in path.split("."):
            node = getattr(node, part, None)
            if node is None:
                raise KeyError(f"Unknown selector path: {path}")
        if isinstance(node, str):
            return [node]
        if isinstance(node, list):
            return list(node)
        raise KeyError(f"Selector path {path!r} does not resolve to a selector")

    def first(self, path: str) -> str:
        """First candidate for *path* (useful for error messages)."""
        candidates = self.candidates(path)
        return candidates[0] if candidates else ""

    def attribute(self, path: str) -> str:
        """Return an attribute-name entry such as ``posts.post_id_attribute``."""
        node: Any = self
        for part in path.split("."):
            node = getattr(node, part)
        return str(node)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "SelectorSet":
        """Load ``config/selectors.yaml`` (returns defaults when absent)."""
        target = Path(path or ConfigPaths().selectors)
        document = load_yaml_document(target)
        payload = document.get("selectors", document)
        if not payload:
            LOGGER.info("No selector overrides at %s; using built-in defaults", target)
            return cls()
        return cls.model_validate(payload)

    def save(self, path: str | Path | None = None) -> Path:
        """Persist the selector catalogue."""
        target = Path(path or ConfigPaths().selectors)
        save_yaml_document(target, {"selectors": self.model_dump(mode="json")})
        return target
