# SENSEX Expiry Engine — Baseline Validation (SYNTHETIC RANDOM WALK - PIPELINE CHECK ONLY, NOT EVIDENCE)

config `b6a274f0c76a5c9a` · optimisation: **NONE - locked EngineConfig() as committed** · days 180 (2025-09-02 → 2026-10-22) · expiry days 60 (with options: 60)

## VERDICT: **INCONCLUSIVE**

- option trades n=8, mean=0.886R, CI=(-0.301, 2.681) includes 0 or n<60
- ~1681 option trades needed to detect +0.15R at 80% power (one-sided 5%)

## Look-ahead audit
```
{
 "truncation_equality": {
  "checked": 8,
  "passed": 8,
  "ok": true
 },
 "future_poison_invariance": {
  "checked": 8,
  "passed": 8,
  "ok": true
 },
 "underlying_poison_invariance": {
  "checked": 8,
  "passed": 8,
  "ok": true
 },
 "prior_day_is_previous_session": {
  "checked": 179,
  "passed": 179,
  "ok": true
 },
 "entries_after_signal_bar": {
  "checked": 1,
  "passed": 1,
  "ok": true
 },
 "testA_truncation_equality": {
  "checked": 8,
  "passed": 8,
  "ok": true
 },
 "ALL_PASS": true,
 "data_consistency_prior_vs_intraday": {
  "checked": 179,
  "passed": 179
 }
}
```

## Tests A / B (R units; A = index points / stop, no costs; B = option rupees / 1R, net of costs)

| test | n | win_rate | avg_win_r | avg_loss_r | expectancy_r | profit_factor | total_r | max_drawdown_r | avg_mae_r | avg_mfe_r | avg_hold_min | best_r | worst_r | worst_losing_streak | total_costs_rs | total_slippage_rs | gross_expectancy_r | net_expectancy_r |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A all days | 54 | 0.426 | 1.376 | -0.830 | 0.109 | 1.229 | 5.897 | 7.357 | -0.704 | 1.458 | 15.111 | 5.403 | -1.200 | 8 | 0.000 | 0.000 | 0.109 | 0.109 |
| A expiry days | 19 | 0.421 | 1.341 | -0.633 | 0.198 | 1.541 | 3.769 | 3.510 | -0.679 | 1.480 | 18.000 | 5.403 | -1.200 | 5 | 0.000 | 0.000 | 0.198 | 0.198 |
| B expiry options | 8 | 0.750 | 1.445 | -0.793 | 0.886 | 5.465 | 7.085 | 1.587 | -0.426 | 2.490 | 11.625 | 6.748 | -0.868 | 2 | 502.860 | 350.170 | 0.918 | 0.886 |

### Significance
```
{
 "TEST_A_all_days_underlying": {
  "n": 54,
  "mean_r": 0.1092074074074074,
  "sd_r": 1.4260203302226255,
  "t_ci95": [
   -0.27114389575292475,
   0.4895587105677396
  ],
  "bootstrap_ci95": [
   -0.24907407407407406,
   0.4946962962962963
  ],
  "prob_mean_gt_0": 0.7034,
  "cohens_d": 0.07658194283272127,
  "p_signflip_mean_gt_0": 0.2952,
  "n_needed_for_0.15R_80pct_power": 560,
  "n_needed_for_observed_effect": 1055
 },
 "TEST_B_expiry_options": {
  "n": 8,
  "mean_r": 0.8856375000000001,
  "sd_r": 2.472229480759364,
  "t_ci95": [
   -0.8275301258843584,
   2.5988051258843585
  ],
  "bootstrap_ci95": [
   -0.3007875,
   2.6809125000000003
  ],
  "prob_mean_gt_0": 0.8518,
  "cohens_d": 0.35823434146896826,
  "p_signflip_mean_gt_0": 0.202,
  "n_needed_for_0.15R_80pct_power": 1681,
  "n_needed_for_observed_effect": 49
 }
}
```

## Test C — setups (A)

