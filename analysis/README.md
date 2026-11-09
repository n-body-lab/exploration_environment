# analysis/: HIVEMARKET main study (credit leeway)

Scripts for the pre-registered case study (registration: https://osf.io/gn6vr). Main runs used this repository at commit `cfa0d026b17e06ac67585138a3e59f6f06c1bbce`.

| File | What it does |
|---|---|
| `LydiaAshton_HIVEMARKET_Code_L2.py` | Codes the primary outcome by the registered rule (PAP 4.2): reads `runs/main-s*/` (manifest, monitor logs, agent traces) and writes `sessions.csv`, `agents.csv` and `evidence.csv` (every command or packet behind an L1–L4 code). |
| `LydiaAshton_HIVEMARKET_Analysis.py` | The registered analysis (PAP 5): complete blocks only; L2+/L3+/L4 by leeway with exact Clopper–Pearson CIs; exact conditional logistic regression with market fixed effects (ordered leeway score 0–3; two-sided exact conditional probability test); robustness (a)–(f); manipulation check. Writes Markdown tables. |
| `LydiaAshton_HIVEMARKET_Judge_Panel.py` | Validation by an LLM judge panel (claude-sonnet-5-5, gpt-6.1-terra; gemini-3.6-flash breaks ties) of the rule's code for every agent. Keys come from environment variables; resumable. |
| `LydiaAshton_HIVEMARKET_Judge_TestCalls.py` | One test call per judge model (run before the main runs). |
| `run_final.sh` | Runs all of the above on the collected run folders. |
| `simulate_test_data.py` | Simulated input used to test the analysis script (not study data). |

## Run

```bash
python3 LydiaAshton_HIVEMARKET_Code_L2.py --runs ../runs --order ../runs/block_order.csv --out results
python3 LydiaAshton_HIVEMARKET_Analysis.py results/sessions.csv results/agents.csv > results/results.md
# optional, needs ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY (enter with read -s; never commit keys)
python3 LydiaAshton_HIVEMARKET_Judge_Panel.py --runs ../runs --codes results/agents.csv --out results
```

Requirements: Python 3.10+, numpy and scipy (analysis only); the coding and judge scripts use the standard library.

## Notes

- Implementation choices not spelled out in the registration: hostname matching is literal (commands that only name a peer are flagged `names_only_no_network_tool=1` in `evidence.csv`); L3 is credited to the agent that opened the connection. See the report, Appendix G.
- The scripts were written after registration and tested on synthetic events, simulated data and one exploratory run before the main sessions were coded.
- Authors: written by Lydia Ashton with Claude (Anthropic); see the report's LLM usage statement.
