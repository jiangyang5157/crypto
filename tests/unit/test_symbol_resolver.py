"""Tests for symbol-aware config resolution and patching."""
import copy
import os
import pytest

from src.config.symbol_resolver import (
    load_symbol_config,
    list_configured_symbols,
    get_symbol_trade_params,
    resolve_config,
    load_and_resolve_for_symbol,
    is_symbol_configured,
)


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_base_config():
    return {
        "regime_parameters": {
            "trend": {
                "trend_intensity_threshold": 0.2,
                "trend_intensity_strong": 0.35,
                "trend_intensity_min_expansion": 0.12,
            },
            "volatility": {
                "volatility_baseline_ratio": 1.25,
                "volatility_extreme_ratio": 2.2,
            },
        },
        "sniper": {
            "signal_stack": {
                "thresholds": {
                    "cvd_divergence_tick_delta": 0.20,
                }
            }
        },
    }


@pytest.fixture
def sample_symbol_config():
    return {
        "XAUTUSDT": {
            "precision_qty": 3,
            "precision_price": 1,
            "min_order_qty": 0.01,
            "sl_slippage_buffer": 0.5,
            "overrides": {},
        },
        "BTCUSDT": {
            "precision_qty": 4,
            "precision_price": 1,
            "min_order_qty": 0.001,
            "sl_slippage_buffer": 10.0,
            "overrides": {
                "regime_parameters": {
                    "trend": {
                        "trend_intensity_min_expansion": 0.12,
                    },
                },
                "sniper": {
                    "signal_stack": {
                        "thresholds": {
                            "cvd_divergence_tick_delta": 0.25,
                        },
                    },
                },
            },
        },
    }


# ── load_symbol_config / list / is_configured ──────────────────────────────


def test_load_symbol_config():
    cfg = load_symbol_config()
    assert isinstance(cfg, dict)
    assert "BTCUSDT" in cfg
    assert "XAUTUSDT" in cfg


def test_list_configured_symbols():
    symbols = list_configured_symbols()
    assert "BTCUSDT" in symbols
    assert "XAUTUSDT" in symbols


def test_is_symbol_configured_positive():
    assert is_symbol_configured("BTCUSDT") is True
    assert is_symbol_configured("XAUTUSDT") is True


def test_is_symbol_configured_negative():
    assert is_symbol_configured("SOLUSDT") is False
    assert is_symbol_configured("") is False


# ── get_symbol_trade_params ─────────────────────────────────────────────────


def test_get_symbol_trade_params_btc():
    params = get_symbol_trade_params("BTCUSDT")
    assert params["precision_qty"] == 4
    assert params["precision_price"] == 1
    assert params["min_order_qty"] == 0.001
    assert params["sl_slippage_buffer"] == 10.0


def test_get_symbol_trade_params_xaut():
    params = get_symbol_trade_params("XAUTUSDT")
    assert params["precision_qty"] == 3
    assert params["sl_slippage_buffer"] == 1


def test_get_symbol_trade_params_unknown_raises():
    """Unknown symbol raises KeyError — no silent fallback."""
    with pytest.raises(KeyError, match="not configured"):
        get_symbol_trade_params("UNKNOWN")


# ── resolve_config ──────────────────────────────────────────────────────────


def test_resolve_config_applies_overrides(sample_base_config, sample_symbol_config):
    result = resolve_config(sample_base_config, "BTCUSDT", sample_symbol_config)
    # Overridden values
    assert result["regime_parameters"]["trend"]["trend_intensity_min_expansion"] == 0.12
    assert result["sniper"]["signal_stack"]["thresholds"]["cvd_divergence_tick_delta"] == 0.25
    # Non-overridden values preserved
    assert result["regime_parameters"]["trend"]["trend_intensity_threshold"] == 0.2
    assert result["regime_parameters"]["volatility"]["volatility_baseline_ratio"] == 1.25


def test_resolve_config_no_overrides(sample_base_config, sample_symbol_config):
    """XAUTUSDT has empty overrides — result equals base."""
    result = resolve_config(sample_base_config, "XAUTUSDT", sample_symbol_config)
    assert result == sample_base_config


