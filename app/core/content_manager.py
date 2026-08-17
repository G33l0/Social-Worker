"""Content orchestration: libraries, rotation policy and database sync."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.content.library import ContentEntry
from app.content.post_library import PostLibrary
from app.content.reply_library import ReplyLibrary
from app.content.rotation import ContentExhausted, ContentRotator
from app.database.database import Database
from app.database.models import ContentItem, ReplyItem
from app.utils.config import ContentConfig, PostingConfig, ReplyConfig
from app.utils.logger import get_logger

LOGGER = get_logger("content.manager")


class ContentManager:
    """Owns the post and reply libraries plus their rotation policies."""

    def __init__(self, content_config: ContentConfig | None = None,
                 posting: PostingConfig | None = None,
                 replies: ReplyConfig | None = None) -> None:
        self.config = content_config or ContentConfig()
        self.posting = posting or PostingConfig()
        self.replies_config = replies or ReplyConfig()
        self.post_library = PostLibrary(max_size=self.config.max_library_size)
        self.reply_library = ReplyLibrary(max_size=self.config.max_library_size)
        self.post_rotator: ContentRotator | None = None
        self.reply_rotator: ContentRotator | None = None

    # ------------------------------------------------------------------ load
    def load(self, *, posts_path: str | Path | None = None,
             replies_path: str | Path | None = None) -> dict[str, int]:
        """Load both libraries from disk and build their rotators."""
        posts_file = Path(posts_path or self.config.posts_path)
        replies_file = Path(replies_path or self.config.replies_path)

        loaded = {"posts": 0, "replies": 0}
        if posts_file.exists():
            self.post_library = PostLibrary.from_file(
                posts_file, max_size=self.config.max_library_size)
            loaded["posts"] = len(self.post_library)
        else:
            LOGGER.warning("Post library not found at %s", posts_file)

        if replies_file.exists():
            self.reply_library = ReplyLibrary.from_file(
                replies_file, max_size=self.config.max_library_size)
            loaded["replies"] = len(self.reply_library)
        else:
            LOGGER.warning("Reply library not found at %s", replies_file)

        self.build_rotators()
        LOGGER.info("Content loaded: %d post(s), %d reply/replies",
                    loaded["posts"], loaded["replies"])
        return loaded

    def build_rotators(self) -> None:
        """(Re)create the rotators from the current configuration."""
        self.post_rotator = ContentRotator(
            self.post_library,
            mode=self.posting.content_selection_mode,
            prevent_duplicates_per_worker=self.config.prevent_duplicates_per_worker,
            prevent_global_duplicates=self.config.prevent_global_duplicates,
        )
        self.reply_rotator = ContentRotator(
            self.reply_library,
            mode=self.replies_config.content_selection_mode,
            prevent_duplicates_per_worker=self.config.prevent_duplicates_per_worker,
            prevent_global_duplicates=self.config.prevent_global_duplicates,
        )

    # --------------------------------------------------------------- selection
    def next_post(self, worker_id: str, *, category: str | None = None) -> ContentEntry:
        """Return the next post body for *worker_id*."""
        if self.post_rotator is None:
            self.build_rotators()
        assert self.post_rotator is not None
        if len(self.post_library) == 0:
            raise ContentExhausted(
                "The post library is empty. Import content first (menu option 4).")
        return self.post_rotator.select(worker_id, category=category)

    def next_reply(self, worker_id: str, *, category: str | None = None) -> ContentEntry:
        """Return the next reply body for *worker_id*."""
        if self.reply_rotator is None:
            self.build_rotators()
        assert self.reply_rotator is not None
        if len(self.reply_library) == 0:
            raise ContentExhausted(
                "The reply library is empty. Import replies first (menu option 5).")
        return self.reply_rotator.select(worker_id, category=category)

    def reset_worker(self, worker_id: str) -> None:
        """Clear a worker's content history."""
        for rotator in (self.post_rotator, self.reply_rotator):
            if rotator is not None:
                rotator.reset_worker(worker_id)

    # ------------------------------------------------------------------ import
    def import_posts(self, path: str | Path, *, replace: bool = False,
                     category: str = "general", paragraph_mode: bool = False) -> int:
        """Import posts from TXT/CSV/JSON."""
        added = self.post_library.import_from(path, replace=replace, category=category,
                                              paragraph_mode=paragraph_mode)
        self.build_rotators()
        return added

    def import_replies(self, path: str | Path, *, replace: bool = False,
                       category: str = "general", paragraph_mode: bool = False) -> int:
        """Import replies from TXT/CSV/JSON."""
        added = self.reply_library.import_from(path, replace=replace, category=category,
                                               paragraph_mode=paragraph_mode)
        self.build_rotators()
        return added

    # ------------------------------------------------------------------ persist
    def sync_to_database(self, database: Database) -> int:
        """Mirror both libraries into the ``content`` and ``replies`` tables."""
        def _write(session: Any) -> int:
            written = 0
            for entry in self.post_library.all():
                record = session.query(ContentItem).filter_by(
                    library="posts", external_id=entry.ref).one_or_none()
                if record is None:
                    record = ContentItem(library="posts", external_id=entry.ref)
                    session.add(record)
                record.category = entry.category
                record.title = entry.title
                record.body = entry.body
                record.checksum = entry.checksum
                record.enabled = entry.enabled
                record.usage_count = entry.usage_count
                record.source = entry.source
                written += 1
            for entry in self.reply_library.all():
                record = session.query(ReplyItem).filter_by(
                    library="replies", external_id=entry.ref).one_or_none()
                if record is None:
                    record = ReplyItem(library="replies", external_id=entry.ref)
                    session.add(record)
                record.category = entry.category
                record.body = entry.body
                record.checksum = entry.checksum
                record.enabled = entry.enabled
                record.usage_count = entry.usage_count
                record.source = entry.source
                written += 1
            return written

        written = database.write(_write)
        LOGGER.info("Synced %d content item(s) to the database", written)
        return written

    def save(self, *, posts_path: str | Path | None = None,
             replies_path: str | Path | None = None) -> dict[str, str]:
        """Write both libraries back to their text files."""
        posts_file = Path(posts_path or self.config.posts_path)
        replies_file = Path(replies_path or self.config.replies_path)
        self.post_library.save_txt(posts_file)
        self.reply_library.save_txt(replies_file)
        return {"posts": str(posts_file), "replies": str(replies_file)}

    # -------------------------------------------------------------------- info
    def stats(self) -> dict[str, Any]:
        """Library and rotation statistics."""
        return {
            "posts": self.post_library.stats(),
            "replies": self.reply_library.stats(),
            "post_rotation": self.post_rotator.stats() if self.post_rotator else {},
            "reply_rotation": self.reply_rotator.stats() if self.reply_rotator else {},
        }

    def is_ready(self) -> tuple[bool, list[str]]:
        """True when there is enough content to run the configured behaviour."""
        problems: list[str] = []
        if self.posting.enabled and len(self.post_library.enabled()) == 0:
            problems.append("Posting is enabled but the post library is empty")
        if self.replies_config.enabled and len(self.reply_library.enabled()) == 0:
            problems.append("Replying is enabled but the reply library is empty")
        return (not problems), problems
