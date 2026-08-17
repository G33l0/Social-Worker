"""Interactive content library management (spec sections 5-6)."""

from __future__ import annotations

from pathlib import Path

from cli import console
from app.content.library import ContentLibrary, DuplicateContentError
from app.core.content_manager import ContentManager
from app.utils.config import ContentSelectionMode, Settings

SELECTION_MODES = [mode.value for mode in ContentSelectionMode]


def run_content_wizard(settings: Settings, *, manager: ContentManager | None = None,
                       library: str = "") -> ContentManager:
    """Import, edit and inspect the post and reply libraries."""
    manager = manager or ContentManager(settings.content, settings.posting,
                                        settings.replies)
    if not len(manager.post_library) and not len(manager.reply_library):
        manager.load()

    while True:
        console.header("CONTENT LIBRARIES")
        stats = manager.stats()
        print(console.keyvalues({
            "Posts": f"{stats['posts']['enabled']} enabled / {stats['posts']['total']} total",
            "Replies": f"{stats['replies']['enabled']} enabled / "
                       f"{stats['replies']['total']} total",
            "Post selection": settings.posting.content_selection_mode.value,
            "Reply selection": settings.replies.content_selection_mode.value,
            "Duplicate prevention": "per worker" if
            settings.content.prevent_duplicates_per_worker else "off",
        }))

        action = console.choose(
            "Action:",
            ["Import posts", "Import replies", "List posts", "List replies",
             "Add item", "Edit item", "Delete item", "Enable/disable item",
             "Set selection mode", "Save libraries", "Return"],
            default=11)

        try:
            if action == "Import posts":
                _import(manager, settings, "posts")
            elif action == "Import replies":
                _import(manager, settings, "replies")
            elif action == "List posts":
                _list(manager.post_library)
            elif action == "List replies":
                _list(manager.reply_library)
            elif action == "Add item":
                _add(manager)
            elif action == "Edit item":
                _edit(manager)
            elif action == "Delete item":
                _delete(manager)
            elif action == "Enable/disable item":
                _toggle(manager)
            elif action == "Set selection mode":
                _selection_mode(manager, settings)
            elif action == "Save libraries":
                paths = manager.save()
                console.success(f"Saved {paths['posts']} and {paths['replies']}")
            else:
                return manager
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            console.error(str(exc))
            console.pause()


def _library_for(manager: ContentManager, kind: str) -> ContentLibrary:
    return manager.post_library if kind == "posts" else manager.reply_library


def _import(manager: ContentManager, settings: Settings, kind: str) -> None:
    default = (settings.content.posts_path if kind == "posts"
               else settings.content.replies_path)
    path = console.ask(f"Path to {kind} file (TXT / CSV / JSON)", default, required=True)
    if not Path(path).exists():
        console.error(f"File not found: {path}")
        return
    replace = console.ask_bool("Replace the current library?", False)
    category = console.ask("Category for imported items", "general")
    paragraph = console.ask_bool("Treat blank lines as item separators "
                                 "(multi-line items)?", False)
    if kind == "posts":
        added = manager.import_posts(path, replace=replace, category=category,
                                     paragraph_mode=paragraph)
    else:
        added = manager.import_replies(path, replace=replace, category=category,
                                       paragraph_mode=paragraph)
    console.success(f"Imported {added} {kind}")


def _list(library: ContentLibrary) -> None:
    rows = [
        {"ref": entry.ref, "category": entry.category, "enabled": entry.enabled,
         "uses": entry.usage_count, "preview": entry.preview(70)}
        for entry in library.all()
    ]
    console.section(f"{library.library_name.title()} ({len(rows)})")
    print(console.table(rows, max_rows=40))
    console.pause()


def _add(manager: ContentManager) -> None:
    kind = console.choose("Which library?", ["posts", "replies"], default=1)
    library = _library_for(manager, kind)
    body = console.ask("Content body", required=True)
    category = console.ask("Category", "general")
    try:
        entry = library.add(body, category=category)
    except DuplicateContentError as exc:
        console.warn(str(exc))
        return
    manager.build_rotators()
    console.success(f"Added {entry.ref}")


def _edit(manager: ContentManager) -> None:
    kind = console.choose("Which library?", ["posts", "replies"], default=1)
    library = _library_for(manager, kind)
    ref = console.ask("Item reference (e.g. POST-001)", required=True).upper()
    entry = library.get(ref)
    print(console.keyvalues({"Current": entry.body, "Category": entry.category}))
    body = console.ask("New body (blank keeps current)", "")
    category = console.ask("New category (blank keeps current)", "")
    library.edit(ref, body=body or None, category=category or None)
    manager.build_rotators()
    console.success(f"Updated {ref}")


def _delete(manager: ContentManager) -> None:
    kind = console.choose("Which library?", ["posts", "replies"], default=1)
    library = _library_for(manager, kind)
    ref = console.ask("Item reference to delete", required=True).upper()
    if console.ask_bool(f"Really delete {ref}?", False):
        library.delete(ref)
        manager.build_rotators()
        console.success(f"Deleted {ref}")


def _toggle(manager: ContentManager) -> None:
    kind = console.choose("Which library?", ["posts", "replies"], default=1)
    library = _library_for(manager, kind)
    ref = console.ask("Item reference", required=True).upper()
    entry = library.get(ref)
    library.set_enabled(ref, not entry.enabled)
    manager.build_rotators()
    console.success(f"{ref} is now {'disabled' if entry.enabled else 'enabled'}")


def _selection_mode(manager: ContentManager, settings: Settings) -> None:
    kind = console.choose("Which library?", ["posts", "replies"], default=1)
    mode = console.choose("Selection mode:", SELECTION_MODES, default=3)
    if kind == "posts":
        settings.posting.content_selection_mode = ContentSelectionMode(mode)
    else:
        settings.replies.content_selection_mode = ContentSelectionMode(mode)
    manager.posting = settings.posting
    manager.replies_config = settings.replies
    manager.build_rotators()
    console.success(f"{kind} selection mode set to {mode}")