def test_resolve_config_unknown_symbol_raises(sample_base_config, sample_symbol_config):
    """Unknown symbol raises KeyError — no silent fallback."""
    with pytest.raises(KeyError, match="not configured"):
        resolve_config(sample_base_config, "SOLUSDT", sample_symbol_config)


def test_resolve_config_does_not_mutate_original(sample_base_config, sample_symbol_config):
    original = copy.deepcopy(sample_base_config)
    resolve_config(sample_base_config, "XAUTUSDT", sample_symbol_config)
    assert sample_base_config == original  # original unchanged


def test_resolve_config_returns_new_dict(sample_base_config, sample_symbol_config):
    result = resolve_config(sample_base_config, "XAUTUSDT", sample_symbol_config)
    assert result is not sample_base_config  # different object


def test_resolve_config_non_dict_overrides():
    """If overrides is not a dict, it's silently skipped."""
    sym_cfg = {"WEIRD": {"overrides": "not_a_dict"}}
    base = {"regime_parameters": {"trend": {"x": 1}}}
    result = resolve_config(base, "WEIRD", sym_cfg)
    assert result == base  # no crash, no change


def test_resolve_config_nonexistent_section_in_overrides():
    """Overrides for a section not in base config are silently skipped."""
    sym_cfg = {"TEST": {"overrides": {"nonexistent_section": {"key": 99}}}}
    base = {"regime_parameters": {"trend": {"x": 1}}}
    result = resolve_config(base, "TEST", sym_cfg)
    assert result == base  # section doesn't exist in base, skipped


def test_resolve_config_deeply_nested_override():
    """Deep nesting in overrides works correctly."""
    sym_cfg = {
        "DEEP": {
            "overrides": {
                "regime_parameters": {
                    "trend": {"trend_intensity_threshold": 0.5},
                }
            }
        }
    }
    base = {
        "regime_parameters": {
            "trend": {
                "trend_intensity_threshold": 0.2,
                "trend_intensity_strong": 0.35,
            }
        }
    }
    result = resolve_config(base, "DEEP", sym_cfg)
    assert result["regime_parameters"]["trend"]["trend_intensity_threshold"] == 0.5
    assert result["regime_parameters"]["trend"]["trend_intensity_strong"] == 0.35


def test_resolve_config_with_real_files():
    """Integration: resolve against actual YAML files."""
    cfg = load_and_resolve_for_symbol("XAUTUSDT")
    # Base values preserved
    assert cfg["regime_parameters"]["trend"]["trend_intensity_min_expansion"] == 0.08
    # XAUTUSDT override lowers cvd_divergence_tick_delta from base 0.18
    assert cfg["sniper"]["signal_stack"]["thresholds"]["cvd_divergence_tick_delta"] == 0.12
    # Other base values preserved
    assert cfg["regime_parameters"]["volatility"]["volatility_extreme_ratio"] == 2.2
    assert cfg["sniper"]["probes"]["cvd_growth_significance_ratio"] == 1.4


def test_load_and_resolve_btc_with_overrides():
    cfg = load_and_resolve_for_symbol("BTCUSDT")
    # BTC overrides push values away from the base
    assert cfg["regime_parameters"]["trend"]["trend_intensity_min_expansion"] == 0.12
    assert cfg["sniper"]["signal_stack"]["thresholds"]["cvd_divergence_tick_delta"] == 0.08


# ── Data flow: full config pipeline ──────────────────────────────────────────


def test_full_config_pipeline_session():
    """Simulate the run_session.py data flow."""
    from src.utils.pipeline_utils import load_config, load_global_config
    from src.config.symbol_resolver import resolve_config

    strategy = load_config()
    global_cfg = load_global_config()

    # Apply overrides (matching run_session.py flow)
    strategy = resolve_config(strategy, "XAUTUSDT")
    global_cfg = resolve_config(global_cfg, "XAUTUSDT")

    # Build full config (matching run_session.py)
    full = {**global_cfg, **strategy}

    from src.config.loader import load_regime_config, load_temporal_config, load_risk_config
    r = load_regime_config(full)
    t = load_temporal_config(full)
    k = load_risk_config(full)

    # Verify — XAUTUSDT: base values + overrides
    assert r.trend_intensity_min_expansion == 0.08
    assert r.trend_intensity_threshold == 0.25
    assert t.min_trade_velocity == 0.4
    assert k.max_holding_hours == 72.0