| test | n | win_rate | avg_win_r | avg_loss_r | expectancy_r | profit_factor | total_r | max_drawdown_r | avg_mae_r | avg_mfe_r | avg_hold_min | best_r | worst_r | worst_losing_streak | total_costs_rs | total_slippage_rs | gross_expectancy_r | net_expectancy_r |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S1_SWEEP_RECLAIM | 51 | 0.451 | 1.376 | -0.818 | 0.171 | 1.381 | 8.729 | 6.854 | -0.679 | 1.513 | 15.588 | 5.403 | -1.200 | 7 | 0.000 | 0.000 | 0.171 | 0.171 |
| S2_ORB_ACCEPT | 3 | 0.000 | 0.000 | -0.944 | -0.944 | 0.000 | -2.831 | 2.831 | -1.130 | 0.518 | 7.000 | -0.502 | -1.200 | 3 | 0.000 | 0.000 | -0.944 | -0.944 |
| S3_LATE_COMPRESSION | 0 | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - |

## Test D — windows by signal time (A)

| test | n | win_rate | avg_win_r | avg_loss_r | expectancy_r | profit_factor | total_r | max_drawdown_r | avg_mae_r | avg_mfe_r | avg_hold_min | best_r | worst_r | worst_losing_streak | total_costs_rs | total_slippage_rs | gross_expectancy_r | net_expectancy_r |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 09:15-09:30 | 0 | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - |
| 09:30-10:00 | 9 | 0.667 | 0.821 | -1.134 | 0.169 | 1.448 | 1.523 | 2.343 | -0.616 | 1.449 | 14.667 | 1.734 | -1.200 | 2 | 0.000 | 0.000 | 0.169 | 0.169 |
| 10:00-11:00 | 17 | 0.471 | 1.423 | -0.661 | 0.320 | 1.915 | 5.437 | 1.867 | -0.593 | 1.692 | 15.235 | 2.645 | -1.200 | 5 | 0.000 | 0.000 | 0.320 | 0.320 |
| 11:00-12:00 | 11 | 0.182 | 0.913 | -0.821 | -0.506 | 0.247 | -5.564 | 5.564 | -0.815 | 0.869 | 13.727 | 1.178 | -1.200 | 6 | 0.000 | 0.000 | -0.506 | -0.506 |
| 12:00-13:00 | 10 | 0.600 | 2.186 | -1.086 | 0.877 | 3.020 | 8.774 | 2.034 | -0.723 | 2.200 | 14.500 | 5.403 | -1.200 | 2 | 0.000 | 0.000 | 0.877 | 0.877 |
| 13:00-14:00 | 3 | 0.000 | 0.000 | -0.993 | -0.993 | 0.000 | -2.978 | 2.978 | -1.120 | 0.193 | 7.000 | -0.749 | -1.200 | 3 | 0.000 | 0.000 | -0.993 | -0.993 |
| 14:00-14:30 | 2 | 0.000 | 0.000 | -0.241 | -0.241 | 0.000 | -0.481 | 0.481 | -0.773 | 1.126 | 35.000 | 0.000 | -0.481 | 2 | 0.000 | 0.000 | -0.241 | -0.241 |
| 14:30-15:10 | 2 | 0.500 | 0.386 | -1.200 | -0.407 | 0.322 | -0.814 | 1.200 | -0.643 | 1.269 | 19.000 | 0.386 | -1.200 | 1 | 0.000 | 0.000 | -0.407 | -0.407 |

## Test E — regime at signal (A)

| test | n | win_rate | avg_win_r | avg_loss_r | expectancy_r | profit_factor | total_r | max_drawdown_r | avg_mae_r | avg_mfe_r | avg_hold_min | best_r | worst_r | worst_losing_streak | total_costs_rs | total_slippage_rs | gross_expectancy_r | net_expectancy_r |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Trend | 2 | 0.000 | 0.000 | -0.626 | -0.626 | 0.000 | -1.251 | 1.251 | -0.803 | 0.390 | 15.000 | -0.502 | -0.749 | 2 | 0.000 | 0.000 | -0.626 | -0.626 |
| Range | 23 | 0.348 | 1.762 | -0.676 | 0.172 | 1.389 | 3.952 | 3.254 | -0.678 | 1.572 | 13.957 | 4.162 | -1.200 | 7 | 0.000 | 0.000 | 0.172 | 0.172 |
| Compression | 2 | 1.000 | 0.587 | 0.000 | 0.587 | inf | 1.173 | 0.000 | -0.162 | 1.824 | 13.500 | 0.649 | 0.524 | 0 | 0.000 | 0.000 | 0.587 | 0.587 |
| Expansion | 1 | 0.000 | 0.000 | -1.200 | -1.200 | 0.000 | -1.200 | 1.200 | -1.286 | 0.627 | 21.000 | -1.200 | -1.200 | 1 | 0.000 | 0.000 | -1.200 | -1.200 |
| Reversal | 18 | 0.556 | 0.790 | -0.987 | 0.000 | 1.000 | 0.001 | 5.201 | -0.659 | 1.275 | 17.889 | 1.734 | -1.200 | 4 | 0.000 | 0.000 | 0.000 | 0.000 |
| Unclear | 8 | 0.375 | 2.823 | -1.049 | 0.403 | 1.614 | 3.222 | 3.332 | -0.914 | 1.820 | 11.875 | 5.403 | -1.200 | 3 | 0.000 | 0.000 | 0.403 | 0.403 |

