"""
Data processing - extracts module-level time data from the Nexus API
response, filters outliers, and aggregates across users.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Minimum time_spent (minutes) to count as a valid data point.
MIN_VALID_TIME = 5


@dataclass
class ModuleInfo:
    """Holds aggregated time data for a single module across users."""

    module_id: int
    module_name: str
    recommended_minutes: float  # sum of exercise durations
    module_duration: float  # module-level "duration" field
    user_times: list[float] = field(default_factory=list)  # valid user_time_spent values

    # Individual exercise breakdown (name → recommended duration)
    exercises: dict[str, float] = field(default_factory=dict)

    @property
    def avg_actual(self) -> float | None:
        return round(statistics.mean(self.user_times), 1) if self.user_times else None

    @property
    def median_actual(self) -> float | None:
        return round(statistics.median(self.user_times), 1) if self.user_times else None

    @property
    def min_actual(self) -> float | None:
        return round(min(self.user_times), 1) if self.user_times else None

    @property
    def max_actual(self) -> float | None:
        return round(max(self.user_times), 1) if self.user_times else None

    @property
    def user_count(self) -> int:
        return len(self.user_times)

    @property
    def deviation(self) -> float | None:
        """Absolute deviation in minutes (median actual − recommended).

        The median is used rather than the mean: a handful of learners who
        leave a module open for hours would otherwise drag the reference up.
        """
        if self.median_actual is None or self.recommended_minutes == 0:
            return None
        return round(self.median_actual - self.recommended_minutes, 1)

    @property
    def deviation_pct(self) -> float | None:
        """Percentage deviation from recommended, based on the median."""
        if self.median_actual is None or self.recommended_minutes == 0:
            return None
        return round(
            ((self.median_actual - self.recommended_minutes) / self.recommended_minutes) * 100,
            1,
        )

    def to_dict(self) -> dict:
        return {
            "module_id": self.module_id,
            "module_name": self.module_name,
            "recommended_minutes": self.recommended_minutes,
            "module_duration": self.module_duration,
            "avg_actual": self.avg_actual,
            "median_actual": self.median_actual,
            "min_actual": self.min_actual,
            "max_actual": self.max_actual,
            "user_count": self.user_count,
            "deviation": self.deviation,
            "deviation_pct": self.deviation_pct,
            "exercises": self.exercises,
        }


def extract_modules_from_lessons(lessons_data) -> list[dict]:
    """
    Given the raw JSON from GET /users/{id}/lessons, extract module-level info.

    The API can return:
      - A wrapper dict: {"sprints": [sprint1, sprint2, ...], "user_modules": [...]}
      - A single sprint dict (has "modules" key directly)
      - A list of sprint dicts

    Returns a list of dicts:
        {module_id, module_name, user_time_spent, recommended_minutes,
         module_duration, exercises: {name: duration}}
    """
    results = []

    # Normalise input to a list of sprint dicts
    if isinstance(lessons_data, dict):
        if "sprints" in lessons_data:
            # Wrapper format: {"sprints": [...], "user_modules": [...]}
            sprints = lessons_data["sprints"]
            log.info("extract_modules: unwrapped 'sprints' key → %d sprint(s)", len(sprints))
        elif "modules" in lessons_data:
            # Single bare sprint dict
            sprints = [lessons_data]
        else:
            log.warning("extract_modules: dict with unexpected keys: %s", list(lessons_data.keys()))
            sprints = [lessons_data]
    elif isinstance(lessons_data, list):
        sprints = lessons_data
    else:
        return results

    for sprint in sprints:
        modules = sprint.get("modules", [])
        for mod_wrapper in modules:
            module = mod_wrapper.get("module", {})
            if not module:
                continue

            module_id = module.get("id")
            module_name = module.get("name", "Unknown")
            user_time_spent = module.get("user_time_spent", 0)
            module_duration = module.get("duration", 0)

            # Sum individual exercise durations as "recommended"
            exercises_raw = module.get("exercises", [])
            exercise_breakdown = {}
            total_exercise_duration = 0
            for ex in exercises_raw:
                ex_name = ex.get("name", "Unnamed")
                ex_dur = ex.get("duration", 0)
                exercise_breakdown[ex_name] = ex_dur
                total_exercise_duration += ex_dur

            results.append(
                {
                    "module_id": module_id,
                    "module_name": module_name,
                    "user_time_spent": user_time_spent or 0,
                    "recommended_minutes": total_exercise_duration,
                    "module_duration": module_duration,
                    "exercises": exercise_breakdown,
                }
            )

    log.info("extract_modules: extracted %d module(s) total", len(results))
    return results


class DataPool:
    """
    Manages the aggregated data pool across multiple cohorts.
    Thread-safe enough for a single-user Flask app.
    """

    def __init__(self):
        # module_id → ModuleInfo
        self.modules: dict[int, ModuleInfo] = {}
        # cohort_id → {user_ids: set, name: str}
        self.cohorts: dict[int, dict] = {}
        # Set of user IDs already processed (for deduplication)
        self.processed_users: set[int] = set()

    def add_user_data(self, user_id: int, modules_data: list[dict]):
        """
        Merge one user's module data into the pool.
        Skips if user was already processed.
        """
        if user_id in self.processed_users:
            return
        self.processed_users.add(user_id)

        log.info("Adding data for user %s: %d modules", user_id, len(modules_data))

        for mod in modules_data:
            mid = mod["module_id"]
            if mid not in self.modules:
                self.modules[mid] = ModuleInfo(
                    module_id=mid,
                    module_name=mod["module_name"],
                    recommended_minutes=mod["recommended_minutes"],
                    module_duration=mod["module_duration"],
                    exercises=mod["exercises"],
                )

            # Only add if time_spent is above the outlier threshold
            time_spent = mod["user_time_spent"]
            if time_spent > MIN_VALID_TIME:
                self.modules[mid].user_times.append(time_spent)
                log.debug("  module %s: user_time_spent=%s (added)", mid, time_spent)
            else:
                log.debug("  module %s: user_time_spent=%s (below threshold)", mid, time_spent)

    def register_cohort(self, cohort_id: int, user_ids: list[int]):
        """Track which cohorts have been loaded."""
        if cohort_id not in self.cohorts:
            self.cohorts[cohort_id] = {"user_ids": set(), "user_count": 0}
        self.cohorts[cohort_id]["user_ids"].update(user_ids)
        self.cohorts[cohort_id]["user_count"] = len(self.cohorts[cohort_id]["user_ids"])

    def get_aggregated_data(self) -> list[dict]:
        """Return all modules sorted by absolute deviation (desc).
        Includes modules with no valid user time data so the recommended
        time is always visible."""
        result = []
        for mod in self.modules.values():
            result.append(mod.to_dict())
        # Sort: modules with deviation data first (by abs deviation desc),
        # then modules with no data (by name)
        result.sort(
            key=lambda x: (
                0 if x["deviation"] is not None else 1,
                -(abs(x["deviation"]) if x["deviation"] is not None else 0),
            )
        )
        log.info("get_aggregated_data: %d total modules, %d with user data",
                 len(result), sum(1 for r in result if r["user_count"] > 0))
        return result

    def get_summary(self) -> dict:
        """Dashboard summary stats."""
        data = self.get_aggregated_data()
        total_modules = len(data)
        total_users = len(self.processed_users)
        total_cohorts = len(self.cohorts)

        deviations = [abs(d["deviation_pct"]) for d in data if d["deviation_pct"] is not None]
        avg_deviation = round(statistics.mean(deviations), 1) if deviations else 0
        needs_recalibration = sum(1 for d in deviations if d > 30)

        return {
            "total_modules": total_modules,
            "total_users": total_users,
            "total_cohorts": total_cohorts,
            "avg_deviation_pct": avg_deviation,
            "needs_recalibration": needs_recalibration,
        }

    def remove_module(self, module_id: int):
        """Remove a module from the pool entirely."""
        if module_id in self.modules:
            del self.modules[module_id]

    def get_cohorts_info(self) -> list[dict]:
        """Return list of loaded cohorts with user counts."""
        return [
            {"cohort_id": cid, "user_count": info["user_count"]}
            for cid, info in self.cohorts.items()
        ]

    def clear(self):
        self.modules.clear()
        self.cohorts.clear()
        self.processed_users.clear()