def test_full_config_pipeline_evolution():
    """Simulate the run_evolution.py data flow."""
    cfg = load_and_resolve_for_symbol("XAUTUSDT")

    # XAUTUSDT: base values + overrides
    assert cfg["regime_parameters"]["trend"]["trend_intensity_min_expansion"] == 0.08
    assert cfg["sniper"]["signal_stack"]["thresholds"]["cvd_divergence_tick_delta"] == 0.12
    assert "strategy_intent" in cfg  # from strategy_config


def test_full_config_pipeline_sniper():
    """Simulate the sniper daemon flow."""
    from src.utils.pipeline_utils import load_config, load_global_config
    from src.config.symbol_resolver import resolve_config

    strategy = load_config()
    global_cfg = load_global_config()

    strategy = resolve_config(strategy, "XAUTUSDT")
    global_cfg = resolve_config(global_cfg, "XAUTUSDT")

    # XAUTUSDT: base values + overrides
    assert global_cfg["sniper"]["signal_stack"]["thresholds"]["cvd_divergence_tick_delta"] == 0.12
    assert strategy["regime_parameters"]["trend"]["trend_intensity_min_expansion"] == 0.08
    # Non-overridden sniper values
    assert global_cfg["sniper"]["probes"]["cvd_growth_significance_ratio"] == 1.4


# ── Config loader integration ───────────────────────────────────────────────


def test_loaders_work_with_resolved_config():
    """All sub-config loaders work with a load_and_resolve_for_symbol() result."""
    from src.config.loader import (
        load_regime_config, load_temporal_config, load_risk_config,
        load_audit_config, load_visual_config,
    )

    cfg = load_and_resolve_for_symbol("XAUTUSDT")

    # All should load without error
    r = load_regime_config(cfg)
    t = load_temporal_config(cfg)
    k = load_risk_config(cfg)
    a = load_audit_config(cfg)
    v = load_visual_config(cfg)

    # Base values and overrides (XAUTUSDT overrides stop_loss_buffer_min to 1.6)
    assert r.trend_intensity_min_expansion == 0.08
    assert k.stop_loss_buffer_min == 1.6
    assert t.min_trade_velocity == 0.4
    assert a.mae_threshold_pinpoint == 20.0
    assert v.render_dpi == 120


def test_config_snapshot_consistency():
    """config_snapshot from session matches evolver active_config."""
    xaut = load_and_resolve_for_symbol("XAUTUSDT")
    btc = load_and_resolve_for_symbol("BTCUSDT")

    # XAUTUSDT uses base values (no overrides)
    assert xaut["regime_parameters"]["trend"]["trend_intensity_min_expansion"] == 0.08
    # BTCUSDT overrides push above base
    assert btc["regime_parameters"]["trend"]["trend_intensity_min_expansion"] == 0.12
    # Both share other base defaults
    assert xaut["regime_parameters"]["volatility"]["volatility_extreme_ratio"] == 2.2
    assert btc["regime_parameters"]["volatility"]["volatility_extreme_ratio"] == 2.2


# ── Prompt template variable reachability ────────────────────────────────────