### Chronological 70/30 (A)

| test | n | win_rate | avg_win_r | avg_loss_r | expectancy_r | profit_factor | total_r | max_drawdown_r | avg_mae_r | avg_mfe_r | avg_hold_min | best_r | worst_r | worst_losing_streak | total_costs_rs | total_slippage_rs | gross_expectancy_r | net_expectancy_r |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| first_70pct_days | 37 | 0.541 | 1.393 | -0.939 | 0.321 | 1.745 | 11.894 | 4.562 | -0.652 | 1.725 | 15.270 | 5.403 | -1.200 | 2 | 0.000 | 0.000 | 0.321 | 0.321 |
| last_30pct_days | 17 | 0.176 | 1.260 | -0.698 | -0.353 | 0.387 | -5.997 | 7.010 | -0.815 | 0.877 | 14.765 | 2.213 | -1.200 | 8 | 0.000 | 0.000 | -0.353 | -0.353 |

## Test C — setups (B)

| test | n | win_rate | avg_win_r | avg_loss_r | expectancy_r | profit_factor | total_r | max_drawdown_r | avg_mae_r | avg_mfe_r | avg_hold_min | best_r | worst_r | worst_losing_streak | total_costs_rs | total_slippage_rs | gross_expectancy_r | net_expectancy_r |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S1_SWEEP_RECLAIM | 8 | 0.750 | 1.445 | -0.793 | 0.886 | 5.465 | 7.085 | 1.587 | -0.426 | 2.490 | 11.625 | 6.748 | -0.868 | 2 | 502.860 | 350.170 | 0.918 | 0.886 |
| S2_ORB_ACCEPT | 0 | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - |
| S3_LATE_COMPRESSION | 0 | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - |

## Test D — windows by signal time (B)

| test | n | win_rate | avg_win_r | avg_loss_r | expectancy_r | profit_factor | total_r | max_drawdown_r | avg_mae_r | avg_mfe_r | avg_hold_min | best_r | worst_r | worst_losing_streak | total_costs_rs | total_slippage_rs | gross_expectancy_r | net_expectancy_r |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 09:15-09:30 | 0 | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - |
| 09:30-10:00 | 0 | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - |
| 10:00-11:00 | 3 | 1.000 | 0.579 | 0.000 | 0.579 | inf | 1.738 | 0.000 | -0.313 | 1.853 | 10.667 | 1.463 | 0.001 | 0 | 185.720 | 121.380 | 0.612 | 0.579 |
| 11:00-12:00 | 1 | 1.000 | 0.002 | 0.000 | 0.002 | inf | 0.002 | 0.000 | -0.211 | 2.152 | 13.000 | 0.002 | 0.002 | 0 | 62.370 | 44.830 | 0.041 | 0.002 |
| 12:00-13:00 | 3 | 0.333 | 6.748 | -0.793 | 1.721 | 4.253 | 5.162 | 1.587 | -0.748 | 3.285 | 10.333 | 6.748 | -0.868 | 2 | 197.200 | 150.160 | 1.752 | 1.721 |
| 13:00-14:00 | 0 | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - |
| 14:00-14:30 | 0 | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - |
| 14:30-15:10 | 1 | 1.000 | 0.184 | 0.000 | 0.184 | inf | 0.184 | 0.000 | -0.013 | 2.351 | 17.000 | 0.184 | 0.184 | 0 | 57.570 | 33.800 | 0.211 | 0.184 |

## Test E — regime at signal (B)

