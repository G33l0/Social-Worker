"""Site-side reply target selection and submission."""

from __future__ import annotations

import random
from typing import Sequence

from app.browser.interaction import Interaction
from app.browser.navigator import Navigator
from app.browser.selectors import SelectorSet
from app.site.base_adapter import ActionResult, AdapterError, PostRef
from app.utils.config import ReplyTargetMode
from app.utils.logger import get_logger
from app.utils.time_utils import duration_ms, utc_now

LOGGER = get_logger("site.reply")


class SiteReplyManager:
    """Chooses an eligible initial post and submits a reply to it."""

    def __init__(self, navigator: Navigator, interaction: Interaction,
                 selectors: SelectorSet, *, dry_run: bool = False,
                 rng: random.Random | None = None) -> None:
        self.navigator = navigator
        self.interaction = interaction
        self.selectors = selectors
        self.dry_run = dry_run
        self._rng = rng or random.Random()

    # --------------------------------------------------------------- targeting
    def select_target(self, posts: Sequence[PostRef], *,
                      mode: ReplyTargetMode = ReplyTargetMode.RANDOM,
                      keywords: Sequence[str] = (),
                      specific_post_id: str = "") -> PostRef | None:
        """Pick the initial post to reply to according to *mode*."""
        candidates = list(posts)
        if not candidates:
            return None

        if mode is ReplyTargetMode.SPECIFIC:
            for post in candidates:
                if post.post_id == specific_post_id:
                    return post
            LOGGER.warning("Specific post %s not present in this forum", specific_post_id)
            return None

        if mode is ReplyTargetMode.OLDEST:
            dated = [post for post in candidates if post.created_at]
            if dated:
                return min(dated, key=lambda post: post.created_at)
            return candidates[-1]

        if mode is ReplyTargetMode.NEWEST:
            dated = [post for post in candidates if post.created_at]
            if dated:
                return max(dated, key=lambda post: post.created_at)
            return candidates[0]

        if mode is ReplyTargetMode.FILTERED and keywords:
            lowered = [keyword.lower() for keyword in keywords]
            filtered = [
                post for post in candidates
                if any(keyword in f"{post.title} {post.excerpt}".lower()
                       for keyword in lowered)
            ]
            if filtered:
                return self._rng.choice(filtered)
            LOGGER.info("No post matched the reply filter; falling back to random")

        return self._rng.choice(candidates)

    # -------------------------------------------------------------- submission
    async def create(self, post: PostRef, body: str, *,
                     timeout_ms: int = 20_000) -> ActionResult:
        """Submit a reply to *post*.  Dry-run composes without submitting."""
        started = utc_now()
        input_candidates = self.selectors.candidates("replies.reply_input")
        if not await self.interaction.exists(input_candidates, timeout_ms=3_000):
            raise AdapterError("No reply form found on this post",
                               action="CREATE_REPLY",
                               selector=self.selectors.first("replies.reply_input"))

        filled = await self.interaction.fill(input_candidates, body,
                                             action="FILL_REPLY_BODY",
                                             timeout_ms=timeout_ms)
        if not filled.ok:
            return ActionResult(action="CREATE_REPLY", ok=False,
                                duration_ms=round(duration_ms(started), 2),
                                target_id=post.post_id, selector=filled.selector,
                                error=filled.error)

        if self.dry_run:
            elapsed = round(duration_ms(started), 2)
            LOGGER.info("DRY RUN: reply composed for %s but not submitted", post.post_id)
            return ActionResult(action="CREATE_REPLY", ok=True, duration_ms=elapsed,
                                target_id=post.post_id, dry_run=True,
                                detail="composed, submission disabled",
                                metadata={"characters": len(body),
                                          "url": self.navigator.current_url})

        submitted = await self.interaction.click(
            self.selectors.candidates("replies.reply_submit"),
            action="SUBMIT_REPLY", timeout_ms=timeout_ms)
        if not submitted.ok:
            return ActionResult(action="CREATE_REPLY", ok=False,
                                duration_ms=round(duration_ms(started), 2),
                                target_id=post.post_id, selector=submitted.selector,
                                error=submitted.error)

        await self.navigator.wait_for_any(
            self.selectors.candidates("replies.reply_success_flag")
            + self.selectors.candidates("replies.reply_items"),
            timeout_ms=timeout_ms)
        elapsed = round(duration_ms(started), 2)
        LOGGER.info("Replied to %s in %.0fms", post.post_id, elapsed)
        return ActionResult(action="CREATE_REPLY", ok=True, duration_ms=elapsed,
                            target_id=post.post_id, detail=f"{len(body)} chars",
                            metadata={"characters": len(body),
                                      "url": self.navigator.current_url})

    async def count_replies(self) -> int:
        """Number of replies currently rendered on the post page."""
        _, total = await self.interaction.count(
            self.selectors.candidates("replies.reply_items"), timeout_ms=2_000)
        return total
