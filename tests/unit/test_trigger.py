"""Tests for Sniper Trigger — SignalCard, SignalMemory, ConfluenceEngine."""
import math
from datetime import datetime, timezone, timedelta
import pytest
from src.sniper.trigger import (
    SignalCard, SignalMemory, ConfluenceEngine, Direction,
)


# ═══════════════════════════════════════════════════════════════════════════
# SignalCard
# ═══════════════════════════════════════════════════════════════════════════

class TestSignalCard:

    def _card(self, strength=0.5, weight=0.8, decay=10.0):
        return SignalCard(
            signal_id="test_1", sub_type="test",
            direction=Direction.BULLISH, strength=strength, weight=weight, timestamp=datetime.now(timezone.utc),
            decay_half_life_minutes=decay,
        )

    def test_weighted_score_normal(self):
        c = self._card(strength=0.5, weight=0.8)
        assert c.weighted_score == pytest.approx(0.4)

    def test_weighted_score_zero_strength(self):
        c = self._card(strength=0.0, weight=0.8)
        assert c.weighted_score == pytest.approx(0.0)

    def test_weighted_score_nan_strength(self):
        c = self._card(strength=math.nan, weight=0.8)
        assert c.weighted_score == pytest.approx(0.0)

    def test_weighted_score_inf_weight(self):
        c = self._card(strength=0.5, weight=math.inf)
        assert c.weighted_score == pytest.approx(0.0)

    def test_decayed_strength_no_elapsed(self):
        c = self._card(strength=0.8, decay=10.0)
        # Same timestamp → no decay
        result = c.decayed_strength(c.timestamp)
        assert result == pytest.approx(0.8)

    def test_decayed_strength_half_life(self):
        c = self._card(strength=1.0, decay=10.0)
        # After 10 minutes → strength halved
        later = c.timestamp + timedelta(minutes=10)
        result = c.decayed_strength(later)
        assert result == pytest.approx(0.5)

    def test_decayed_strength_zero_half_life(self):
        c = self._card(strength=1.0, decay=0.0)
        later = c.timestamp + timedelta(minutes=5)
        # decay_half_life_minutes floors to 0.01
        result = c.decayed_strength(later)
        assert result > 0.0
        assert result < 1.0

# ═══════════════════════════════════════════════════════════════════════════
# SignalMemory
# ═══════════════════════════════════════════════════════════════════════════

class TestSignalMemory:

    def _card(self, sid="flow.cvd_1", sub_type="cvd_divergence", strength=0.5, ts=None):
        return SignalCard(
            signal_id=sid, sub_type=sub_type,
            direction=Direction.BULLISH, strength=strength, weight=0.8, timestamp=ts or datetime.now(timezone.utc),
            decay_half_life_minutes=10.0,
        )

    def test_ingest_empty(self):
        mem = SignalMemory()
        now = datetime.now(timezone.utc)
        result = mem.ingest([], now)
        assert result == []

    def test_ingest_new_signal(self):
        mem = SignalMemory()
        now = datetime.now(timezone.utc)
        cards = [self._card()]
        result = mem.ingest(cards, now)
        assert len(result) == 1
        assert result[0].signal_id == "flow.cvd_1"

    def test_ingest_expired_signal_purged(self):
        mem = SignalMemory()
        old_ts = datetime.now(timezone.utc) - timedelta(hours=24)
        cards = [self._card(strength=0.1, ts=old_ts)]
        mem.ingest(cards, old_ts)  # stored at old_ts

        now = datetime.now(timezone.utc)
        # strength already decayed to < 0.05, should be purged
        result = mem.ingest([], now)
        assert len(result) == 0

    def test_ingest_new_replaces_old_same_subtype(self):
        mem = SignalMemory()
        now = datetime.now(timezone.utc)
        old_card = self._card(sub_type="cvd_divergence", strength=0.3, ts=now)
        mem.ingest([old_card], now)

        new_card = self._card(sub_type="cvd_divergence", strength=0.9, ts=now)
        result = mem.ingest([new_card], now)
        # Only the new one should survive (same sub_type)
        assert len(result) == 1
        assert result[0].strength == pytest.approx(0.9)


# ═══════════════════════════════════════════════════════════════════════════
# ConfluenceEngine
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def engine():
    return ConfluenceEngine({
        'trigger_threshold': 0.35,
        'emergency_threshold': 0.85,
        'regime_modifiers': {'trending': 0.85, 'ranging': 1.15, 'squeeze': 0.70, 'chaos': 1.50},
        'weights': {},
    })


