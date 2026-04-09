"""
Win probability model for esports matches.

Combines multiple signal components into a single probability estimate for
team A winning a given match. The model is designed to exploit the
information asymmetry between deep-scene analysts and casual Kalshi traders.

Component weights (tuned heuristically, recalibrate with calibrate()):
  1. Ranking differential        20%
  2. Recent form (weighted)      25%
  3. Head-to-head record         15%
  4. Map pool advantage          15%
  5. Player rating differential  15%
  6. Roster stability            10%

Format adjustment (Bo1 = higher variance, Bo5 = skill dominates) scales
uncertainty toward 0.5 for Bo1 and away from 0.5 for Bo3/Bo5.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

from botd.config import FORMAT_UPSET_SCALE

logger = logging.getLogger(__name__)


@dataclass
class MatchFeatures:
    """All numeric features for a single match prediction."""
    # Team identifiers
    team1_id: str
    team2_id: str
    game: str                       # 'cs2' or 'val'
    match_format: str               # 'Bo1', 'Bo3', 'Bo5'
    tournament_tier: str            # 'S', 'A', 'B', 'C'

    # Rankings (lower = better)
    ranking1: Optional[int] = None
    ranking2: Optional[int] = None

    # Recent form (weighted win rate 0-1)
    form1: float = 0.5
    form2: float = 0.5
    form1_n: int = 0
    form2_n: int = 0

    # H2H
    h2h_wins1: int = 0
    h2h_wins2: int = 0
    h2h_total: int = 0

    # Map pool win rate (average across shared maps)
    map_wr1: float = 0.5
    map_wr2: float = 0.5

    # Average player rating
    avg_rating1: float = 1.0        # HLTV Rating 2.0 baseline = 1.0
    avg_rating2: float = 1.0        # or average ACS for Valorant

    # Roster stability (1.0 = no changes in 30 days, decreases per change)
    stability1: float = 1.0
    stability2: float = 1.0

    # Derived fields (filled by model)
    components: dict = field(default_factory=dict)


@dataclass
class PredictionResult:
    p_team1: float                  # probability team1 wins
    p_team2: float                  # = 1 - p_team1
    confidence: float               # 0-1 confidence in prediction
    features: MatchFeatures = None
    reasoning: str = ""

    @property
    def dominant_team(self) -> int:
        return 1 if self.p_team1 > self.p_team2 else 2

    @property
    def edge_magnitude(self) -> float:
        return abs(self.p_team1 - 0.5)


class EsportsProbabilityModel:
    """
    Converts match features into win probability estimates.

    Component weights and the calibration exponent can be adjusted
    via recalibrate() once we have a history of predictions vs outcomes.
    """

    # Component weights — must sum to 1.0
    WEIGHTS = {
        "ranking": 0.20,
        "form": 0.25,
        "h2h": 0.15,
        "map_pool": 0.15,
        "player_rating": 0.15,
        "roster_stability": 0.10,
    }

    # Minimum sample sizes before a component is trusted
    MIN_FORM_MATCHES = 3
    MIN_H2H_MATCHES = 2
    MIN_MAP_MATCHES = 5

    def predict(self, features: MatchFeatures) -> PredictionResult:
        """Compute win probability for team1 given the feature set."""
        components = {}

        # -------------------------------------------------------
        # 1. Ranking component (Elo-like sigmoid)
        # -------------------------------------------------------
        if features.ranking1 and features.ranking2:
            rank_delta = features.ranking2 - features.ranking1  # positive = team1 better
            # Sigmoid scaled so rank delta of 10 ≈ 0.67 probability
            p_rank = self._sigmoid(rank_delta, scale=8.0)
        else:
            # No ranking data → neutral
            p_rank = 0.5
        components["ranking"] = p_rank

        # -------------------------------------------------------
        # 2. Recent form component
        # -------------------------------------------------------
        if features.form1_n >= self.MIN_FORM_MATCHES and features.form2_n >= self.MIN_FORM_MATCHES:
            # Normalise both forms to a probability-space comparison
            f1 = features.form1
            f2 = features.form2
            total = f1 + f2
            p_form = f1 / total if total > 0 else 0.5
        elif features.form1_n >= self.MIN_FORM_MATCHES:
            p_form = features.form1
        elif features.form2_n >= self.MIN_FORM_MATCHES:
            p_form = 1.0 - features.form2
        else:
            p_form = 0.5
        components["form"] = p_form

        # -------------------------------------------------------
        # 3. H2H component
        # -------------------------------------------------------
        if features.h2h_total >= self.MIN_H2H_MATCHES:
            # Laplace smoothing to prevent extremes with small samples
            alpha = 1
            p_h2h = (features.h2h_wins1 + alpha) / (features.h2h_total + 2 * alpha)
        else:
            p_h2h = 0.5
        components["h2h"] = p_h2h

        # -------------------------------------------------------
        # 4. Map pool component
        # -------------------------------------------------------
        map_total = features.map_wr1 + features.map_wr2
        if map_total > 0:
            p_map = features.map_wr1 / map_total
        else:
            p_map = 0.5
        components["map_pool"] = p_map

        # -------------------------------------------------------
        # 5. Player rating component
        # -------------------------------------------------------
        r1 = max(features.avg_rating1, 0.1)
        r2 = max(features.avg_rating2, 0.1)
        # Rating 2.0: 1.0 is average; for ACS, ~220 is average
        # Normalise to same scale by just using ratio
        if r1 + r2 > 0:
            p_player = r1 / (r1 + r2)
        else:
            p_player = 0.5
        components["player_rating"] = p_player

        # -------------------------------------------------------
        # 6. Roster stability component
        # -------------------------------------------------------
        # A team with recent roster changes is less predictable (nerf toward 0.5)
        p_stability = 0.5 + (features.stability1 - features.stability2) * 0.15
        p_stability = max(0.3, min(0.7, p_stability))
        components["roster_stability"] = p_stability

        # -------------------------------------------------------
        # Weighted blend
        # -------------------------------------------------------
        p_raw = sum(
            self.WEIGHTS[k] * v for k, v in components.items()
        )
        p_raw = max(0.05, min(0.95, p_raw))  # hard clamp

        # -------------------------------------------------------
        # Format adjustment
        # -------------------------------------------------------
        p_adjusted = self._apply_format_adjustment(p_raw, features.match_format)

        # -------------------------------------------------------
        # Confidence — based on data completeness
        # -------------------------------------------------------
        confidence = self._compute_confidence(features)

        features.components = components

        reasoning = self._build_reasoning(features, components, p_adjusted)

        return PredictionResult(
            p_team1=round(p_adjusted, 4),
            p_team2=round(1.0 - p_adjusted, 4),
            confidence=round(confidence, 3),
            features=features,
            reasoning=reasoning,
        )

    def _sigmoid(self, x: float, scale: float = 10.0) -> float:
        """Sigmoid function: maps x in (-inf, +inf) to (0, 1)."""
        return 1.0 / (1.0 + math.exp(-x / scale))

    def _apply_format_adjustment(self, p: float, match_format: str) -> float:
        """
        Adjust probability toward 0.5 for high-variance formats (Bo1),
        and away from 0.5 for skill-dominant formats (Bo5).
        """
        scale = FORMAT_UPSET_SCALE.get(match_format, 1.0)
        if scale > 1.0:
            # Shrink toward 0.5 (Bo1: more variance)
            deviation = p - 0.5
            p_adj = 0.5 + deviation / scale
        elif scale < 1.0:
            # Stretch away from 0.5 (Bo5: skill dominates)
            deviation = p - 0.5
            p_adj = 0.5 + deviation / scale
        else:
            p_adj = p

        return max(0.05, min(0.95, p_adj))

    def _compute_confidence(self, features: MatchFeatures) -> float:
        """
        Confidence score 0-1 based on how much reliable data we have.
        Full confidence requires: rankings, 10+ form matches, 3+ H2H, player ratings.
        """
        score = 0.0
        if features.ranking1 and features.ranking2:
            score += 0.20
        if features.form1_n >= 5:
            score += 0.20
        if features.form2_n >= 5:
            score += 0.10
        if features.h2h_total >= self.MIN_H2H_MATCHES:
            score += 0.15
        if features.map_wr1 > 0 and features.map_wr2 > 0:
            score += 0.15
        if features.avg_rating1 != 1.0 and features.avg_rating2 != 1.0:
            score += 0.20
        return min(1.0, score)

    def _build_reasoning(
        self, features: MatchFeatures, components: dict, p_final: float
    ) -> str:
        lines = [
            f"Predicted P(team1 wins) = {p_final:.1%}  "
            f"[format={features.match_format}, tier={features.tournament_tier}]",
            "",
            "Signal components:",
        ]
        labels = {
            "ranking": "World ranking",
            "form": "Recent form (recency-weighted)",
            "h2h": "Head-to-head history",
            "map_pool": "Map pool advantage",
            "player_rating": "Avg player rating",
            "roster_stability": "Roster stability",
        }
        for k, v in components.items():
            weight = self.WEIGHTS[k]
            lines.append(f"  {labels.get(k, k):30s} => {v:.3f}  (weight {weight:.0%})")

        if features.h2h_total:
            lines.append(
                f"\nH2H: {features.h2h_wins1}-{features.h2h_wins2} in {features.h2h_total} matches"
            )
        if features.form1_n:
            lines.append(
                f"Form (team1): {features.form1:.1%} over {features.form1_n} matches"
            )
        if features.form2_n:
            lines.append(
                f"Form (team2): {features.form2:.1%} over {features.form2_n} matches"
            )
        return "\n".join(lines)


def compute_roster_stability(roster_changes: list[dict]) -> float:
    """
    Return a stability score 0-1 based on recent roster changes.
    0 changes in 30 days → 1.0
    Each change reduces score by 0.15, floored at 0.10.
    """
    n = len(roster_changes)
    return max(0.10, 1.0 - n * 0.15)


def build_cs2_features(
    match: dict, h2h: dict, form1: dict, form2: dict,
    map_stats1: list, map_stats2: list, players1: list, players2: list,
    roster_changes1: list, roster_changes2: list,
    team1: dict, team2: dict,
) -> MatchFeatures:
    """Build a MatchFeatures object from raw CS2 data dicts."""
    avg_map_wr1 = (
        sum(m["win_rate"] for m in map_stats1) / len(map_stats1) if map_stats1 else 0.5
    )
    avg_map_wr2 = (
        sum(m["win_rate"] for m in map_stats2) / len(map_stats2) if map_stats2 else 0.5
    )

    avg_r1 = (
        sum(p["rating"] for p in players1 if p.get("rating")) / len(players1)
        if players1 else 1.0
    )
    avg_r2 = (
        sum(p["rating"] for p in players2 if p.get("rating")) / len(players2)
        if players2 else 1.0
    )

    h2h_wins1 = h2h.get("team1_wins", 0) if h2h else 0
    h2h_wins2 = h2h.get("team2_wins", 0) if h2h else 0
    h2h_total = h2h.get("total_matches", 0) if h2h else 0

    return MatchFeatures(
        team1_id=match.get("team1_id", ""),
        team2_id=match.get("team2_id", ""),
        game="cs2",
        match_format=match.get("match_format", "Bo3"),
        tournament_tier=match.get("tournament_tier", "C"),
        ranking1=team1.get("ranking"),
        ranking2=team2.get("ranking"),
        form1=form1.get("weighted_wr", 0.5),
        form2=form2.get("weighted_wr", 0.5),
        form1_n=form1.get("n_matches", 0),
        form2_n=form2.get("n_matches", 0),
        h2h_wins1=h2h_wins1,
        h2h_wins2=h2h_wins2,
        h2h_total=h2h_total,
        map_wr1=avg_map_wr1,
        map_wr2=avg_map_wr2,
        avg_rating1=avg_r1,
        avg_rating2=avg_r2,
        stability1=compute_roster_stability(roster_changes1),
        stability2=compute_roster_stability(roster_changes2),
    )


def build_val_features(
    match: dict, h2h: dict, form1: dict, form2: dict,
    map_stats1: list, map_stats2: list, players1: list, players2: list,
    roster_changes1: list, roster_changes2: list,
    team1: dict, team2: dict,
) -> MatchFeatures:
    """Build a MatchFeatures object from raw Valorant data dicts."""
    avg_map_wr1 = (
        sum(m["win_rate"] for m in map_stats1) / len(map_stats1) if map_stats1 else 0.5
    )
    avg_map_wr2 = (
        sum(m["win_rate"] for m in map_stats2) / len(map_stats2) if map_stats2 else 0.5
    )

    # Valorant: use ACS, normalise to ~1.0 scale (average ACS ~220)
    avg_acs1 = (
        sum(p["acs"] for p in players1 if p.get("acs")) / len(players1)
        if players1 else 220.0
    )
    avg_acs2 = (
        sum(p["acs"] for p in players2 if p.get("acs")) / len(players2)
        if players2 else 220.0
    )

    h2h_wins1 = h2h.get("team1_wins", 0) if h2h else 0
    h2h_wins2 = h2h.get("team2_wins", 0) if h2h else 0
    h2h_total = h2h.get("total_matches", 0) if h2h else 0

    return MatchFeatures(
        team1_id=match.get("team1_id", ""),
        team2_id=match.get("team2_id", ""),
        game="val",
        match_format=match.get("match_format", "Bo3"),
        tournament_tier=match.get("tournament_tier", "C"),
        ranking1=team1.get("ranking"),
        ranking2=team2.get("ranking"),
        form1=form1.get("weighted_wr", 0.5),
        form2=form2.get("weighted_wr", 0.5),
        form1_n=form1.get("n_matches", 0),
        form2_n=form2.get("n_matches", 0),
        h2h_wins1=h2h_wins1,
        h2h_wins2=h2h_wins2,
        h2h_total=h2h_total,
        map_wr1=avg_map_wr1,
        map_wr2=avg_map_wr2,
        avg_rating1=avg_acs1 / 220.0,   # normalise to ~1.0 scale
        avg_rating2=avg_acs2 / 220.0,
        stability1=compute_roster_stability(roster_changes1),
        stability2=compute_roster_stability(roster_changes2),
    )
