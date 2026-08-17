"""Post library: the operator's collection of post bodies (100+ supported)."""

from __future__ import annotations

from pathlib import Path

from app.content.importer import import_file
from app.content.library import ContentLibrary


class PostLibrary(ContentLibrary):
    """Content library specialised for forum posts."""

    library_name = "posts"
    ref_prefix = "POST"

    @classmethod
    def from_file(cls, path: str | Path, *, category: str = "general",
                  paragraph_mode: bool = False, max_size: int = 10_000) -> "PostLibrary":
        """Build a post library from a TXT/CSV/JSON file."""
        library = cls(max_size=max_size)
        entries = import_file(path, category=category, paragraph_mode=paragraph_mode)
        library.load_entries(entries)
        library.source_path = Path(path)
        return library

    def import_from(self, path: str | Path, *, replace: bool = False,
                    category: str = "general", paragraph_mode: bool = False) -> int:
        """Import additional posts; returns the number of new items added."""
        entries = import_file(path, category=category, paragraph_mode=paragraph_mode)
        added = self.load_entries(entries, replace=replace)
        self.source_path = Path(path)
        return added
