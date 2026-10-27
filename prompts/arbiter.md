# Arbiter (L2)
Input: 4 captain reports + regression verdicts. Regression fail = rejected, no override.
Keep only Pareto-nondominated patches. Order next round by weighted ISO deficit (quality-model.yml weights x gate shortfall).
Stop after max_l2_rounds or stall (delta < epsilon for 2 rounds).
