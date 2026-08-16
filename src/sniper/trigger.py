"""
BinaryStar Sniper Trigger — Signal Stack Architecture.

Replaces the old binary-trigger model with a continuous multi-signal confluence
engine. 10 signal detectors are evaluated per pulse, each scored on
a 0–1 continuum, stacked directionally, and only fire an AI session when the
confluence score exceeds a regime-adaptive threshold.

See docs/trigger-design-20260625.md for the full design specification.
"""

import math
from collections import deque
from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Any, Tuple, Optional, List

from src.utils.logger_utils import setup_logger

logger = setup_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Enums
# ═══════════════════════════════════════════════════════════════════════════

class Direction(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


# ═══════════════════════════════════════════════════════════════════════════
# Data Structures
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class SignalCard:
    """A single detected market signal with continuous 0–1 scoring."""
    signal_id: str
    sub_type: str
    direction: Direction
    strength: float                         # 0.0–1.0
    weight: float                           # 0.0–1.0, detector weight
    timestamp: datetime
    decay_half_life_minutes: float
    evidence: Dict[str, Any] = field(default_factory=dict)

    @property
    def weighted_score(self) -> float:
        """Effective score used in confluence stacking."""
        result = self.strength * self.weight
        return 0.0 if math.isnan(result) or math.isinf(result) else result

    def decayed_strength(self, now: datetime) -> float:
        """Strength after applying temporal decay (half-life formula)."""
        elapsed = (now - self.timestamp).total_seconds() / 60.0
        if elapsed <= 0:
            return self.strength
        return self.strength * (0.5 ** (elapsed / max(self.decay_half_life_minutes, 1.0)))

@dataclass
class TriggerResult:
    """Output of trigger evaluation — replaces old (bool, str, str) tuple."""
    triggered: bool
    confluence_score: float                 # 0.0–1.0
    confluence_direction: Direction
    signals: List[SignalCard]               # all signals (including decayed survivors)
    active_signals: List[SignalCard]        # fresh signals that contributed to trigger
    gate_result: str                        # "PASS" | "FAIL"
    situation_brief: Optional[Dict[str, Any]]  # None if not triggered
    cooldown_minutes: float


@dataclass
class SignalMemory:
    """Tracks signals across pulses for decay and persistence."""
    active_signals: Dict[str, SignalCard] = field(default_factory=dict)

    def ingest(self, new_signals: List[SignalCard], now: datetime) -> List[SignalCard]:
        """Add new signals, decay existing ones, purge expired (strength < 0.05)."""
        decayed = []
        expired_keys = []
        for sid, card in self.active_signals.items():
            d_strength = card.decayed_strength(now)
            if d_strength > 0.05:
                card.strength = d_strength
                decayed.append(card)
            else:
                expired_keys.append(sid)

        for key in expired_keys:
            del self.active_signals[key]

        # Merge new signals (replace existing with same sub_type)
        for ns in new_signals:
            key = ns.sub_type
            self.active_signals[key] = ns

        # Return alive signals: fresh signals take priority over decayed survivors
        # (same sub_type in both → keep the fresh one, discard the stale one)
        fresh_keys = {ns.sub_type for ns in new_signals}
        return new_signals + [d for d in decayed if d.sub_type not in fresh_keys]


@dataclass
class StructuralFingerprint:
    """Snapshot of market structure at trigger time.

    Compared on subsequent cooldown break attempts.  If unchanged,
    the break is rejected to avoid re-triggering on the same market
    conditions that already produced a NEUTRAL or failed session.
    """
    vah: float
    val: float
    poc: float
    price: float
    atr_macro: float
    regime: str
    timestamp: datetime


# ═══════════════════════════════════════════════════════════════════════════
# Calibration Constants — detector strength saturation curves
# ═══════════════════════════════════════════════════════════════════════════
# These control how raw measurements map to 0–1 signal strengths.
# They are NOT config-driven because they encode the physical relationship
# between each measurement type and "how extreme is this?" — changing them
# would alter the signal stack's calibration, not its sensitivity.
# For sensitivity tuning, use config thresholds and weights instead.

# FLOW — CVD-based detectors saturate fast (CVD is noisy, needs aggressive scaling)
CVD_SATURATION_FACTOR = 3.0
CVD_ABSORPTION_SATURATION = 1.5
CVD_ABSORPTION_MIN_DENOM = 0.15

# SIZE — Z-score to strength mapping
LARGE_TRADE_SATURATION = 2.0
MIN_LARGE_TRADE_SAMPLES = 5

# ENERGY
VOLATILITY_SATURATION = 2.0
SQUEEZE_DECLINE_THRESHOLD = 0.98

# STRUCTURAL — proximity-based detectors saturate slowly (ATR distance is precise)
BOUNDARY_STRENGTH_SCALE = 0.25
LIQUIDATION_STRENGTH_SCALE = 0.15

# POSITIONING
FUNDING_SATURATION = 4.0
OI_DIVERGENCE_SATURATION = 0.03
OI_SURGE_MIN_DELTA = 0.02
OI_SURGE_SATURATION = 0.04

# General
MIN_STACK_STRENGTH = 0.15
ABSORPTION_PRICE_STALL_ATR = 0.3
LEADER_SYNC_CAP = 0.10
LEADER_SYNC_SCALE = 0.25
CVD_NEUTRAL_EPSILON = 0.01


# ═══════════════════════════════════════════════════════════════════════════
# Confluence Engine
# ═══════════════════════════════════════════════════════════════════════════

class ConfluenceEngine:
    """Evaluates a stack of SignalCards and produces a confluence score + trigger decision."""

    def __init__(self, config: dict):
        # config is the signal_stack sub-dict
        self.base_threshold = config.get('trigger_threshold', 0.35)
        self.effective_threshold = self.base_threshold  # updated per-evaluate with regime modifier
        self.emergency_threshold = config.get('emergency_threshold', 0.85)
        self.regime_modifiers = config.get('regime_modifiers', {
            'trending': 0.85, 'ranging': 1.0, 'squeeze': 0.75, 'chaos': 1.50,
        })
        self.signal_weights = config.get('weights', {})
        self.min_strength_for_stack = MIN_STACK_STRENGTH

    def _directional_score(self, signals: List[SignalCard], direction: Direction) -> float:
        """1 - ∏(1 - s.weighted_score) for all signals matching direction."""
        matching = [s for s in signals
                    if s.direction == direction and s.strength >= self.min_strength_for_stack]
        if not matching:
            return 0.0
        product = 1.0
        for s in matching:
            product *= (1.0 - s.weighted_score)
        return 1.0 - product

    def _compute_confluence(self, signals: List[SignalCard]) -> Tuple[float, Direction]:
        """Returns (confluence_score, dominant_direction)."""
        bullish_score = self._directional_score(signals, Direction.BULLISH)
        bearish_score = self._directional_score(signals, Direction.BEARISH)

        if bullish_score > bearish_score:
            dominant = Direction.BULLISH
            raw_score = bullish_score
        elif bearish_score > bullish_score:
            dominant = Direction.BEARISH
            raw_score = bearish_score
        else:
            return 0.0, Direction.NEUTRAL

        noise_factor = 1.0 - (bullish_score * bearish_score)
        confluence_score = raw_score * noise_factor
        if math.isnan(confluence_score):
            logger.warning("NaN confluence — returning 0.0")
            return 0.0, Direction.NEUTRAL
        return confluence_score, dominant

    def evaluate(self, signals: List[SignalCard], regime: str,
                 is_cooldown_active: bool = False) -> Tuple[float, Direction, bool]:
        """
        Returns (confluence_score, dominant_direction, should_trigger).

        Considers: directional stacking, noise cancellation, regime modifier,
        emergency override, and cooldown.
        """
        confluence_score, dominant_direction = self._compute_confluence(signals)

        modifier = self.regime_modifiers.get(regime, 1.0)
        self.effective_threshold = self.base_threshold * modifier
        effective_threshold = self.effective_threshold

        # Emergency override: fire if any single fresh signal exceeds emergency threshold
        emergency = any(
            s.strength >= self.emergency_threshold and s.direction != Direction.NEUTRAL
            for s in signals
        )

        should_trigger = emergency or (confluence_score >= effective_threshold)

        # During cooldown, only fire on emergency override or stacked break
        # (stacked break handled by caller via cooldown_break logic)
        if is_cooldown_active and not emergency:
            should_trigger = False

        return confluence_score, dominant_direction, should_trigger


# ═══════════════════════════════════════════════════════════════════════════
# SniperTrigger — Main Class
# ═══════════════════════════════════════════════════════════════════════════

class SniperTrigger:
    """
    Signal Stack trigger engine.

    Replaces the old binary-trigger model. Every 2-minute pulse, detects up to
    9 signal types, stacks them directionally via ConfluenceEngine, and fires
    an AI session only when confluence exceeds a regime-adaptive threshold.
    """

    # Cross-symbol correlation matrix: leader → {follower: coefficient}.
    # Only leaders listed as keys can propagate to followers.  Currently empty
    # (all symbols trade independently); add entries when introducing ETH/SOL.
    #
    # Usage — when a leader symbol fires (WAKE), the daemon pushes a leader_sync
    # signal card to each follower listed under that leader.  The boost strength
    # is capped at 0.10 and only nudges a follower that is already close to its
    # own trigger threshold.  A single leader_sync card cannot cause a trigger on
    # its own.
    #
    # Calibration — effective correlation range is 0.50–0.80 (cap flattens
    # everything above 0.80).  For majors that track a common leader
    # (ETH, SOL), 0.70 is a good starting point.  For weaker relationships,
    # 0.50–0.60 gives a barely-felt nudge.  Tune based on backtest results.
    #
    # Example:
    #   CROSS_CORRELATIONS = {
    #       'BTCUSDT': {
    #           'ETHUSDT': 0.70,
    #           'SOLUSDT': 0.70,
    #       },
    #   }
    CROSS_CORRELATIONS: Dict[str, Dict[str, float]] = {}

    def __init__(self, strategy_cfg: Optional[dict] = None, global_cfg: Optional[dict] = None, symbol: Optional[str] = None):
        self.last_trigger_time: Optional[datetime] = None
        self.last_trigger_score: Optional[float] = None
        self._last_trigger_type: Optional[str] = None  # TRADED | NEUTRAL | OBSERVE_ONLY | ACTIVE_POSITION | FAILED
        self.cooldown_active: bool = False
        self.symbol: Optional[str] = symbol

        # Config loading
        from src.utils.pipeline_utils import load_combined_config, load_global_config
        self.strat_cfg = strategy_cfg if strategy_cfg is not None else load_combined_config()
        self.global_cfg = global_cfg if global_cfg is not None else load_global_config()

        # Apply per-symbol overrides if symbol is provided and configs were loaded as base
        if self.symbol:
            from src.config.symbol_resolver import resolve_config, is_symbol_configured
            if is_symbol_configured(self.symbol):
                if strategy_cfg is None:
                    self.strat_cfg = resolve_config(self.strat_cfg, self.symbol)
                if global_cfg is None:
                    self.global_cfg = resolve_config(self.global_cfg, self.symbol)

        self.regime_cfg = self.strat_cfg['regime_parameters']
        self.sniper_cfg = self.global_cfg['sniper']

        # State lock duration: one macro candle
        macro_interval = self.strat_cfg['analysis_window']['macro_context']['time_interval']
        self.macro_hours = self._parse_interval_to_minutes(macro_interval) / 60.0

        # Confluence engine (receives signal_stack sub-config only)
        self.engine = ConfluenceEngine(self.sniper_cfg.get('signal_stack', {}))

        # Signal memory for inter-pulse decay
        self.memory = SignalMemory()

        # State locks for structural/sentiment patterns (ported from old trigger)
        self.state_locks: Dict[str, datetime] = {}

        # Rolling stats for large_trade detector (per-symbol deque of avg_trade_size)
        large_trade_cfg = self.sniper_cfg.get('signal_stack', {}).get('thresholds', {})
        self._trade_size_window = deque(maxlen=large_trade_cfg.get('large_trade_lookback', 30))

        # Structural fingerprint for cooldown break noise gate (see spec 2026-07-23)
        self._fingerprint: Optional[StructuralFingerprint] = None

        # Signal weights (convenience accessor)
        self.signal_weights = self.sniper_cfg.get('signal_stack', {}).get('weights', {})

        # Signal decay half-lives (minutes) — keyed by detector sub_type
        self._decay = self.sniper_cfg['signal_stack']['decay']

        # Ordered signal detection registry
        self._signal_detectors = [
            # FLOW (fastest, most direct)
            self._detect_cvd_momentum,
            self._detect_cvd_divergence,
            self._detect_cvd_absorption,
            # SIZE
            self._detect_large_trade,
            # ENERGY
            self._detect_volatility_surge,
            self._detect_squeeze,
            # STRUCTURAL
            self._detect_boundary_test,
            self._detect_liquidation_hunt,
            # POSITIONING
            self._detect_positioning_extreme,
        ]

        logger.info(
            f"Signal Stack active | base={self.engine.base_threshold} | "
            f"emergency={self.engine.emergency_threshold} | "
            f"detectors={len(self._signal_detectors)}"
        )

    # ── Helpers ──────────────────────────────────────────────────────────

    def _parse_interval_to_minutes(self, interval_str: str) -> float:
        """Parse a Binance interval string ('15m', '1h', '1d') into float minutes."""
        val = int(interval_str[:-1])
        unit = interval_str[-1]
        if unit == 'h':
            return val * 60.0
        if unit == 'd':
            return val * 1440.0
        if unit == 'M':
            return val * 1440.0 * 30.0  # approximate month
        return float(val)

    def _check_state_lock(self, lock_key: str, now: datetime) -> bool:
        """Returns True if permitted to trigger, False if muted by state lock."""
        lock_hours = self.macro_hours
        if lock_key in self.state_locks:
            lock_time, lock_duration = self.state_locks[lock_key]
            elapsed_hours = (now - lock_time).total_seconds() / 3600.0
            if elapsed_hours < lock_duration:
                return False
        self.state_locks[lock_key] = (now, lock_hours)
        return True

    def _signal_cfg(self, *path, default=None):
        """Walk sniper_cfg → signal_stack → ... safely. e.g. _signal_cfg('thresholds', 'cvd_extreme_threshold', default=0.18)."""
        node = self.sniper_cfg.get('signal_stack', {})
        for key in path:
            node = node.get(key, {}) if isinstance(node, dict) else {}
        return node if node else default

    # ── Regime Detection ─────────────────────────────────────────────────

    def _determine_regime(self, curr: Dict[str, Any]) -> str:
        """Classify current market regime for threshold modulation."""
        pd = curr.get('price_dynamics', {})
        mr = curr.get('market_regime', {})
        vii = pd.get('volatility_intensity_index', 0)
        sf = mr.get('squeeze_factor', 1.0)
        trend = abs(mr.get('trend_intensity', 0))
        trend_strong = self.regime_cfg['trend']['trend_intensity_strong']
        squeeze_threshold = self.regime_cfg['volatility']['squeeze_threshold']
        extreme_ratio = self.regime_cfg['volatility']['volatility_extreme_ratio']

        if vii > extreme_ratio:
            return 'chaos'
        if sf < squeeze_threshold:
            return 'squeeze'
        if trend > trend_strong:
            return 'trending'
        return 'ranging'

    # ── Adaptive Cooldown ────────────────────────────────────────────────

    def _check_adaptive_cooldown(self, now: datetime, regime: str) -> Tuple[bool, str]:
        """Returns (is_cooldown_active, reason_string)."""
        if not self.last_trigger_time:
            return False, ""

        cooldown_cfg = self.sniper_cfg.get('signal_stack', {}).get('cooldown', {})
        regime_minutes = cooldown_cfg.get('regime_base_minutes', {})
        cooldown_mins = regime_minutes[regime]

        # Outcome-aware: NEUTRAL/OBSERVE_ONLY get shortened cooldown (no capital deployed)
        if self._last_trigger_type in ("NEUTRAL", "OBSERVE_ONLY"):
            neutral_mult = cooldown_cfg.get('neutral_multiplier', 1.0)
            cooldown_mins = cooldown_mins * neutral_mult

        # FAILED: session errored (e.g. MaxIterationsError) — short fixed backoff,
        # not regime-dependent. The purpose is solely to prevent re-trigger storms.
        if self._last_trigger_type == "FAILED":
            cooldown_mins = cooldown_cfg.get('failure_cooldown_minutes', 3)

        elapsed = (now - self.last_trigger_time).total_seconds() / 60.0

        if elapsed < cooldown_mins:
            return True, f"COOLDOWN_{regime.upper()} ({elapsed:.1f}m/{cooldown_mins}m)"

        return False, ""

    def _get_regime_cooldown(self, regime: str) -> float:
        """Return the cooldown that will be applied after a trigger in this regime."""
        cooldown_cfg = self.sniper_cfg.get('signal_stack', {}).get('cooldown', {})
        regime_minutes = cooldown_cfg.get('regime_base_minutes', {})
        return regime_minutes[regime]

    def _check_cooldown_break(self, fresh_signals: List[SignalCard]) -> bool:
        """Check if cooldown should break due to stacked signals or strength ratio."""
        cooldown_cfg = self.sniper_cfg.get('signal_stack', {}).get('cooldown', {})

        # Break if 3+ fresh signals stack in same direction
        stacked_count = cooldown_cfg.get('stacked_break_count', 3)
        dir_counts: Dict[Direction, int] = {}
        for s in fresh_signals:
            if s.direction != Direction.NEUTRAL and s.strength >= MIN_STACK_STRENGTH:
                dir_counts[s.direction] = dir_counts.get(s.direction, 0) + 1
        for direction, count in dir_counts.items():
            if count >= stacked_count:
                logger.debug(
                    "[%s] cooldown break: stacked | dir=%s count=%d threshold=%d",
                    self.symbol, direction.value, count, stacked_count,
                )
                return True

        # Break if any fresh signal exceeds strength ratio vs last trigger
        if self.last_trigger_score is not None:
            break_ratio = cooldown_cfg.get('break_on_strength_ratio', 1.8)
            ratio_threshold = self.last_trigger_score * break_ratio
            for s in fresh_signals:
                if s.weighted_score >= ratio_threshold:
                    logger.debug(
                        "[%s] cooldown break: strength_ratio | signal=%s "
                        "weighted=%.3f threshold=%.3f (last_score=%.3f × %.1f)",
                        self.symbol, s.sub_type, s.weighted_score,
                        ratio_threshold, self.last_trigger_score, break_ratio,
                    )
                    return True

        return False

    # ── Structural Fingerprint ────────────────────────────────────────────

    def _save_fingerprint(self, metrics: Dict[str, Any]) -> None:
        """Record current market structure snapshot for later stale-check."""
        try:
            topo = metrics.get('volume_profile', {})
            dyn = metrics.get('price_dynamics', {})
            vah = topo.get('vah')
            val = topo.get('val')
            poc = topo.get('poc')
            price = dyn.get('current_price')
            atr = dyn.get('atr_macro', 0)
            regime = self._determine_regime(metrics)

            if None in (vah, val, poc, price) or atr is None or atr <= 0:
                logger.info("[%s] _save_fingerprint: incomplete metrics (vah=%s, val=%s, poc=%s, price=%s, atr=%s)",
                               self.symbol, vah, val, poc, price, atr)
                return

            self._fingerprint = StructuralFingerprint(
                vah=vah, val=val, poc=poc, price=price,
                atr_macro=atr, regime=regime,
                timestamp=datetime.now(timezone.utc),
            )
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("[%s] _save_fingerprint failed | error=%s: %s", self.symbol, type(e).__name__, e)

    def _structure_is_unchanged(self, metrics: Dict[str, Any]) -> Tuple[bool, str]:
        """True if market structure has NOT changed meaningfully since last trigger.

        Return True -> structure unchanged -> cooldown break should be BLOCKED.
        Return False -> structure changed -> allow break through.
        Returns (bool, delta_summary) where delta_summary describes per-field ratios.
        """
        fp = self._fingerprint
        if fp is None:
            return False, ""

        try:
            # Read thresholds from config
            fp_cfg = self.sniper_cfg.get('signal_stack', {}).get('cooldown', {}).get('structural_fingerprint', {})
            structure_shift_atr = fp_cfg.get('structure_shift_atr', 0.5)
            price_drift_atr = fp_cfg.get('price_drift_atr', 0.7)
            atr_change_ratio = fp_cfg.get('atr_change_ratio', 0.30)

            topo = metrics.get('volume_profile', {})
            dyn = metrics.get('price_dynamics', {})
            atr = dyn.get('atr_macro', fp.atr_macro or 1)
            if atr is None or atr <= 0:
                return False, ""

            # None guard — incomplete metrics mean structure is unmeasurable
            curr_vah = topo.get('vah')
            curr_val = topo.get('val')
            curr_poc = topo.get('poc')
            curr_price = dyn.get('current_price')
            if None in (curr_vah, curr_val, curr_poc, curr_price):
                logger.info("[%s] _fingerprint_is_stale: incomplete metrics, allowing break", self.symbol)
                return False, ""

            # ATR shift > ratio -> volatility regime changed
            atr_shift = abs(atr - fp.atr_macro) / max(fp.atr_macro, 1e-9)
            if atr_shift > atr_change_ratio:
                return False, ""

            # Structural boundary shift > N ATR — single loop over fields
            struct_deltas = {}
            for field, val in [('vah', curr_vah), ('val', curr_val), ('poc', curr_poc)]:
                d = abs(val - getattr(fp, field)) / atr
                struct_deltas[field] = d
                if d > structure_shift_atr:
                    return False, ""

            # Price drift > N ATR
            price_delta = abs(curr_price - fp.price) / atr
            if price_delta > price_drift_atr:
                return False, ""

            # Regime change -> unlock different execution rules
            current_regime = self._determine_regime(metrics)
            if current_regime != fp.regime:
                return False, f"regime_changed:{fp.regime}->{current_regime}"

            # Build delta summary for logging
            deltas = [f"{f}={struct_deltas[f]:.2f}atr" for f in ('vah', 'val', 'poc')]
            deltas.append(f"price={price_delta:.2f}atr")
            return True, ", ".join(deltas)

        except (KeyError, TypeError, ValueError) as e:
            logger.warning("[%s] _fingerprint_is_stale failed | error=%s: %s", self.symbol, type(e).__name__, e)
            return False, ""

    # ── Pre-AI Gate ──────────────────────────────────────────────────────

    def _run_pre_ai_gate(self, curr: Dict[str, Any],
                         signals: List[SignalCard],
                         direction: Direction,
                         regime: str) -> Tuple[str, str]:
        """Deterministic pre-check before spending AI tokens. Returns (result, reason)."""
        gate_cfg = self.sniper_cfg.get('signal_stack', {}).get('gate', {})

        # 1. Directional sanity
        if gate_cfg.get('directional_sanity', True):
            trend = curr['market_regime'].get('trend_intensity', 0)
            trend_strong = self.regime_cfg['trend']['trend_intensity_strong']
            cvd = curr['sentiment_signals'].get('cvd_intensity_ratio', 0)
            cvd_threshold = self.regime_cfg['micro_sentiment'].get('cvd_intensity_threshold', 0.1)
            # Counter-trend without structural anchor → suspect
            if direction == Direction.BULLISH and trend < -trend_strong:
                if abs(cvd) < cvd_threshold:
                    return "FAIL", "DIRECTIONAL_SANITY: BULLISH against strong bearish trend without CVD confirmation"
            if direction == Direction.BEARISH and trend > trend_strong:
                if abs(cvd) < cvd_threshold:
                    return "FAIL", "DIRECTIONAL_SANITY: BEARISH against strong bullish trend without CVD confirmation"

        # 2. Chaos survival
        if gate_cfg.get('chaos_survival', True) and regime == 'chaos':
            # Directional momentum signals in chaos are prohibited
            momentum_signals = [s for s in signals
                              if s.sub_type in ('cvd_momentum', 'volatility_surge')
                              and s.direction == direction]
            if momentum_signals and not any(
                s.sub_type in ('squeeze', 'cvd_absorption', 'large_trade') for s in signals
            ):
                return "FAIL", "CHAOS_SURVIVAL: directional momentum prohibited in chaos regime"

        # 3. Minimal active signal convergence — single-signal noise filter.
        # Non-trend regimes (ranging, squeeze, chaos) require at least
        # min_active_non_trend signals in the trigger direction.
        # Trending is exempt — trend inertia makes single signals more reliable.
        # Squeeze signals (direction=NEUTRAL) are naturally excluded by the
        # direction filter — no explicit sub_type exclusion needed.
        min_active = gate_cfg.get('min_active_non_trend', 0)
        if min_active > 0 and regime != 'trending':
            same_dir_active = [
                s for s in signals
                if s.direction == direction
                and s.strength >= MIN_STACK_STRENGTH
            ]
            if len(same_dir_active) < min_active:
                return "FAIL", (
                    f"MIN_ACTIVE_SIGNALS: regime={regime} requires ≥{min_active} "
                    f"signals in {direction.value} direction, "
                    f"got {len(same_dir_active)}"
                )

        return "PASS", ""

    # ── Pre-Brief Builder ────────────────────────────────────────────────

    def _build_situation_brief(self, all_signals: List[SignalCard],
                         confluence_score: float,
                         direction: Direction) -> Dict[str, Any]:
        """Build the pre-brief JSON injected into the SessionAgent's observation."""
        activated_by = []
        for s in sorted(all_signals, key=lambda x: x.weighted_score, reverse=True)[:5]:
            if s.direction != Direction.NEUTRAL or s.sub_type == 'squeeze':
                activated_by.append({
                    "signal": s.sub_type,
                    "direction": s.direction.value,
                    "strength": round(s.strength, 2),
                })

        return {
            "confluence_score": round(confluence_score, 2),
            "confluence_direction": direction.value,
            "activated_by": activated_by,
        }


    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL DETECTORS (9 direct + 1 cross-symbol = 10 total)
    # ═══════════════════════════════════════════════════════════════════════

    def _make_id(self, sub_type: str, now: datetime) -> str:
        return f"{sub_type}_{now.strftime('%Y%m%d_%H%M%S')}"

    # ── FLOW: CVD Momentum (#1) ────────────────────────────────────────

    def _detect_cvd_momentum(self, curr: Dict[str, Any],
                              prev: Optional[Dict[str, Any]],
                              now: datetime) -> Optional[SignalCard]:
        cvd = curr['sentiment_signals']['cvd_intensity_ratio']
        base_threshold = self.regime_cfg['micro_sentiment']['cvd_intensity_threshold']
        extreme_threshold = self._signal_cfg('thresholds', 'cvd_extreme_threshold', default=0.18)

        # Path A (growth): CVD above base threshold AND still growing
        path_a = False
        if abs(cvd) > base_threshold:
            if prev:
                prev_cvd = prev['sentiment_signals']['cvd_intensity_ratio']
                growth_ratio = self.sniper_cfg['probes']['cvd_growth_significance_ratio']
                if abs(cvd) >= abs(prev_cvd) * growth_ratio:
                    path_a = True
            # else: first pulse — no growth check possible; require extreme path (path_b)

        # Path B (extreme static): CVD above the higher extreme threshold
        path_b = abs(cvd) > extreme_threshold

        if not (path_a or path_b):
            return None

        direction = Direction.BULLISH if cvd > 0 else Direction.BEARISH
        strength_a = min(abs(cvd) / (base_threshold * CVD_SATURATION_FACTOR), 1.0) if path_a else 0.0
        strength_b = min((abs(cvd) - extreme_threshold) / extreme_threshold, 1.0) if path_b else 0.0
        strength = max(strength_a, strength_b)
        weight = self.signal_weights.get('cvd_momentum')

        trigger_path = 'extreme' if path_b and strength_b >= strength_a else 'growth'
        return SignalCard(
            signal_id=self._make_id('cvd_momentum', now),
            sub_type='cvd_momentum',
            direction=direction,
            strength=strength,
            weight=weight,
            timestamp=now,
            decay_half_life_minutes=self._decay['cvd_momentum'],
            evidence={'cvd_intensity': cvd, 'threshold': base_threshold,
                      'trigger_path': trigger_path},
        )

    # ── FLOW: CVD Divergence (#2) ──────────────────────────────────────

    def _detect_cvd_divergence(self, curr: Dict[str, Any],
                                prev: Optional[Dict[str, Any]],
                                now: datetime) -> Optional[SignalCard]:
        if not prev:
            return None
        cvd = curr['sentiment_signals']['cvd_intensity_ratio']
        prev_cvd = prev['sentiment_signals']['cvd_intensity_ratio']
        cvd_delta = cvd - prev_cvd
        threshold = self.sniper_cfg['signal_stack']['thresholds']['cvd_divergence_tick_delta']
        if abs(cvd_delta) <= threshold:
            return None

        price_delta = (curr['price_dynamics']['current_price'] -
                       prev['price_dynamics']['current_price'])
        if not ((price_delta > 0 and cvd_delta < 0) or (price_delta < 0 and cvd_delta > 0)):
            return None

        direction = Direction.BEARISH if price_delta > 0 else Direction.BULLISH
        strength = min(abs(cvd_delta) / (threshold * CVD_SATURATION_FACTOR), 1.0)
        weight = self.signal_weights.get('cvd_divergence')

        return SignalCard(
            signal_id=self._make_id('cvd_divergence', now),
            sub_type='cvd_divergence',
            direction=direction,
            strength=strength,
            weight=weight,
            timestamp=now,
            decay_half_life_minutes=self._decay['cvd_divergence'],
            evidence={'cvd_delta': cvd_delta, 'price_delta': price_delta, 'threshold': threshold},
        )

    # ── FLOW: CVD Absorption (#3) ──────────────────────────────────────

    def _detect_cvd_absorption(self, curr: Dict[str, Any],
                                prev: Optional[Dict[str, Any]],
                                now: datetime) -> Optional[SignalCard]:
        cvd = curr['sentiment_signals']['cvd_intensity_ratio']
        extreme_threshold = self.regime_cfg['micro_sentiment']['cvd_intensity_extreme']
        if abs(cvd) <= extreme_threshold:
            return None
        if not prev:
            return None
        price_delta = abs(curr['price_dynamics']['current_price'] -
                          prev['price_dynamics']['current_price'])
        atr_micro = curr['price_dynamics'].get('atr_micro', 0)
        if atr_micro <= 0:
            return None
        if price_delta >= ABSORPTION_PRICE_STALL_ATR * atr_micro:
            return None

        direction = Direction.BEARISH if cvd > 0 else Direction.BULLISH
        saturation_denom = max(extreme_threshold * CVD_ABSORPTION_SATURATION, CVD_ABSORPTION_MIN_DENOM)
        strength = min((abs(cvd) - extreme_threshold) / saturation_denom, 1.0)
        weight = self.signal_weights.get('cvd_absorption')

        return SignalCard(
            signal_id=self._make_id('cvd_absorption', now),
            sub_type='cvd_absorption',
            direction=direction,
            strength=strength,
            weight=weight,
            timestamp=now,
            decay_half_life_minutes=self._decay['cvd_absorption'],
            evidence={'cvd_intensity': cvd, 'price_delta': price_delta},
        )

    # ── SIZE: Large Trade / Institutional Activity (#4) ─────────────────

    def _detect_large_trade(self, curr: Dict[str, Any],
                             prev: Optional[Dict[str, Any]],
                             now: datetime) -> Optional[SignalCard]:
        avg_size = curr['sentiment_signals'].get('avg_trade_size', 0.0)
        if avg_size <= 0:
            return None

        thresholds = self.sniper_cfg.get('signal_stack', {}).get('thresholds', {})
        zscore_threshold = thresholds.get('large_trade_zscore', LARGE_TRADE_SATURATION)

        # Record current value in rolling window
        self._trade_size_window.append(avg_size)
        if len(self._trade_size_window) < MIN_LARGE_TRADE_SAMPLES:
            return None  # need minimum history for meaningful Z-score

        mean = sum(self._trade_size_window) / len(self._trade_size_window)
        variance = sum((x - mean) ** 2 for x in self._trade_size_window) / len(self._trade_size_window)
        std = variance ** 0.5
        if std <= 1e-9:
            return None

        z_score = (avg_size - mean) / std
        if z_score <= zscore_threshold:
            return None

        cvd = curr['sentiment_signals']['cvd_intensity_ratio']
        if abs(cvd) <= CVD_NEUTRAL_EPSILON:
            direction = Direction.NEUTRAL
        else:
            direction = Direction.BULLISH if cvd > 0 else Direction.BEARISH

        strength = min(z_score / (zscore_threshold * LARGE_TRADE_SATURATION), 1.0)
        weight = self.signal_weights.get('large_trade', 0.55)

        return SignalCard(
            signal_id=self._make_id('large_trade', now),
            sub_type='large_trade',
            direction=direction,
            strength=strength,
            weight=weight,
            timestamp=now,
            decay_half_life_minutes=self._decay['large_trade'],
            evidence={'avg_trade_size': avg_size, 'z_score': z_score,
                      'trade_count': curr['sentiment_signals'].get('trade_count', 0)},
        )

    # ── ENERGY: Volatility Surge (#5) ───────────────────────────────────

    def _detect_volatility_surge(self, curr: Dict[str, Any],
                                  prev: Optional[Dict[str, Any]],
                                  now: datetime) -> Optional[SignalCard]:
        vii = curr['price_dynamics']['volatility_intensity_index']
        vpr = curr['market_regime']['volume_participation_ratio']
        baseline = self.regime_cfg['volatility']['volatility_baseline_ratio']
        volume_threshold = self.sniper_cfg['signal_stack']['thresholds']['volume_participation_threshold']

        if not (vii > baseline and vpr > volume_threshold):
            return None

        if prev:
            prev_vii = prev['price_dynamics']['volatility_intensity_index']
            growth_ratio = self.sniper_cfg['probes'].get('volatility_growth_significance_ratio', 1.03)
            if vii <= prev_vii * growth_ratio:
                return None

        cvd = curr['sentiment_signals']['cvd_intensity_ratio']
        if abs(cvd) > 0.05:
            direction = Direction.BULLISH if cvd > 0 else Direction.BEARISH
        elif abs(trend := curr['market_regime'].get('trend_intensity', 0)) > 0.05:
            direction = Direction.BULLISH if trend > 0 else Direction.BEARISH
        else:
            direction = Direction.NEUTRAL  # flat — no directional bias

        strength = min((vii - baseline) / (baseline * VOLATILITY_SATURATION), 1.0)
        weight = self.signal_weights.get('volatility_surge')

        return SignalCard(
            signal_id=self._make_id('volatility_surge', now),
            sub_type='volatility_surge',
            direction=direction,
            strength=strength,
            weight=weight,
            timestamp=now,
            decay_half_life_minutes=self._decay['volatility_surge'],
            evidence={'vii': vii, 'vpr': vpr},
        )

    # ── ENERGY: Squeeze (#6) ─────────────────────────────────────────────

    def _detect_squeeze(self, curr: Dict[str, Any],
                         prev: Optional[Dict[str, Any]],
                         now: datetime) -> Optional[SignalCard]:
        sf = curr['market_regime']['squeeze_factor']
        threshold = (self.regime_cfg['volatility']['squeeze_threshold'] *
                     self.sniper_cfg['probes']['squeeze_trigger_multiplier'])
        if sf >= threshold:
            return None

        if prev:
            prev_sf = prev['market_regime']['squeeze_factor']
            if sf >= prev_sf * SQUEEZE_DECLINE_THRESHOLD:
                return None

        strength = min((threshold - sf) / threshold, 1.0)
        weight = self.signal_weights.get('squeeze')

        return SignalCard(
            signal_id=self._make_id('squeeze', now),
            sub_type='squeeze',
            direction=Direction.NEUTRAL,
            strength=strength,
            weight=weight,
            timestamp=now,
            decay_half_life_minutes=self._decay['squeeze'],
            evidence={'squeeze_factor': sf, 'threshold': threshold},
        )

    # ── STRUCTURAL: Boundary Test (#7) ──────────────────────────────────

    def _detect_boundary_test(self, curr: Dict[str, Any],
                               prev: Optional[Dict[str, Any]],
                               now: datetime) -> Optional[SignalCard]:
        topo = curr['volume_profile']
        atr = curr['price_dynamics'].get('atr_macro', 0)
        if atr <= 0:
            return None
        price = curr['price_dynamics']['current_price']
        part = curr['market_regime']['volume_participation_ratio']

        dist_vh = abs(price - topo['vah']) / atr
        dist_val = abs(price - topo['val']) / atr
        threshold = self.sniper_cfg['proximity']['vah_val_atr']

        nearest_dist = min(dist_vh, dist_val)
        nearest = 'VAH' if dist_vh < dist_val else 'VAL'

        if nearest_dist >= threshold or part <= self.regime_cfg['volume']['min_volume_participation_ratio']:
            return None

        if prev:
            prev_price = prev['price_dynamics']['current_price']
            approaching_up = nearest == 'VAH' and price > prev_price
            approaching_down = nearest == 'VAL' and price < prev_price
            if not (approaching_up or approaching_down):
                return None

        if not self._check_state_lock(f"BOUNDARY_{nearest}", now):
            return None

        direction = Direction.BULLISH if nearest == 'VAH' else Direction.BEARISH
        strength = min(threshold / max(nearest_dist, 0.01) * BOUNDARY_STRENGTH_SCALE, 1.0)
        weight = self.signal_weights.get('boundary_test')

        return SignalCard(
            signal_id=self._make_id('boundary_test', now),
            sub_type='boundary_test',
            direction=direction,
            strength=strength,
            weight=weight,
            timestamp=now,
            decay_half_life_minutes=self._decay['boundary_test'],
            evidence={'boundary': nearest, 'dist_atr': nearest_dist, 'threshold': threshold},
        )

    # ── STRUCTURAL: Liquidation Hunt (#8) ────────────────────────────────

    def _detect_liquidation_hunt(self, curr: Dict[str, Any],
                                  prev: Optional[Dict[str, Any]],
                                  now: datetime) -> Optional[SignalCard]:
        liq_clusters = curr['sentiment_signals'].get('liquidation_clusters')
        if not liq_clusters or not isinstance(liq_clusters, dict):
            return None

        atr = curr['price_dynamics'].get('atr_macro', 0)
        if atr <= 0:
            return None
        price = curr['price_dynamics']['current_price']
        threshold = self.sniper_cfg['proximity']['liq_atr']

        # Check long liquidation clusters (price moving DOWN to sweep)
        for cluster in liq_clusters.get('long_liquidation', []):
            p = float(cluster['price'])
            dist_atr = abs(price - p) / atr
            if dist_atr < threshold:
                if prev:
                    prev_price = prev['price_dynamics']['current_price']
                    if price >= prev_price:
                        continue
                if self._check_state_lock(f"LONG_LIQ_{int(p/100)*100}", now):
                    strength = min(threshold / max(dist_atr, 0.01) * LIQUIDATION_STRENGTH_SCALE, 1.0)
                    return SignalCard(
                        signal_id=self._make_id('liquidation_hunt', now),
                                sub_type='liquidation_hunt',
                        direction=Direction.BEARISH,
                        strength=strength,
                        weight=self.signal_weights.get('liquidation_hunt'),
                                timestamp=now,
                        decay_half_life_minutes=self._decay['liquidation_hunt'],
                        evidence={'cluster_price': p, 'dist_atr': dist_atr, 'type': 'long'},
                    )

        # Check short liquidation clusters (price moving UP to squeeze)
        for cluster in liq_clusters.get('short_liquidation', []):
            p = float(cluster['price'])
            dist_atr = abs(price - p) / atr
            if dist_atr < threshold:
                if prev:
                    prev_price = prev['price_dynamics']['current_price']
                    if price <= prev_price:
                        continue
                if self._check_state_lock(f"SHORT_LIQ_{int(p/100)*100}", now):
                    strength = min(threshold / max(dist_atr, 0.01) * LIQUIDATION_STRENGTH_SCALE, 1.0)
                    return SignalCard(
                        signal_id=self._make_id('liquidation_hunt', now),
                                sub_type='liquidation_hunt',
                        direction=Direction.BULLISH,
                        strength=strength,
                        weight=self.signal_weights.get('liquidation_hunt'),
                                timestamp=now,
                        decay_half_life_minutes=self._decay['liquidation_hunt'],
                        evidence={'cluster_price': p, 'dist_atr': dist_atr, 'type': 'short'},
                    )

        return None

    # ── POSITIONING: Unified Positioning Extreme (#9) ───────────────────

    def _detect_positioning_extreme(self, curr: Dict[str, Any],
                                     prev: Optional[Dict[str, Any]],
                                     now: datetime) -> Optional[SignalCard]:
        """Unifies retail_extreme, oi_divergence, and oi_surge into one detector.
        Three independent trigger paths — strongest one wins if multiple fire."""

        best_direction = None
        best_strength = 0.0
        best_evidence: Dict[str, Any] = {}

        # Path 1: LS extreme (retail positioning)
        ls = curr['sentiment_signals'].get('ls_ratio_micro', 1.0)
        cfg = self.regime_cfg['imbalance']
        if ls > cfg['long_short_imbalance_ratio']:
            strength = min((ls - 1.0) / (cfg['long_short_imbalance_ratio'] * 2), 1.0)
            if strength > best_strength:
                best_direction = Direction.BEARISH
                best_strength = strength
                best_evidence = {'trigger': 'ls_long', 'ls_ratio': ls}
        elif ls < cfg['short_heavy_imbalance_ratio']:
            strength = min((1.0 - ls) / ((1.0 - cfg['short_heavy_imbalance_ratio']) * 2), 1.0)
            if strength > best_strength:
                best_direction = Direction.BULLISH
                best_strength = strength
                best_evidence = {'trigger': 'ls_short', 'ls_ratio': ls}

        # Path 2: Funding extreme
        funding = curr['sentiment_signals'].get('funding_rate', 0.0)
        funding_threshold = self.regime_cfg['micro_sentiment']['funding_extreme_threshold']
        if abs(funding) > funding_threshold:
            f_direction = Direction.BEARISH if funding > 0 else Direction.BULLISH
            f_strength = min(abs(funding) / (funding_threshold * FUNDING_SATURATION), 1.0)
            if f_strength > best_strength:
                best_direction = f_direction
                best_strength = f_strength
                best_evidence = {'trigger': 'funding', 'funding_rate': funding}

        # Path 3: OI divergence (OI and price move opposite)
        if prev:
            oi_delta = curr['sentiment_signals'].get('oi_delta_micro', 0.0)
            price_delta = (curr['price_dynamics']['current_price'] -
                           prev['price_dynamics']['current_price'])
            if abs(oi_delta) > 1e-10 and abs(price_delta) > 1e-10 and abs(oi_delta) > 0.01:
                if (oi_delta > 0 and price_delta < 0) or (oi_delta < 0 and price_delta > 0):
                    oi_dir = Direction.BEARISH if price_delta > 0 else Direction.BULLISH
                    oi_strength = min(abs(oi_delta) / OI_DIVERGENCE_SATURATION, 1.0)
                    if oi_strength > best_strength:
                        best_direction = oi_dir
                        best_strength = oi_strength
                        best_evidence = {'trigger': 'oi_divergence', 'oi_delta': oi_delta,
                                         'price_delta': price_delta}

        # Path 4: OI surge (OI and price move same direction)
        if prev:
            oi_delta = curr['sentiment_signals'].get('oi_delta_micro', 0.0)
            price_delta = (curr['price_dynamics']['current_price'] -
                           prev['price_dynamics']['current_price'])
            if abs(oi_delta) > OI_SURGE_MIN_DELTA:
                if (oi_delta > 0 and price_delta > 0) or (oi_delta < 0 and price_delta < 0):
                    oi_dir = Direction.BULLISH if price_delta > 0 else Direction.BEARISH
                    oi_strength = min((abs(oi_delta) - OI_SURGE_MIN_DELTA) / OI_SURGE_SATURATION, 1.0)
                    if oi_strength > best_strength:
                        best_direction = oi_dir
                        best_strength = oi_strength
                        best_evidence = {'trigger': 'oi_surge', 'oi_delta': oi_delta,
                                         'price_delta': price_delta}

        if best_direction is None:
            return None

        lock_key = f"POSITIONING_EXTREME_{best_direction.value}"
        if not self._check_state_lock(lock_key, now):
            return None

        weight = self.signal_weights.get('positioning_extreme')

        return SignalCard(
            signal_id=self._make_id('positioning_extreme', now),
            sub_type='positioning_extreme',
            direction=best_direction,
            strength=best_strength,
            weight=weight,
            timestamp=now,
            decay_half_life_minutes=self._decay['positioning_extreme'],
            evidence=best_evidence,
        )

    # ── CROSS-SYMBOL / Re-evaluation ───────────────────────────────────

    def reevaluate_with_boost(self, boosted_signals: List[SignalCard],
                              metrics: Dict[str, Any]) -> Optional['TriggerResult']:
        """Public entry point for cross-symbol Leader Sync re-evaluation.

        Called by SniperDaemon when a correlated leader symbol triggers.
        Recomputes confluence with the boost signal included, re-runs the
        pre-AI gate, and returns a new TriggerResult if the boost tips the
        follower over threshold. Returns None if still below threshold or
        gate fails.
        """
        regime = self._determine_regime(metrics)
        confluence_score, dominant_direction, should_trigger = self.engine.evaluate(
            boosted_signals, regime, is_cooldown_active=False
        )
        if not should_trigger:
            return None

        try:
            gate_result, _ = self._run_pre_ai_gate(
                metrics, boosted_signals, dominant_direction, regime
            )
        except Exception as e:
            logger.warning(f"pre-AI gate crashed in reevaluate_with_boost | error={e}")
            return None
        if gate_result == 'FAIL':
            return None

        situation_brief = self._build_situation_brief(
            boosted_signals, confluence_score, dominant_direction
        )
        cooldown_mins = self._get_regime_cooldown(regime)

        # ★ Save fingerprint for cooldown break noise gate
        self._save_fingerprint(metrics)

        return TriggerResult(
            triggered=True,
            confluence_score=confluence_score,
            confluence_direction=dominant_direction,
            signals=boosted_signals,
            active_signals=[s for s in boosted_signals
                           if s.strength >= MIN_STACK_STRENGTH and s.sub_type != 'leader_sync'],
            gate_result=gate_result,
            situation_brief=situation_brief,
            cooldown_minutes=cooldown_mins,
        )

    def apply_leader_sync(self, own_signals: List[SignalCard],
                          leader_confluence_score: float,
                          leader_direction: Direction,
                          correlation: float,
                          now: datetime) -> Optional[SignalCard]:
        """Amplify existing weak directional alignment when leader fires."""
        if leader_confluence_score <= 0:
            return None

        aligned = [s for s in own_signals if s.direction == leader_direction]
        if not aligned:
            return None

        boost = min(leader_confluence_score * correlation * LEADER_SYNC_SCALE, LEADER_SYNC_CAP)
        weight = self.signal_weights.get('leader_sync', 0.40)

        return SignalCard(
            signal_id=self._make_id('leader_sync', now),
            sub_type='leader_sync',
            direction=leader_direction,
            strength=boost,
            weight=weight,
            timestamp=now,
            decay_half_life_minutes=self._decay['leader_sync'],
            evidence={'leader_score': leader_confluence_score, 'correlation': correlation},
        )

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL DIAGNOSTICS — per-pulse compact log of all detector key metrics
    # ═══════════════════════════════════════════════════════════════════════

    def _log_signal_diagnostics(self, metrics: Dict[str, Any],
                                 fresh_signals: List[SignalCard]) -> None:
        """Log a compact per-pulse summary of key decision metrics for all 9
        signal detectors."""
        fired = {s.sub_type: s for s in fresh_signals}
        parts: List[str] = []

        # ── FLOW category ──
        cvd = metrics['sentiment_signals']['cvd_intensity_ratio']
        cvd_thresh = self.regime_cfg['micro_sentiment']['cvd_intensity_threshold']
        parts.append(f"cvd={cvd:+.3f}")

        s = fired.get('cvd_momentum')
        if s:
            path = s.evidence.get('trigger_path', '?')
            parts.append(f"cvd_momentum=F:{s.strength:.2f}({path})")
        else:
            if abs(cvd) <= cvd_thresh:
                parts.append(f"cvd_momentum=R:|cvd|={abs(cvd):.3f}<={cvd_thresh}")
            else:
                parts.append(f"cvd_momentum=R:no_growth(|cvd|={abs(cvd):.3f}>{cvd_thresh})")

        s = fired.get('cvd_divergence')
        parts.append(f"cvd_divergence={'F:'+str(round(s.strength,2)) if s else 'R:no-prev/div-low'}")
        s = fired.get('cvd_absorption')
        parts.append(f"cvd_absorption={'F:'+str(round(s.strength,2)) if s else f'R:|cvd|<=extreme'}")

        # ── SIZE category ──
        avg_size = metrics['sentiment_signals'].get('avg_trade_size', 0.0)
        trade_n = metrics['sentiment_signals'].get('trade_count', 0)
        z_thresh = self._signal_cfg('thresholds', 'large_trade_zscore', default=2.0)
        s = fired.get('large_trade')
        if s:
            z = s.evidence.get('z_score', 0.0)
            parts.append(f"trade_sz={avg_size:.4f}/n={trade_n} | large_trade=F:{s.strength:.2f}(z={z:.1f})")
        elif len(self._trade_size_window) < MIN_LARGE_TRADE_SAMPLES:
            parts.append(f"trade_sz={avg_size:.4f}/n={trade_n} | large_trade=R:warmup({len(self._trade_size_window)}/{MIN_LARGE_TRADE_SAMPLES})")
        else:
            mean = sum(self._trade_size_window) / len(self._trade_size_window)
            std = (sum((x - mean) ** 2 for x in self._trade_size_window) / len(self._trade_size_window)) ** 0.5
            z = (avg_size - mean) / std if std > 1e-9 else 0.0
            parts.append(f"trade_sz={avg_size:.4f}/n={trade_n} | large_trade=R:z={z:.1f}<={z_thresh}")

        # ── ENERGY category ──
        vii = metrics['price_dynamics']['volatility_intensity_index']
        vpr = metrics['market_regime']['volume_participation_ratio']
        parts.append(f"vii={vii:.2f},vpr={vpr:.2f}")

        s = fired.get('volatility_surge')
        vol_base = self.regime_cfg['volatility']['volatility_baseline_ratio']
        volume_threshold = self.sniper_cfg['signal_stack']['thresholds']['volume_participation_threshold']
        parts.append(f"vol_surge={'F:'+str(round(s.strength,2)) if s else f'R:vii<={vol_base}|vpr<={volume_threshold}'}")

        sf = metrics['market_regime']['squeeze_factor']
        sq_thresh = (self.regime_cfg['volatility']['squeeze_threshold'] *
                     self.sniper_cfg['probes']['squeeze_trigger_multiplier'])
        s = fired.get('squeeze')
        parts.append(f"squeeze={'F:'+str(round(s.strength,2)) if s else f'R:sf={sf:.3f}>={sq_thresh:.3f}'}")

        # ── STRUCTURAL category ──
        price = metrics['price_dynamics']['current_price']
        atr = metrics['price_dynamics'].get('atr_macro', 0)
        topo = metrics['volume_profile']
        parts.append(f"price={price:.1f},atr={atr:.2f}")

        s = fired.get('boundary_test')
        if atr > 0:
            dist_vh = abs(price - topo['vah']) / atr
            dist_val = abs(price - topo['val']) / atr
            prox_thresh = self.sniper_cfg['proximity']['vah_val_atr']
            parts.append(f"boundary_test={'F:'+str(round(s.strength,2)) if s else f'R:dist_vh={dist_vh:.1f},dist_val={dist_val:.1f}>={prox_thresh}'}")
        else:
            parts.append("boundary_test=R:atr=0")

        s = fired.get('liquidation_hunt')
        parts.append(f"liq_hunt={'F:'+str(round(s.strength,2)) if s else 'R:no-cluster-in-range'}")

        # ── POSITIONING category ──
        ls = metrics['sentiment_signals'].get('ls_ratio_micro', 1.0)
        funding = metrics['sentiment_signals'].get('funding_rate', 0.0)
        oi_delta = metrics['sentiment_signals'].get('oi_delta_micro', 0.0)
        parts.append(f"ls={ls:.2f},fund={funding:.4f},oi_d={oi_delta:.4f}")

        s = fired.get('positioning_extreme')
        ls_imb = self.regime_cfg['imbalance']['long_short_imbalance_ratio']
        ls_short = self.regime_cfg['imbalance']['short_heavy_imbalance_ratio']
        fund_ext = self.regime_cfg['micro_sentiment']['funding_extreme_threshold']
        parts.append(f"pos_ext={'F:'+str(round(s.strength,2)) if s else f'R:ls<={ls_imb}&ls>={ls_short}&|fund|<={fund_ext}&oi<0.01'}")

        logger.info("[%s] SIGNAL DIAG | %s", self.symbol, " | ".join(parts))

    # ═══════════════════════════════════════════════════════════════════════
    # MAIN EVALUATE — replaces old (bool, str, str) method
    # ═══════════════════════════════════════════════════════════════════════

    def evaluate(self, current_metrics: Dict[str, Any],
                 prev_metrics: Optional[Dict[str, Any]] = None) -> TriggerResult:
        """
        Evaluate current market state and return a TriggerResult.

        Algorithm:
        1. Check adaptive cooldown
        2. Detect all 9 direct signal types
        3. Merge with decayed signal memory
        4. Compute confluence via ConfluenceEngine
        5. Check emergency override and cooldown break
        6. Run Pre-AI gate if triggering
        7. Build pre-brief if passing
        8. Return TriggerResult
        """
        now = datetime.now(timezone.utc)

        # 0. Determine regime
        regime = self._determine_regime(current_metrics)

        # 1. Cooldown check
        cooldown_active, cooldown_reason = self._check_adaptive_cooldown(now, regime)

        # 2. Detect all signals from current pulse
        fresh_signals: List[SignalCard] = []
        for detector in self._signal_detectors:
            try:
                card = detector(current_metrics, prev_metrics, now)
                if card:
                    fresh_signals.append(card)
            except Exception as e:
                logger.warning(f"detector {detector.__name__} failed | error={e}")

        # 2b. Per-pulse signal diagnostics — compact log of all detector key metrics
        self._log_signal_diagnostics(current_metrics, fresh_signals)

        # 3. Merge with decayed signal memory
        all_signals = self.memory.ingest(fresh_signals, now)

        # 4. Check cooldown break (stacked signals or strength ratio)
        cooldown_break = False
        if cooldown_active:
            cooldown_break = self._check_cooldown_break(fresh_signals)

            # ★ Structural fingerprint gate: if structure hasn't changed,
            #   don't allow cooldown break — prevents re-triggering on the
            #   same market conditions.
            if cooldown_break:
                unchanged, delta_summary = self._structure_is_unchanged(current_metrics)
                if unchanged:
                    logger.info(
                        "[%s] cooldown NOT broken: structure unchanged (%s)",
                        self.symbol, delta_summary,
                    )
                    cooldown_break = False
                else:
                    logger.info("[%s] cooldown break: signal surge", self.symbol)

        # 5. Compute confluence
        effective_cooldown = cooldown_active and not cooldown_break
        self.cooldown_active = effective_cooldown
        confluence_score, dominant_direction, should_trigger = self.engine.evaluate(
            all_signals, regime, is_cooldown_active=effective_cooldown
        )

        # 6. Pre-AI Gate
        gate_result = "PASS"
        if should_trigger:
            try:
                gate_result, _ = self._run_pre_ai_gate(
                    current_metrics, all_signals, dominant_direction, regime
                )
            except Exception as e:
                logger.warning(f"pre-AI gate crashed | error={e}")
                gate_result = "FAIL"
            if gate_result == "FAIL":
                should_trigger = False

        # 7. Build situation brief
        situation_brief = None
        if should_trigger:
            situation_brief = self._build_situation_brief(
                all_signals, confluence_score, dominant_direction
            )
            # ★ Snapshot structure at trigger time for cooldown break noise gate
            self._save_fingerprint(current_metrics)

        # 8. Cooldown for this trigger
        cooldown_mins = self._get_regime_cooldown(regime)

        result = TriggerResult(
            triggered=should_trigger,
            confluence_score=confluence_score,
            confluence_direction=dominant_direction,
            signals=all_signals,
            active_signals=[s for s in fresh_signals if s.strength >= MIN_STACK_STRENGTH],
            gate_result=gate_result,
            situation_brief=situation_brief,
            cooldown_minutes=cooldown_mins,
        )

        if should_trigger:
            memory_signal_count = len(all_signals) - len(fresh_signals)
            logger.info(
                f"[{self.symbol}] WAKE | dir={dominant_direction.value} | "
                f"confluence={confluence_score:.2f} | "
                f"fresh={len(fresh_signals)} | memory={memory_signal_count} | "
                f"active={[s.sub_type for s in result.active_signals]} | "
                f"gate={gate_result} | regime={regime}"
            )

        return result

    def set_triggered(self, result: TriggerResult, trigger_type: str = "ACTIVE"):
        """Record trigger time and score for cooldown tracking.

        trigger_type controls cooldown duration:
        - TRADED / ACTIVE_POSITION → full regime cooldown (capital deployed)
        - NEUTRAL / OBSERVE_ONLY → shortened (× neutral_multiplier)
        """
        self.last_trigger_time = datetime.now(timezone.utc)
        self.last_trigger_score = result.confluence_score
        self._last_trigger_type = trigger_type
        self.cooldown_active = True