| test | n | win_rate | avg_win_r | avg_loss_r | expectancy_r | profit_factor | total_r | max_drawdown_r | avg_mae_r | avg_mfe_r | avg_hold_min | best_r | worst_r | worst_losing_streak | total_costs_rs | total_slippage_rs | gross_expectancy_r | net_expectancy_r |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Trend | 0 | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - |
| Range | 4 | 0.500 | 0.093 | -0.793 | -0.350 | 0.117 | -1.401 | 1.587 | -0.440 | 1.126 | 8.500 | 0.184 | -0.868 | 2 | 232.390 | 155.360 | -0.321 | -0.350 |
| Compression | 0 | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - |
| Expansion | 0 | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - |
| Reversal | 2 | 1.000 | 0.138 | 0.000 | 0.138 | inf | 0.275 | 0.000 | -0.375 | 1.301 | 9.000 | 0.274 | 0.001 | 0 | 120.890 | 76.240 | 0.171 | 0.138 |
| Unclear | 2 | 1.000 | 4.106 | 0.000 | 4.106 | inf | 8.211 | 0.000 | -0.448 | 6.405 | 20.500 | 6.748 | 1.463 | 0 | 149.580 | 118.570 | 4.143 | 4.106 |

### Chronological 70/30 (B)

| test | n | win_rate | avg_win_r | avg_loss_r | expectancy_r | profit_factor | total_r | max_drawdown_r | avg_mae_r | avg_mfe_r | avg_hold_min | best_r | worst_r | worst_losing_streak | total_costs_rs | total_slippage_rs | gross_expectancy_r | net_expectancy_r |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| first_70pct_days | 4 | 0.500 | 3.511 | -0.793 | 1.359 | 4.426 | 5.436 | 1.587 | -0.591 | 2.795 | 9.750 | 6.748 | -0.868 | 2 | 258.780 | 192.220 | 1.389 | 1.359 |
| last_30pct_days | 4 | 1.000 | 0.412 | 0.000 | 0.412 | inf | 1.650 | 0.000 | -0.261 | 2.184 | 13.500 | 1.463 | 0.001 | 0 | 244.080 | 157.950 | 0.447 | 0.412 |

