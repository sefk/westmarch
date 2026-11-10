from datetime import date, datetime, timedelta

import pytest
from django.http import QueryDict
from django.utils import timezone

from catalog import workouts as wk
from sources.health.models import Workout, WorkoutRoutePoint

pytestmark = pytest.mark.django_db
TODAY = date(2026, 11, 9)


def at(d, h=9):
    return timezone.make_aware(datetime(d.year, d.month, d.day, h))


def q(s):
    return QueryDict(s)


@pytest.fixture
def data():
    def w(ext, name, d, mins, dist=None, units="km", route=0, hr=None):
        o = Workout.objects.create(external_id=ext, name=name, start=at(d), duration_seconds=mins * 60, distance=dist,
                                   distance_units=units, route_points=route, avg_heart_rate=hr, active_energy_kcal=100)
        for i in range(route):
            WorkoutRoutePoint.objects.create(workout=o, at=o.start + timedelta(seconds=i), lat=37 + i / 1000, lon=-122)
        return o

    return {
        "hike": w("h", "Hiking", TODAY - timedelta(days=1), 120, 6.0, route=400, hr=110),
        "walk": w("w", "Outdoor Walk", TODAY - timedelta(days=40), 40, 2.0, units="mi", route=3, hr=100),
        "lift": w("l", "Functional Strength Training", TODAY - timedelta(days=2), 30),
        "old": w("o", "Hiking", date(2024, 5, 1), 90, 5.0, route=2),
    }


def test_filters_parse():
    f = wk.Filters.from_request(q("range=30d"), today=TODAY)
    assert f.preset == "30d" and f.start == date(2026, 10, 11) and f.end is None
    f = wk.Filters.from_request(q("from=2026-11-05&to=2026-01-01&type=Hiking&type=Yoga&route=yes&min=30&sort=hr&page=2"),
                                today=TODAY)
    assert (f.preset, f.start, f.end) == ("custom", date(2026, 1, 1), date(2026, 11, 5))  # swapped
    assert f.types == ["Hiking", "Yoga"] and f.route == "yes" and f.min_minutes == 30 and f.sort == "hr" and f.page == 2
    f = wk.Filters.from_request(q("range=bogus&route=maybe&sort=x&page=-3&min=abc"), today=TODAY)
    assert (f.preset, f.route, f.sort, f.page, f.min_minutes) == ("1y", "any", "date", 1, None)


def test_queryset_filters(data):
    names = lambda s: sorted(w.external_id for w in wk.Filters.from_request(q(s), today=TODAY).queryset())  # noqa: E731
    assert names("range=all") == ["h", "l", "o", "w"]
    assert names("range=30d") == ["h", "l"]
    assert names("range=all&type=Hiking") == ["h", "o"]
    assert names("range=all&route=no") == ["l"]
    assert names("range=all&min=60") == ["h", "o"]
    assert names("from=2024-01-01&to=2024-12-31") == ["o"]


def test_summary_converts_miles(data):
    s = wk.summary(wk.Filters.from_request(q("range=1y"), today=TODAY).queryset())
    assert s["count"] == 3 and s["routes"] == 2 and s["time"] == "3h 10m"
    assert round(s["km"], 2) == round(6.0 + 2.0 * 1.609344, 2)


def test_query_roundtrip():
    f = wk.Filters.from_request(q("from=2026-01-01&to=2026-02-01&type=Hiking&min=30"), today=TODAY)
    again = wk.Filters.from_request(QueryDict(f.query(sort="kcal")), today=TODAY)
    assert (again.start, again.end, again.types, again.min_minutes, again.sort) == (
        date(2026, 1, 1), date(2026, 2, 1), ["Hiking"], 30, "kcal")
    preset = QueryDict(f.query(range="90d", **{"from": "", "to": ""}))
    assert wk.Filters.from_request(preset, today=TODAY).preset == "90d"


def test_chart_and_routes(data):
    f = wk.Filters.from_request(q("range=all"), today=TODAY)
    c = wk.chart(f, today=TODAY)
    assert c["bucket"] == "month" and c["x"][0] == "2024-05-01"  # spans > 2 years
    assert wk.chart(wk.Filters.from_request(q("range=1y"), today=TODAY), today=TODAY)["bucket"] == "week"
    assert {t["name"] for t in c["types"]} == {"Hiking", "Outdoor Walk", "Functional Strength Training"}
    assert sum(sum(t["minutes"]) for t in c["types"]) == 120 + 40 + 30 + 90
    r = wk.routes(f)
    assert [x["id"] for x in r["routes"]] == [data["hike"].id, data["walk"].id, data["old"].id]
    hike = r["routes"][0]
    assert len(hike["lat"]) <= wk.POINTS_PER_ROUTE + 2 and hike["lat"][-1] == round(37 + 399 / 1000, 5)
    assert r["truncated"] is False


def test_pages(client, data):
    r = client.get("/body/workouts/?range=all&sort=distance")
    body = r.content.decode()
    assert r.status_code == 200 and "Hiking" in body and "All 4" in body
    assert body.index("6.00 km") < body.index("3.22 km")  # sorted by distance
    d = client.get("/body/workouts/data/?range=all&type=Hiking").json()
    assert len(d["routes"]) == 2 and d["chart"]["types"][0]["name"] == "Hiking"
    assert client.get("/body/workouts/?range=all&type=Nope").status_code == 200
    from sources.health.rollup import rebuild

    rebuild({TODAY - timedelta(days=1)})  # ingest keeps HealthDay current; here it's built by hand
    assert b"Browse and filter all workouts" in client.get("/body/?range=1y").content