class TestConfluenceEngine:

    def _card(self, direction=Direction.BULLISH, strength=0.5, weight=0.8):
        return SignalCard(
            signal_id="t", sub_type="test",
            direction=direction, strength=strength, weight=weight, timestamp=datetime.now(timezone.utc),
            decay_half_life_minutes=10.0,
        )

    def test_no_signals(self, engine):
        score, direction, trigger = engine.evaluate([], "ranging")
        assert score == pytest.approx(0.0)
        assert trigger is False

    def test_single_bullish_strong(self, engine):
        signals = [self._card(Direction.BULLISH, strength=0.9, weight=1.0)]
        score, direction, trigger = engine.evaluate(signals, "trending")
        assert direction == Direction.BULLISH
        assert trigger is True

    def test_single_weak_below_threshold(self, engine):
        signals = [self._card(Direction.BULLISH, strength=0.1, weight=0.3)]
        score, direction, trigger = engine.evaluate(signals, "ranging")
        # threshold = 0.35 * 1.15 = 0.4025
        assert trigger is False

    def test_regime_modifier_chaos_raises_threshold(self, engine):
        """Chaos modifier (1.50) raises effective threshold to 0.525."""
        signals = [self._card(Direction.BULLISH, strength=0.4, weight=1.0)]
        score, direction, trigger = engine.evaluate(signals, "chaos")
        # effective_threshold = 0.35 * 1.50 = 0.525, score = 0.4 < 0.525
        assert trigger is False

    def test_regime_modifier_squeeze_lowers_threshold(self, engine):
        """Squeeze modifier (0.70) lowers effective threshold to 0.245."""
        signals = [self._card(Direction.BULLISH, strength=0.3, weight=1.0)]
        score, direction, trigger = engine.evaluate(signals, "squeeze")
        assert trigger is True

    def test_emergency_override_triggers_during_cooldown(self, engine):
        signals = [self._card(Direction.BULLISH, strength=0.9, weight=1.0)]
        score, direction, trigger = engine.evaluate(signals, "ranging", is_cooldown_active=True)
        assert trigger is True  # emergency: strength=0.9 >= 0.85

    def test_cooldown_blocks_non_emergency(self, engine):
        signals = [self._card(Direction.BULLISH, strength=0.5, weight=1.0)]
        score, direction, trigger = engine.evaluate(signals, "ranging", is_cooldown_active=True)
        assert trigger is False  # strength=0.5 < 0.85 emergency

    def test_neutral_signals_dont_trigger_emergency(self, engine):
        """NEUTRAL direction signals should not count as emergency override."""
        signals = [self._card(Direction.NEUTRAL, strength=0.9, weight=1.0)]
        score, direction, trigger = engine.evaluate(signals, "ranging")
        assert trigger is False

    def test_bullish_wins_over_bearish(self, engine):
        signals = [
            self._card(Direction.BULLISH, strength=0.6, weight=1.0),
            self._card(Direction.BEARISH, strength=0.3, weight=1.0),
        ]
        score, direction, trigger = engine.evaluate(signals, "trending")
        assert direction == Direction.BULLISH

    def test_directional_score_below_min_strength_ignored(self, engine):
        """strength < 0.15 should be excluded from stacking."""
        signals = [
            self._card(Direction.BULLISH, strength=0.05, weight=1.0),
        ]
        score, direction, trigger = engine.evaluate(signals, "trending")
        # No matching signal (below min_strength_for_stack=0.15)
        assert score == pytest.approx(0.0)
        assert trigger is False

    def test_nan_score_returns_zero_trigger(self, engine):
        """NaN confluence returns 0.0 score and False trigger."""
        signals = [self._card(Direction.BULLISH, strength=math.nan, weight=math.nan)]
        score, direction, trigger = engine.evaluate(signals, "ranging")
        assert score == pytest.approx(0.0)
        assert trigger is False


def test_sniper_trigger_initialization_with_symbol_override():
    """SniperTrigger initialized with symbol alone should resolve per-symbol overrides."""
    from src.sniper.trigger import SniperTrigger
    trigger_btc = SniperTrigger(symbol="BTCUSDT")
    # BTC overrides trigger_threshold to 0.36 and cvd_divergence_tick_delta to 0.08
    thresholds = trigger_btc.sniper_cfg.get("signal_stack", {}).get("thresholds", {})
    assert thresholds.get("cvd_divergence_tick_delta") == 0.08
    assert trigger_btc.sniper_cfg.get("signal_stack", {}).get("trigger_threshold") == 0.36