## Controls (Test A, underlying)
```
{
 "C1_RANDOM_ENTRY": {
  "null_mean_of_means": 0.004587055555555568,
  "null_p95": 0.42967222222222223,
  "randomization_p": 0.3465346534653465
 },
 "C2_RANDOM_DIRECTION": {
  "null_mean_of_means": -0.02646057407407407,
  "null_p95": 0.21194444444444446,
  "randomization_p": 0.18811881188118812
 },
 "C3_RANDOM_SAME_HOLD": {
  "null_mean_of_means": 0.007899481481481481,
  "null_p95": 0.39654074074074075,
  "randomization_p": 0.2871287128712871
 },
 "C4_RANDOM_SAME_STOP": {
  "null_mean_of_means": 0.05918275925925927,
  "null_p95": 0.47454074074074076,
  "randomization_p": 0.3465346534653465
 },
 "C6_SIMPLE_ORB": {
  "metrics": {
   "n": 180,
   "win_rate": 0.2388888888888889,
   "avg_win_r": 3.1334837209302324,
   "avg_loss_r": -0.9707474452554744,
   "expectancy_r": 0.009707777777777774,
   "median_r": -1.0,
   "p25": -1.0,
   "p75": -0.1977,
   "p90": 3.3441,
   "p95": 4.8032,
   "best_r": 8.7714,
   "worst_r": -1.0,
   "total_r": 1.7473999999999994,
   "profit_factor": 1.0131390966701856,
   "sharpe_per_trade": 0.004747784879831462,
   "sortino_per_trade": 0.011338166917535182,
   "max_drawdown_r": 22.800299999999993,
   "recovery_factor": 0.07663934246479212,
   "worst_losing_streak": 14,
   "expectancy_ex_top5_r": -0.194144,
   "share_of_profit_from_top5": 20.44328716950899,
   "avg_hold_min": 144.54444444444445,
   "avg_mfe_r": 1.7015288888888889,
   "gross_expectancy_r": 0.009707777777777774,
   "net_expectancy_r": 0.009707777777777774,
   "avg_mae_r": -0.93022,
   "total_costs_rs": 0.0,
   "total_slippage_rs": 0.0,
   "total_risk_rs": 0
  },
  "perm_p_strategy_better": 0.367
 },
 "C7_SIMPLE_BREAKOUT": {
  "metrics": {
   "n": 152,
   "win_rate": 0.11842105263157894,
   "avg_win_r": 8.413783333333333,
   "avg_loss_r": -1.0,
   "expectancy_r": 0.1147901315789474,
   "median_r": -1.0,
   "p25": -1.0,
   "p75": -1.0,
   "p90": 3.1966,
   "p95": 8.2684,
   "best_r": 19.2495,
   "worst_r": -1.0,
   "total_r": 17.448100000000004,
   "profit_factor": 1.1302097014925374,
   "sharpe_per_trade": 0.03272494543110914,
   "sortino_per_trade": 0.12225706019360937,
   "max_drawdown_r": 37.0,
   "recovery_factor": 0.4715702702702704,
   "worst_losing_streak": 37,
   "expectancy_ex_top5_r": -0.40455986394557825,
   "share_of_profit_from_top5": 4.4084112310222885,
   "avg_hold_min": 55.07236842105263,
   "avg_mfe_r": 2.516707894736842,
   "gross_expectancy_r": 0.1147901315789474,
   "net_expectancy_r": 0.1147901315789474,
   "avg_mae_r": -1.1687697368421053,
   "total_costs_rs": 0.0,
   "total_slippage_rs": 0.0,
   "total_risk_rs": 0
  },
  "perm_p_strategy_better": 0.4874
 },
 "C8_BUY_AND_HOLD": {
  "metrics": {
   "n": 180,
   "win_rate": 0.5055555555555555,
   "avg_win_r": 5.964891208791209,
   "avg_loss_r": -6.167459550561798,
   "expectancy_r": -0.0338822222222223,
   "median_r": 0.12435,
   "p25": -5.6955,
   "p75": 4.4529,
   "p90": 10.2345,
   "p95": 13.3088,
   "best_r": 24.1253,
   "worst_r": -20.8886,
   "total_r": -6.098800000000014,
   "profit_factor": 0.9888891297729895,
   "sharpe_per_trade": -0.004387040720670297,
   "sortino_per_trade": -0.006294212528361915,
   "max_drawdown_r": 103.35780000000001,
   "recovery_factor": -0.05900667390366294,
   "worst_losing_streak": 5,
   "expectancy_ex_top5_r": -0.5735405714285715,
   "share_of_profit_from_top5": NaN,
   "avg_hold_min": 335.0,
   "avg_mfe_r": 5.685056666666667,
   "gross_expectancy_r": -0.0338822222222223,
   "net_expectancy_r": -0.0338822222222223,
   "avg_mae_r": -5.712442222222222,
   "total_costs_rs": 0.0,
   "total_slippage_rs": 0.0,
   "total_risk_rs": 0
  },
  "perm_p_strategy_better": 0.4506,
  "mean_day_pct": -0.016282705463725147
 }
}
```

## Controls (Test B, options)
```
{
 "C1_RANDOM_ENTRY": {
  "null_mean_of_means": 0.03040000000000001,
  "randomization_p": 0.14285714285714285
 },
 "C2_RANDOM_DIRECTION": {
  "null_mean_of_means": 0.11476437499999999,
  "randomization_p": 0.14285714285714285
 },
 "C4_RANDOM_SAME_STOP": {
  "null_mean_of_means": 0.13242508928571428,
  "randomization_p": 0.14285714285714285
 },
 "C5_SIMPLE_ATM_BUY": {
  "metrics": {
   "n": 60,
   "win_rate": 0.05,
   "avg_win_r": 14.905266666666666,
   "avg_loss_r": -1.0,
   "expectancy_r": -0.20473666666666668,
   "median_r": -1.0,
   "p25": -1.0,
   "p75": -1.0,
   "p90": -1.0,
   "p95": -1.0,
   "best_r": 19.0197,
   "worst_r": -1.0,
   "total_r": -12.2842,
   "profit_factor": 0.7844877192982457,
   "sharpe_per_trade": -0.057805973496786685,
   "sortino_per_trade": -0.21005538787807418,
   "max_drawdown_r": 24.0,
   "recovery_factor": -0.5118416666666666,
   "worst_losing_streak": 24,
   "expectancy_ex_top5_r": -1.0,
   "share_of_profit_from_top5": NaN,
   "avg_hold_min": 47.983333333333334,
   "avg_mfe_r": 3.2565933333333335,
   "gross_expectancy_r": -0.13570833333333332,
   "net_expectancy_r": -0.20473666666666668,
   "avg_mae_r": -1.1077316666666666,
   "total_costs_rs": 3151.6,
   "total_slippage_rs": 941.89,
   "total_risk_rs": 46140.15
  },
  "perm_p_strategy_better": 0.234
 }
}
```

