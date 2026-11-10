from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone

import pytest
from django.utils import timezone

from derive.geo import GeoLoader, haversine_km, homes, weighted_median
from derive.models import PlaceDay, Trip
from sources.health.models import Workout, WorkoutRoutePoint
from sources.location.models import LocationPoint, Visit
from sources.photos.models import Photo

pytestmark = pytest.mark.django_db
HOME = (37.44, -122.16)  # Palo Alto-ish
FAR = (48.86, 2.35)  # Paris


def at(d, hour=12):
    return timezone.make_aware(datetime(d.year, d.month, d.day, hour))


def photo(uuid, d, latlon, city="", country="", off=None, hour=12):
    return Photo.objects.create(uuid=uuid, taken_at=at(d, hour), tz_offset_s=off, kind="photo", lat=latlon[0],
                                lon=latlon[1], city=city, country=country)


def test_helpers():
    assert round(haversine_km(HOME, FAR)) == 8972
    assert weighted_median([(1, 1), (2, 1), (10, 5)]) == 10
    assert weighted_median([(1, 3), (2, 1), (10, 1)]) == 1
    days = {date(2026, 1, i): HOME for i in range(1, 25)} | {date(2026, 2, 1): FAR}
    assert homes(days)[2026] == (37.44, -122.16)


def test_home_override(settings):
    settings.HOME_LATLON = "1.5,2.5"
    assert homes({})[1999] == (1.5, 2.5)


def test_load_days_and_trip(settings):
    settings.HOME_LATLON = f"{HOME[0]},{HOME[1]}"
    d0 = date(2026, 5, 1)
    photo("h1", d0, HOME, "Palo Alto", "US")
    # Paris for three days, with an empty day in the middle (gap allowed), then home.
    photo("p1", d0 + timedelta(days=1), FAR, "Paris", "FR", off=3600)
    photo("p2", d0 + timedelta(days=1), FAR, "Paris", "FR", off=3600)
    Visit.objects.create(source="timeline-ios", start=at(d0 + timedelta(days=3)), end=at(d0 + timedelta(days=3), 18),
                         lat=FAR[0], lon=FAR[1])
    photo("h2", d0 + timedelta(days=5), HOME)
    # A day trip: one away day doesn't make a Trip.
    w = Workout.objects.create(external_id="w", name="Hike", start=at(d0 + timedelta(days=8)), route_points=2)
    for i in range(2):
        WorkoutRoutePoint.objects.create(workout=w, at=w.start + timedelta(minutes=i), lat=38.0, lon=-122.9)
    # Overland points: sampled one per 10 minutes.
    for m in range(0, 30):
        LocationPoint.objects.create(device_id="phone", at=at(d0 + timedelta(days=9)) + timedelta(minutes=m),
                                     lat=HOME[0], lon=HOME[1])
    assert GeoLoader().load() == 6
    days = {pd.day: pd for pd in PlaceDay.objects.all()}
    paris = days[d0 + timedelta(days=1)]
    assert paris.away and paris.city == "Paris" and paris.country == "FR" and paris.sources == {"photos": 2}
    assert not days[d0].away and days[d0].home_km == 0
    assert days[d0 + timedelta(days=8)].away and days[d0 + timedelta(days=8)].trip is None
    assert days[d0 + timedelta(days=9)].sources == {"points": 3}
    t = Trip.objects.get()
    assert (t.start, t.end, t.days, t.label, t.countries) == (d0 + timedelta(days=1), d0 + timedelta(days=3), 3,
                                                              "Paris", ["FR"])
    assert paris.trip_id == t.id and round(t.max_km) == 8972
    # Rebuild is idempotent.
    GeoLoader().load()
    assert PlaceDay.objects.count() == 6 and Trip.objects.count() == 1


def test_photo_day_uses_its_own_offset(settings):
    settings.HOME_LATLON = f"{HOME[0]},{HOME[1]}"
    # 23:00 UTC on May 1 is May 2 01:00 in Paris (UTC+2): the photo belongs to May 2.
    Photo.objects.create(uuid="x", taken_at=datetime(2026, 5, 1, 23, tzinfo=dt_timezone.utc), tz_offset_s=7200,
                         kind="photo", lat=FAR[0], lon=FAR[1])
    GeoLoader().load()
    assert PlaceDay.objects.get().day == date(2026, 5, 2)


def test_zero_zero_ignored():
    photo("z", date(2026, 5, 1), (0, 0))
    GeoLoader().load()
    assert PlaceDay.objects.count() == 0
