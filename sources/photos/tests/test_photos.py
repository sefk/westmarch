from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from core.loaders import run_loader
from sources.photos.loader import PhotosLoader, photo_row
from sources.photos.models import Person, Photo

pytestmark = pytest.mark.django_db
PDT = timezone(timedelta(hours=-7))


def person(uuid, name, display=""):
    return NS(uuid=uuid, name=name, display_name=display)


def fake(uuid="A", people=(), place=True, **kw):
    base = dict(
        uuid=uuid, date=datetime(2012, 6, 23, 14, 53, tzinfo=PDT), tzoffset=-25200,
        date_added=datetime(2012, 6, 24, tzinfo=timezone.utc), date_modified=None, ismovie=False,
        screenshot=False, selfie=False, favorite=True, hidden=False, intrash=False, shared=False, syndicated=False,
        latitude=37.863, longitude=-122.3147, original_filename="IMG_4558.JPG", uti="public.jpeg", width=1936,
        height=2592, title=None, description=None, albums=["Walsh Visit"], keywords=["Flagged"],
        labels=["People", "Play"], person_info=list(people),
        exif_info=NS(camera_make="Apple", camera_model="iPhone 4"),
        place=NS(name="Adventure Playground, Berkeley", address_str="160 University Ave, Berkeley, CA",
                 names=NS(city=["Berkeley", "Berkeley"]), country_code="US") if place else None,
    )
    base.update(kw)
    return NS(**base)


def test_photo_row():
    row, named = photo_row(fake(people=[person("p1", "Ann", "Annie"), person("p2", "_UNKNOWN_"), person("p3", None)]))
    assert (row.kind, row.city, row.country, row.camera, row.faces) == ("photo", "Berkeley", "US", "Apple iPhone 4", 3)
    assert row.taken_at.utcoffset() == timedelta(hours=-7) and row.labels == ["People", "Play"]
    assert named == [("p1", "Ann", "Annie")]
    row, _ = photo_row(fake(place=False, ismovie=True, exif_info=NS(camera_make=None, camera_model=None)))
    assert (row.kind, row.place, row.city, row.camera) == ("video", "", "", "")


def test_store_upserts_links_and_marks_deleted():
    loader = PhotosLoader()
    ann, bob = person("p1", "Ann"), person("p2", "Bob")
    assert loader.store([fake("A", [ann, bob]), fake("B", [ann])]) == 2
    assert Photo.objects.get(uuid="A").people.count() == 2 and Person.objects.count() == 2
    # Next run: B gone, A lost Bob and was un-favorited, Ann renamed.
    loader.store([fake("A", [person("p1", "Ann K")], favorite=False)])
    a, b = Photo.objects.get(uuid="A"), Photo.objects.get(uuid="B")
    assert [p.name for p in a.people.all()] == ["Ann K"] and a.favorite is False and a.deleted_at is None
    assert b.deleted_at is not None and b.people.count() == 0
    # It comes back.
    loader.store([fake("A"), fake("B")])
    assert Photo.objects.get(uuid="B").deleted_at is None


def test_disabled_without_library(settings, tmp_path):
    settings.PHOTOS_LIBRARY = tmp_path / "missing.photoslibrary"
    run = run_loader(PhotosLoader())
    assert run.ok and run.message.startswith("disabled:")