## Multiple-testing correction
```
{
 "raw_p": {
  "A:S1_SWEEP_RECLAIM": 0.23529411764705882,
  "B:S1_SWEEP_RECLAIM": 0.202,
  "A:S2_ORB_ACCEPT": 0.9411764705882353,
  "A:ALL": 0.3465346534653465,
  "B:ALL": 0.202
 },
 "holm_adjusted": {
  "B:ALL": 1.0,
  "B:S1_SWEEP_RECLAIM": 1.0,
  "A:S1_SWEEP_RECLAIM": 1.0,
  "A:ALL": 1.0,
  "A:S2_ORB_ACCEPT": 1.0
 },
 "bh_fdr_adjusted": {
  "A:S2_ORB_ACCEPT": 0.9411764705882353,
  "A:ALL": 0.4331683168316831,
  "A:S1_SWEEP_RECLAIM": 0.39215686274509803,
  "B:S1_SWEEP_RECLAIM": 0.39215686274509803,
  "B:ALL": 0.39215686274509803
 },
 "family_size": 5,
 "note": "every setup x test tried is in the family; nothing was selected"
}
```

## Trade lists

Complete lists: `trades_testA_underlying.csv`, `trades_testB_options.csv`, `trades_control_C5_simple_atm_buy.csv`.

### Test B trades

| day | setup | dir | regime | entry_ts | exit_ts | entry | exit | qty | 1R ₹ | R gross | R net | MFE | MAE | exit |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2025-10-09 | S1_SWEEP_RECLAIM | LONG | UNCLEAR | 12:49 | 13:16 | 99.8 | 310.07 | 60 | 1856.98 | 6.7939 | 6.7483 | 9.8545 | -0.706 | EXIT_TRAIL |
| 2025-10-30 | S1_SWEEP_RECLAIM | SHORT | FAILED_BREAKOUT | 10:13 | 10:21 | 94.39 | 106.38 | 60 | 2401.2 | 0.2996 | 0.274 | 1.3265 | -0.1206 | EXIT_TRAIL |
| 2025-11-27 | S1_SWEEP_RECLAIM | LONG | RANGE | 12:19 | 12:21 | 65.41 | 46.23 | 80 | 2211.97 | -0.6937 | -0.7192 | 0.0 | -0.6937 | EXIT_INVALIDATION |
| 2026-01-22 | S1_SWEEP_RECLAIM | LONG | RANGE | 12:22 | 12:24 | 68.52 | 43.99 | 80 | 2326.72 | -0.8434 | -0.8675 | 0.0 | -0.8434 | EXIT_INVALIDATION |
| 2026-02-05 | S1_SWEEP_RECLAIM | SHORT | RANGE | 11:34 | 11:47 | 109.07 | 110.16 | 60 | 1580.98 | 0.0414 | 0.0019 | 2.1522 | -0.211 | EXIT_STOP_PREMIUM |
| 2026-02-05 | S1_SWEEP_RECLAIM | LONG | RANGE | 14:35 | 14:52 | 69.07 | 76.51 | 60 | 2116.84 | 0.2109 | 0.1837 | 2.3507 | -0.0125 | EXIT_TRAIL |
| 2026-03-05 | S1_SWEEP_RECLAIM | LONG | UNCLEAR | 10:53 | 11:07 | 83.76 | 137.51 | 60 | 2160.19 | 1.4929 | 1.4629 | 2.9564 | -0.1894 | EXIT_TRAIL |
| 2026-05-21 | S1_SWEEP_RECLAIM | SHORT | FAILED_BREAKOUT | 10:53 | 11:03 | 130.37 | 131.89 | 40 | 1428.6 | 0.0426 | 0.001 | 1.2765 | -0.63 | EXIT_STOP_PREMIUM |