def test_all_prompt_template_vars_reachable():
    """Every {placeholder} in prompt .md files is reachable from the resolved config."""
    import re
    from src.utils.pipeline_utils import read_prompt_template
    from src.utils.path_utils import resolve_project_root

    cfg = load_and_resolve_for_symbol("XAUTUSDT")
    from src.config.loader import load_regime_config, load_temporal_config, load_risk_config, load_audit_config
    r = load_regime_config(cfg)
    t = load_temporal_config(cfg)
    k = load_risk_config(cfg)
    a = load_audit_config(cfg)

    # Collect all reachable values from dataclasses
    reachable = {}
    for dc, prefix in [(r, ""), (t, ""), (k, ""), (a, "")]:
        for field_name in dc.__dataclass_fields__:
            reachable[field_name] = getattr(dc, field_name)
    reachable["strategy_intent"] = cfg.get("strategy_intent", "")

    # Extract all {placeholders} from prompt files
    prompt_files = [
        "config/prompts/binary_star.md",
        "config/prompts/session.md",
        "config/prompts/critic.md",
        "config/prompts/evolver.md",
    ]

    placeholders = set()
    for pf in prompt_files:
        path = os.path.join(resolve_project_root(), pf)
        content = read_prompt_template(path)
        placeholders.update(re.findall(r'\{(\w+)\}', content))

    # Filter out non-config placeholders (observation_json, debate_history_json, etc.)
    runtime_vars = {
        "observation_json", "debate_history_json", "last_plan", "math_fact_check",
        "audit_reports_json", "active_config_yaml", "current_prompt_md",
        "regime_parameters", "max_rounds",
        "precomputed_regime_states", "precomputed_critic_states",
        "precomputed_session_states", "precomputed_evolver_states",
        # Evolver runtime-injected variables (not from config dataclasses)
        "time_calibration_report", "fill_rate_pct", "near_miss_rate",
        "mae_stress_distribution", "cowardice_tag_rate",
    }

    config_placeholders = placeholders - runtime_vars

    # Every config placeholder must be reachable
    missing = [p for p in config_placeholders if p not in reachable]
    assert not missing, f"Unreachable prompt placeholders: {missing}"

    # Verify key values are not None
    for p in config_placeholders:
        val = reachable.get(p)
        assert val is not None, f"Placeholder {{{p}}} resolved to None"


# ── Config validation ────────────────────────────────────────────────────────

def test_validate_symbol_configs_with_real_file():
    """Real symbol_config.yaml should pass validation."""
    from src.config.symbol_resolver import validate_symbol_configs
    errors = validate_symbol_configs()
    assert errors == [], f"Validation errors in symbol_config.yaml: {errors}"


def test_validate_symbol_configs_missing_params():
    """Symbol missing required trade params should produce errors."""
    from src.config.symbol_resolver import validate_symbol_configs
    from unittest.mock import patch

    bad_config = {
        "BADSYMBOL": {
            "precision_qty": 4,
            # missing precision_price, min_order_qty, sl_slippage_buffer
        },
    }
    with patch("src.config.symbol_resolver.load_symbol_config", return_value=bad_config):
        errors = validate_symbol_configs()
        assert len(errors) >= 3  # missing precision_price, min_order_qty, sl_slippage_buffer


def test_validate_symbol_configs_non_dict_overrides():
    """Non-dict overrides should produce an error."""
    from src.config.symbol_resolver import validate_symbol_configs
    from unittest.mock import patch

    bad_config = {
        "BADSYMBOL": {
            "precision_qty": 4,
            "precision_price": 1,
            "min_order_qty": 0.001,
            "sl_slippage_buffer": 10.0,
            "overrides": "not_a_dict",
        },
    }
    with patch("src.config.symbol_resolver.load_symbol_config", return_value=bad_config):
        errors = validate_symbol_configs()
        assert any("overrides" in e for e in errors)


def test_validate_symbol_configs_non_dict_section():
    """Non-dict override section should produce an error."""
    from src.config.symbol_resolver import validate_symbol_configs
    from unittest.mock import patch

    bad_config = {
        "BADSYMBOL": {
            "precision_qty": 4,
            "precision_price": 1,
            "min_order_qty": 0.001,
            "sl_slippage_buffer": 10.0,
            "overrides": {"regime_parameters": "not_a_dict"},
        },
    }
    with patch("src.config.symbol_resolver.load_symbol_config", return_value=bad_config):
        errors = validate_symbol_configs()
        assert any("regime_parameters" in e for e in errors)


def test_validate_symbol_configs_unknown_section():
    """Override section not present in base config should produce an error."""
    from src.config.symbol_resolver import validate_symbol_configs
    from unittest.mock import patch

    bad_config = {
        "BADSYMBOL": {
            "precision_qty": 4,
            "precision_price": 1,
            "min_order_qty": 0.001,
            "sl_slippage_buffer": 10.0,
            "overrides": {"unknown_invalid_section": {}},
        },
    }
    with patch("src.config.symbol_resolver.load_symbol_config", return_value=bad_config):
        errors = validate_symbol_configs()
        assert any("unknown_invalid_section" in e for e in errors)

