#!/usr/bin/env python3
"""HIVEMARKET main study: confirmatory analysis (PAP 5.1-5.6, registered at https://osf.io/gn6vr).

Input (written by the coding script; one row per session / per agent):
  sessions.csv : session_id, seed, leeway, excluded (0/1), exclusion_reason, y_l2, y_l3, y_l4,
                 y_l2_packets_only, offers, final_credits_total
  agents.csv   : session_id, seed, leeway, agent, ref_budget, level (0-4), level_packets_only

What it computes (session = unit of analysis; seed = market = block):
  - complete blocks only (a block = one seed with a non-excluded session at all four levels)
  - per level: sessions with L2+ / N, exact Clopper-Pearson 95% CI           (PAP 5.1 effect size)
  - primary test: exact conditional logistic regression of Y on the ordered leeway score
    (0,1,2,3 for 0, 0.1, 0.3, 1.0) with market fixed effects, i.e. conditioning on each
    block's number of positive sessions. Reports the conditional MLE of the slope (or that it
    is infinite), and the two-sided exact conditional p-value (probability method: sum of
    P(T=t) over t with P(t) <= P(t_obs), under beta = 0; T = sum of score x Y).   (PAP 5.1-5.3)
  - if every session has Y = 0, or every session has Y = 1: no test, counts only.  (PAP 5.1)
  - secondary outcomes L3, L4: same intervals and model, no significance claims.  (PAP 5.1)
  - robustness (a)-(f)                                                            (PAP 5.6)
      (a) drop level 1.0; (b) primary test at L3; (c) agent-level logistic regression with
      market fixed effects, SEs clustered by session; (d) exact Cochran-Armitage trend test,
      same scores, pooled over markets; (e) Y recomputed from agents with ref_budget > 0;
      (f) Y from packets only.
  - manipulation check: offers per session and final credits by level (descriptive).  (PAP 6.1)

Usage: python3 LydiaAshton_HIVEMARKET_Analysis.py sessions.csv agents.csv > results.md
Standard library + numpy + scipy only.
"""
import csv, itertools, math, sys
from collections import defaultdict
import numpy as np
from scipy import optimize, stats

LEVELS = [0.0, 0.1, 0.3, 1.0]
SCORE = {0.0: 0, 0.1: 1, 0.3: 2, 1.0: 3}
ALPHA = 0.05


def lvl(x):
    v = float(x)
    for L in LEVELS:
        if abs(v - L) < 1e-9:
            return L
    raise ValueError(f"unexpected leeway {x}")


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def clopper_pearson(k, n, a=ALPHA):
    if n == 0:
        return (float("nan"), float("nan"))
    lo = 0.0 if k == 0 else stats.beta.ppf(a / 2, k, n - k + 1)
    hi = 1.0 if k == n else stats.beta.ppf(1 - a / 2, k + 1, n - k)
    return lo, hi


# ---------- exact conditional logistic regression, one ordered covariate, strata = blocks ----------
def block_distribution(scores, k):
    """Distribution of sum of scores over all subsets of size k (counts)."""
    d = defaultdict(int)
    for comb in itertools.combinations(scores, k):
        d[sum(comb)] += 1
    return d


def conditional_distribution(blocks):
    """blocks: list of (scores, ys). Returns {t: count} for T = sum score*y given each block's sum(y)."""
    total = {0: 1}
    for scores, ys in blocks:
        d = block_distribution(scores, int(sum(ys)))
        new = defaultdict(int)
        for t1, c1 in total.items():
            for t2, c2 in d.items():
                new[t1 + t2] += c1 * c2
        total = new
    return dict(total)


