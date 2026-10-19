"""Campaign test scenarios, in the order an episode runs them."""
from __future__ import annotations

from sim.campaign.episode import Scenario
from sim.campaign.scenarios import approach, follow, hazard, orbit, perception, rth, scan, selection

SCENARIOS: dict[str, Scenario] = {
    "perception": perception.run,
    "select": selection.run,
    "approach": approach.run,
    "orbit": orbit.run,
    "scan": scan.run,
    "follow": follow.run,
    "rth": rth.run,
    "hazard": hazard.run,
}
