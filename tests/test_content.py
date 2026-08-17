"""Content library, import and rotation tests."""

from __future__ import annotations

import json

import pytest

from app.content.importer import ImportError_, import_file
from app.content.library import DuplicateContentError
from app.content.post_library import PostLibrary
from app.content.reply_library import ReplyLibrary
from app.content.rotation import ContentExhausted, ContentRotator
from app.utils.config import ContentSelectionMode


def _write(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_import_txt_skips_comments_and_blanks(tmp_path):
    path = _write(tmp_path / "posts.txt",
                  ["# comment", "", "First post", "Second post"])
    entries = import_file(path)
    assert [entry.body for entry in entries] == ["First post", "Second post"]


def test_import_txt_paragraph_mode(tmp_path):
    path = tmp_path / "posts.txt"
    path.write_text("Line one\nstill one\n\nSecond item\n", encoding="utf-8")
    entries = import_file(path, paragraph_mode=True)
    assert len(entries) == 2
    assert "still one" in entries[0].body


def test_import_csv_with_header(tmp_path):
    path = tmp_path / "posts.csv"
    path.write_text("id,category,body,enabled\n"
                    "P1,questions,How do you focus?,true\n"
                    "P2,general,Hello there,false\n", encoding="utf-8")
    entries = import_file(path)
    assert len(entries) == 2
    assert entries[0].category == "questions"
    assert entries[1].enabled is False


def test_import_json_shapes(tmp_path):
    array = tmp_path / "a.json"
    array.write_text(json.dumps(["one", "two"]), encoding="utf-8")
    assert len(import_file(array)) == 2

    objects = tmp_path / "b.json"
    objects.write_text(json.dumps({"posts": [{"id": "X1", "body": "hello",
                                              "category": "random"}]}),
                       encoding="utf-8")
    entries = import_file(objects)
    assert entries[0].category == "random"


def test_import_rejects_unknown_format(tmp_path):
    path = tmp_path / "posts.xml"
    path.write_text("<posts/>", encoding="utf-8")
    with pytest.raises(ImportError_):
        import_file(path)


def test_library_supports_100_items_and_crud():
    library = PostLibrary()
    added = library.add_many([f"Post number {index:03d}" for index in range(1, 101)])
    assert added == 100
    assert len(library) == 100

    entry = library.all()[0]
    library.edit(entry.ref, body="Edited body", category="questions")
    assert library.get(entry.ref).body == "Edited body"
    assert library.get(entry.ref).category == "questions"

    library.set_enabled(entry.ref, False)
    assert len(library.enabled()) == 99

    library.delete(entry.ref)
    assert len(library) == 99
    assert "questions" not in library.categories()


def test_duplicate_prevention_within_library():
    library = ReplyLibrary()
    library.add("Same body")
    with pytest.raises(DuplicateContentError):
        library.add("Same body")


def test_sequential_rotation_walks_the_library():
    library = PostLibrary()
    library.add_many([f"Item {index}" for index in range(5)])
    rotator = ContentRotator(library, mode=ContentSelectionMode.SEQUENTIAL,
                             prevent_duplicates_per_worker=False)
    picks = [rotator.select("SW-00001").body for _ in range(5)]
    assert picks == [f"Item {index}" for index in range(5)]


def test_random_without_repetition_exhausts_before_repeating():
    library = PostLibrary()
    library.add_many([f"Item {index}" for index in range(10)])
    rotator = ContentRotator(library,
                             mode=ContentSelectionMode.RANDOM_WITHOUT_REPETITION,
                             prevent_duplicates_per_worker=True)
    picks = [rotator.select("SW-00001").ref for _ in range(10)]
    assert len(set(picks)) == 10


def test_per_worker_history_is_isolated():
    library = PostLibrary()
    library.add_many([f"Item {index}" for index in range(4)])
    rotator = ContentRotator(library, mode=ContentSelectionMode.RANDOM)
    first = [rotator.select("SW-00001").ref for _ in range(4)]
    second = [rotator.select("SW-00002").ref for _ in range(4)]
    assert sorted(first) == sorted(second)
    assert len(rotator.history("SW-00001")) == 4
    rotator.reset_worker("SW-00001")
    assert rotator.history("SW-00001") == []


def test_worker_history_recycles_instead_of_stalling():
    library = PostLibrary()
    library.add_many(["only item"])
    rotator = ContentRotator(library, mode=ContentSelectionMode.RANDOM,
                             prevent_duplicates_per_worker=True)
    assert rotator.select("SW-00001").ref == rotator.select("SW-00001").ref


def test_global_duplicate_prevention_raises_when_exhausted():
    library = PostLibrary()
    library.add_many(["a", "b"])
    rotator = ContentRotator(library, mode=ContentSelectionMode.RANDOM,
                             prevent_global_duplicates=True)
    rotator.select("SW-00001")
    rotator.select("SW-00002")
    with pytest.raises(ContentExhausted):
        rotator.select("SW-00003")


def test_category_based_selection_filters_the_pool():
    library = PostLibrary()
    library.add("General one", category="general")
    library.add("Question one", category="questions")
    rotator = ContentRotator(library, mode=ContentSelectionMode.CATEGORY_BASED)
    for _ in range(5):
        assert rotator.select("SW-00001", category="questions").category == "questions"


def test_usage_tracking_increments(content_manager):
    entry = content_manager.next_post("SW-00001")
    assert content_manager.post_library.get(entry.ref).usage_count == 1
    stats = content_manager.stats()
    assert stats["post_rotation"]["total_selections"] == 1


def test_content_manager_reports_readiness(settings):
    from app.core.content_manager import ContentManager

    manager = ContentManager(settings.content, settings.posting, settings.replies)
    ready, problems = manager.is_ready()
    assert ready is False
    assert problems


def test_content_manager_syncs_to_database(content_manager, database):
    written = content_manager.sync_to_database(database)
    assert written == 40

    from app.database.models import ContentItem

    def _count(session):
        return session.query(ContentItem).count()

    assert database.read(_count) == 20
