"""Photos loader: a daily full refresh of library metadata via osxphotos.

Reading all ~100k assets takes about 20 seconds, so every run rereads
everything; there is no cursor to drift. Pixels are never read.
"""

from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.loaders import Disabled, Loader

from .models import Person, Photo

CHUNK = 1000
UNKNOWN = "_UNKNOWN_"
FIELDS = ["taken_at", "tz_offset_s", "added_at", "modified_at", "kind", "screenshot", "selfie", "favorite", "hidden",
          "in_trash", "shared", "syndicated", "lat", "lon", "place", "address", "city", "country", "original_filename",
          "uti", "width", "height", "camera", "title", "description", "albums", "keywords", "labels", "faces",
          "deleted_at"]


def _s(v, n):
    return str(v or "")[:n]


def photo_row(p) -> tuple[Photo, list[tuple[str, str, str]]]:
    """An osxphotos PhotoInfo -> (unsaved Photo, [(person uuid, name, display name)]) for named people."""
    place = p.place
    city = ""
    if place is not None and getattr(place, "names", None) is not None and place.names.city:
        city = place.names.city[0]
    exif = p.exif_info
    camera = " ".join(x for x in (getattr(exif, "camera_make", None), getattr(exif, "camera_model", None)) if x)
    people = [(pi.uuid, pi.name, pi.display_name or "") for pi in p.person_info if pi.name and pi.name != UNKNOWN]
    row = Photo(
        uuid=p.uuid,
        taken_at=p.date,
        tz_offset_s=p.tzoffset,
        added_at=p.date_added,
        modified_at=p.date_modified,
        kind="video" if p.ismovie else "photo",
        screenshot=bool(p.screenshot),
        selfie=bool(p.selfie),
        favorite=bool(p.favorite),
        hidden=bool(p.hidden),
        in_trash=bool(p.intrash),
        shared=bool(p.shared),
        syndicated=bool(p.syndicated),
        lat=p.latitude,
        lon=p.longitude,
        place=_s(place.name if place else "", 300),
        address=_s(place.address_str if place else "", 500),
        city=_s(city, 100),
        country=_s(place.country_code if place else "", 10),
        original_filename=_s(p.original_filename, 300),
        uti=_s(p.uti, 100),
        width=p.width,
        height=p.height,
        camera=_s(camera, 200),
        title=_s(p.title, 500),
        description=p.description or "",
        albums=list(p.albums or []),
        keywords=list(p.keywords or []),
        labels=list(p.labels or []),
        faces=len(p.person_info),
        deleted_at=None,
    )
    return row, people


class PhotosLoader(Loader):
    name = "photos"
    cadence = "daily"

    def open_db(self):
        lib = Path(settings.PHOTOS_LIBRARY)
        if not lib.exists():
            raise Disabled(f"{lib} not found (volume not mounted?)")
        import osxphotos  # heavy import; only when the library is there

        return osxphotos.PhotosDB(dbfile=str(lib))

    def load(self, full: bool = False) -> int:
        db = self.open_db()
        # photos() is everything outside Recently Deleted (hidden, shared and
        # syndicated included); intrash=True is only what's in it.
        return self.store(db.photos() + db.photos(intrash=True))

    @transaction.atomic
    def store(self, photos) -> int:
        """Upsert the whole library; anything stored but absent is marked deleted."""
        rows, links, people = {}, [], {}
        for p in photos:
            row, named = photo_row(p)
            rows[row.uuid] = row
            for uuid, name, display in named:
                people[uuid] = (name, display)
                links.append((row.uuid, uuid))
        for uuid, (name, display) in people.items():
            Person.objects.update_or_create(uuid=uuid, defaults={"name": name[:200], "display_name": display[:200]})
        batch = list(rows.values())
        for i in range(0, len(batch), CHUNK):
            Photo.objects.bulk_create(batch[i : i + CHUNK], update_conflicts=True, unique_fields=["uuid"],
                                      update_fields=FIELDS)
        photo_ids = dict(Photo.objects.values_list("uuid", "id"))
        person_ids = dict(Person.objects.values_list("uuid", "id"))
        # `photos` is the whole library, so the links are replaced wholesale.
        through = Photo.people.through
        through.objects.all().delete()
        pairs = {(photo_ids[ph], person_ids[pe]) for ph, pe in links}
        objs = [through(photo_id=a, person_id=b) for a, b in pairs]
        for i in range(0, len(objs), CHUNK):
            through.objects.bulk_create(objs[i : i + CHUNK])
        gone = [pid for uuid, pid in photo_ids.items() if uuid not in rows]
        now = timezone.now()
        for i in range(0, len(gone), CHUNK):
            Photo.objects.filter(id__in=gone[i : i + CHUNK], deleted_at__isnull=True).update(deleted_at=now)
        self.state_set("photos", str(len(rows)))
        self.state_set("people", str(len(people)))
        return len(rows)