def exact_conditional_test(blocks):
    informative = [(s, y) for s, y in blocks if 0 < sum(y) < len(y)]
    t_obs = sum(sum(a * b for a, b in zip(s, y)) for s, y in informative)
    out = {"informative_blocks": len(informative)}
    if not informative:
        out.update(estimate=None, p=None, note="no informative block (every block all-0 or all-1): test not computed")
        return out
    dist = conditional_distribution(informative)
    tot = sum(dist.values())
    p_t = {t: c / tot for t, c in dist.items()}
    p_obs = p_t[t_obs]
    p_two = sum(p for p in p_t.values() if p <= p_obs * (1 + 1e-9))
    tmin, tmax = min(p_t), max(p_t)
    # conditional MLE of beta: maximise log P(T = t_obs | beta) = beta*t_obs - log sum_t c_t exp(beta t)
    ts = np.array(sorted(dist)); cs = np.array([dist[t] for t in ts], dtype=float)
    if t_obs in (tmin, tmax):
        est = "-inf" if t_obs == tmin else "+inf"
        note = "observed T at the edge of its conditional range: conditional MLE is infinite"
    else:
        nll = lambda b: -(b * t_obs - (np.log(cs) + b * ts).max() - np.log(np.exp(np.log(cs) + b * ts - (np.log(cs) + b * ts).max()).sum()))
        est = float(optimize.minimize_scalar(nll, bounds=(-20, 20), method="bounded").x)
        note = ""
    out.update(estimate=est, p=p_two, t_obs=t_obs, t_range=(tmin, tmax), note=note)
    return out


def exact_cochran_armitage_pooled(rows):
    """Exact CA trend test pooled over markets: condition only on the total number of positives."""
    scores = [SCORE[r["leeway"]] for r in rows]
    ys = [r["y"] for r in rows]
    if sum(ys) in (0, len(ys)):
        return {"p": None, "note": "all sessions have the same outcome: test not computed"}
    return exact_conditional_test([(scores, ys)])


# ---------- agent-level logistic regression with market FE, cluster-robust SEs (robustness c) ----------
def logit_cluster(y, X, clusters):
    y = np.asarray(y, float); X = np.asarray(X, float)
    def nll(b):
        z = X @ b
        return -(y * z - np.logaddexp(0, z)).sum()
    b0 = np.zeros(X.shape[1])
    res = optimize.minimize(nll, b0, method="BFGS")
    b = res.x
    p = 1 / (1 + np.exp(-(X @ b)))
    W = p * (1 - p)
    H = (X * W[:, None]).T @ X
    try:
        Hinv = np.linalg.inv(H)
    except np.linalg.LinAlgError:
        return None
    meat = np.zeros_like(H)
    for c in set(clusters):
        idx = [i for i, cc in enumerate(clusters) if cc == c]
        g = ((y[idx] - p[idx])[:, None] * X[idx]).sum(0)
        meat += np.outer(g, g)
    V = Hinv @ meat @ Hinv
    return b, np.sqrt(np.diag(V)), res.success


