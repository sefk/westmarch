"""The geo loader: where was I each day, and which runs of days were trips.

Rebuilds PlaceDay and Trip from scratch every run (cheap: tens of
thousands of located things). Inputs, each a weighted vote for the day:

- photos with GPS (weight 1 each), dated in the time zone they were taken in
- outdoor workouts with a route (the route's centroid, weight 5)
- Google Timeline visits (weight = hours there, 0.25 to 12)
- GPS points from Overland or Timeline paths, one per 10 minutes (weight 1)

A day's position is the weighted median latitude and longitude. Home is
HOME_LATLON if set, else per year the ~1 km cell most days are spent in
(falling back to the all-time one for thin years). A day is away when its
position is more than AWAY_KM from home; runs of away days at least
MIN_TRIP_DAYS long are Trips.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import date, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Avg
from django.utils import timezone

from core.loaders import Loader
from sources.health.models import Workout, WorkoutRoutePoint
from sources.location.models import LocationPoint, Visit
from sources.photos.models import Photo

from .models import PlaceDay, Trip

AWAY_KM = 40
TRIP_GAP_DAYS = 2  # days without data inside a trip that don't end it
MIN_TRIP_DAYS = 2  # a single away day is a day trip: away, but not a Trip
MIN_HOME_DAYS = 20  # days a year needs before it gets its own home
SAMPLE_MINUTES = 10


def haversine_km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def weighted_median(pairs):
    """[(value, weight)] -> the value at half the total weight."""
    pairs = sorted(pairs)
    half = sum(w for _, w in pairs) / 2
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= half:
            return v
    return pairs[-1][0]


def local_day(dt):
    return timezone.localtime(dt).date()


def collect():
    """day -> [(lat, lon, weight, source, city, country)]"""
    votes = defaultdict(list)
    photos = Photo.objects.filter(lat__isnull=False, deleted_at__isnull=True).values_list(
        "taken_at", "tz_offset_s", "lat", "lon", "city", "country")
    for at, off, lat, lon, city, country in photos.iterator(chunk_size=5000):
        if lat == 0 and lon == 0:
            continue  # "no location" written as 0,0
        d = (at + timedelta(seconds=off)).date() if off is not None else local_day(at)
        votes[d].append((lat, lon, 1.0, "photos", city, country))

    starts = dict(Workout.objects.filter(route_points__gt=0).values_list("id", "start"))
    for row in WorkoutRoutePoint.objects.values("workout_id").annotate(lat=Avg("lat"), lon=Avg("lon")):
        if row["workout_id"] in starts:
            votes[local_day(starts[row["workout_id"]])].append((row["lat"], row["lon"], 5.0, "routes", "", ""))

    for start, end, lat, lon in Visit.objects.filter(lat__isnull=False).values_list("start", "end", "lat", "lon"):
        hours = ((end - start).total_seconds() / 3600) if end else 1
        votes[local_day(start)].append((lat, lon, min(12.0, max(0.25, hours)), "visits", "", ""))

    seen = set()
    for at, lat, lon in LocationPoint.objects.order_by("at").values_list("at", "lat", "lon").iterator(chunk_size=10000):
        slot = int(at.timestamp() // (SAMPLE_MINUTES * 60))
        if slot in seen:
            continue
        seen.add(slot)
        votes[local_day(at)].append((lat, lon, 1.0, "points", "", ""))
    return votes


def cell(lat, lon):
    return (round(lat, 2), round(lon, 2))


def homes(primaries: dict[date, tuple[float, float]]):
    """year -> home (lat, lon). HOME_LATLON wins; else the most common
    ~1 km cell per year, or all-time for years with few located days."""
    fixed = getattr(settings, "HOME_LATLON", "")
    if fixed:
        lat, lon = (float(x) for x in fixed.split(","))
        return defaultdict(lambda: (lat, lon))
    if not primaries:
        return {}
    overall = Counter(cell(*p) for p in primaries.values()).most_common(1)[0][0]
    by_year = defaultdict(Counter)
    for d, p in primaries.items():
        by_year[d.year][cell(*p)] += 1
    out = defaultdict(lambda: overall)
    for y, c in by_year.items():
        top, n = c.most_common(1)[0]
        out[y] = top if sum(c.values()) >= MIN_HOME_DAYS else overall
    return out


def build(votes):
    primaries, labels, counts = {}, {}, {}
    for d, vs in votes.items():
        primaries[d] = (weighted_median([(v[0], v[2]) for v in vs]), weighted_median([(v[1], v[2]) for v in vs]))
        named = Counter((v[4], v[5]) for v in vs if v[4] or v[5])
        labels[d] = named.most_common(1)[0][0] if named else ("", "")
        counts[d] = dict(Counter(v[3] for v in vs))
    home = homes(primaries)
    days = []
    for d in sorted(primaries):
        h = home[d.year]
        km = haversine_km(primaries[d], h)
        far = max(haversine_km((v[0], v[1]), h) for v in votes[d])
        days.append(PlaceDay(day=d, lat=primaries[d][0], lon=primaries[d][1], city=labels[d][0][:100],
                             country=labels[d][1][:10], home_km=round(km, 2), furthest_km=round(far, 2),
                             away=km > AWAY_KM, sources=counts[d]))
    return days


def trips(days: list[PlaceDay]) -> list[tuple[Trip, list[PlaceDay]]]:
    out, cur = [], []
    for pd in days:
        if not pd.away:
            if cur:
                out.append(cur)
                cur = []
            continue
        if cur and (pd.day - cur[-1].day).days > TRIP_GAP_DAYS + 1:
            out.append(cur)
            cur = []
        cur.append(pd)
    if cur:
        out.append(cur)
    result = []
    for run in out:
        if (run[-1].day - run[0].day).days + 1 < MIN_TRIP_DAYS:
            continue
        cities = Counter(pd.city for pd in run if pd.city)
        label = cities.most_common(1)[0][0] if cities else f"{run[0].lat:.2f}, {run[0].lon:.2f}"
        countries = sorted({pd.country for pd in run if pd.country})
        t = Trip(start=run[0].day, end=run[-1].day, days=(run[-1].day - run[0].day).days + 1, label=label[:200],
                 countries=countries, max_km=round(max(pd.furthest_km for pd in run), 1))
        result.append((t, run))
    return result


class GeoLoader(Loader):
    name = "geo"
    cadence = "daily"

    @transaction.atomic
    def load(self, full: bool = False) -> int:
        days = build(collect())
        PlaceDay.objects.all().delete()
        Trip.objects.all().delete()
        for t, run in trips(days):
            t.save()
            for pd in run:
                pd.trip = t
        PlaceDay.objects.bulk_create(days, batch_size=1000)
        self.state_set("trips", str(Trip.objects.count()))
        return len(days)