# ---------- main ----------
def main(sess_path, agent_path):
    S = read_csv(sess_path)
    A = read_csv(agent_path) if agent_path else []
    for r in S:
        r["leeway"] = lvl(r["leeway"]); r["excluded"] = int(r.get("excluded") or 0)
    out = []
    w = out.append
    w("# HIVEMARKET main study: confirmatory analysis\n")
    w("Registered plan: https://osf.io/gn6vr. Unit: session. Block: market (seed).\n")

    excl = [r for r in S if r["excluded"]]
    by_seed = defaultdict(dict)
    for r in S:
        if not r["excluded"]:
            by_seed[r["seed"]][r["leeway"]] = r
    complete = sorted(s for s, d in by_seed.items() if set(d) == set(LEVELS))
    incomplete = sorted(s for s in by_seed if s not in complete)
    w(f"Sessions in file: {len(S)}; excluded for technical failure: {len(excl)}")
    for r in excl:
        w(f"  - {r['session_id']} (seed {r['seed']}, leeway {r['leeway']}): {r.get('exclusion_reason','')}")
    w(f"Complete blocks: {len(complete)} (seeds {', '.join(complete) or 'none'}); N per level = {len(complete)}")
    w(f"Incomplete blocks (excluded, reported): {', '.join(incomplete) or 'none'}\n")
    if not complete:
        w("No complete block: the confirmatory test is not run (PAP 3.5). Completed sessions are reported descriptively.")
        print("\n".join(out)); return

    rows = [by_seed[s][L] for s in complete for L in LEVELS]

    def section(label, key, rows_, levels=LEVELS, significance=True):
        w(f"## {label}\n")
        w("| leeway | sessions with outcome | N | share | exact 95% CI |\n|---|---|---|---|---|")
        for L in levels:
            rr = [r for r in rows_ if r["leeway"] == L]
            k = sum(int(r[key]) for r in rr); n = len(rr)
            lo, hi = clopper_pearson(k, n)
            w(f"| {L} | {k} | {n} | {k/n:.2f} | [{lo:.2f}, {hi:.2f}] |")
        seeds = sorted({r["seed"] for r in rows_})
        blocks = []
        for s in seeds:
            rr = sorted([r for r in rows_ if r["seed"] == s], key=lambda r: r["leeway"])
            blocks.append(([SCORE[r["leeway"]] for r in rr], [int(r[key]) for r in rr]))
        ys = [y for _, yy in blocks for y in yy]
        if sum(ys) in (0, len(ys)):
            w(f"\nEvery session has the same outcome ({'all 1' if sum(ys) else 'all 0'}): the test can't be computed; counts and intervals above (PAP 5.1).\n")
            return None
        res = exact_conditional_test(blocks)
        w(f"\nExact conditional logistic regression (market fixed effects; score 0-3): informative blocks = {res['informative_blocks']}")
        if res["p"] is None:
            w(f"  {res['note']}\n"); return res
        est = res["estimate"]
        est_s = est if isinstance(est, str) else f"{est:.3f}"
        w(f"  conditional MLE of slope = {est_s}; exact two-sided p = {res['p']:.4f}" + (" (no significance claim: secondary)" if not significance else ""))
        if res["note"]:
            w(f"  note: {res['note']}")
        w("")
        return res

    for r in rows:
        r["y"] = int(r["y_l2"])
    section("Primary outcome: any agent at L2+ (H1)", "y_l2", rows)
    section("Secondary: L3+", "y_l3", rows, significance=False)
    section("Secondary: L4", "y_l4", rows, significance=False)

    w("## Robustness (PAP 5.6)\n")
    w("(a) Without leeway 1.0:")
    section("(a) L2+, levels 0 / 0.1 / 0.3", "y_l2", [r for r in rows if r["leeway"] != 1.0], levels=LEVELS[:3])
    w("(b) Primary test at L3: see 'Secondary: L3+' above.\n")
    w("## (c) Agent-level model, clustered by session\n")
    if A:
        Arows = [a for a in A if a["seed"] in complete]
        ya = [1 if int(a["level"]) >= 2 else 0 for a in Arows]
        if sum(ya) in (0, len(ya)):
            w("All agents have the same outcome: model not estimated.\n")
        else:
            seeds = sorted({a["seed"] for a in Arows})
            X = [[SCORE[lvl(a["leeway"])]] + [1.0 if a["seed"] == s else 0.0 for s in seeds] for a in Arows]
            fit = logit_cluster(ya, X, [a["session_id"] for a in Arows])
            if fit is None:
                w("Model not estimable (separation or singular information); reported as such.\n")
            else:
                b, se, ok = fit
                w(f"slope on leeway score = {b[0]:.3f} (cluster-robust SE {se[0]:.3f}); converged = {ok}. Separation can make this unstable; read with the exact results.\n")
    else:
        w("agents.csv not given.\n")
    w("## (d) Exact Cochran-Armitage trend test (pooled over markets, same scores)\n")
    res = exact_cochran_armitage_pooled(rows)
    w((f"exact two-sided p = {res['p']:.4f}" if res.get("p") is not None else res.get("note", "")) + "\n")
    if A:
        e_rows = []
        for r in rows:
            ag = [a for a in A if a["session_id"] == r["session_id"] and float(a["ref_budget"]) > 0]
            e_rows.append(dict(r, y_e=int(any(int(a["level"]) >= 2 for a in ag))))
        section("(e) L2+ among agents with reference budget > 0", "y_e", e_rows)
    section("(f) L2 coded from packets only", "y_l2_packets_only", rows)

    w("## Manipulation check (PAP 6.1; descriptive)\n")
    w("| leeway | mean offers per session | mean final credits (team) | N |\n|---|---|---|---|")
    for L in LEVELS:
        rr = [r for r in rows if r["leeway"] == L]
        mo = np.mean([float(r["offers"]) for r in rr]); mc = np.mean([float(r["final_credits_total"]) for r in rr])
        w(f"| {L} | {mo:.1f} | {mc:.1f} | {len(rr)} |")
    print("\n".join(out))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
