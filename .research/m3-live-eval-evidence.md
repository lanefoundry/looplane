# M3 live Ollama eval evidence

Date: 2026-08-21 (Asia/Taipei)

This compact record preserves the final release-eval result after all M3 production and review
fixes. Raw bundles were retained at the paths below at verification time; their hashes allow an
available bundle to be checked without committing generated workspaces or model transcripts.

## Configuration and result

- Manifest: `evals/live/tiny-python-bug.json`
- Public entrypoint: `python -m coding_agent run`
- Provider/model: local Ollama / `qwen3:4b`
- Prompt: `m3-exact-edit-v1`
- Ollama turn bound: 4096 output tokens; task bound: 8 steps / 300 seconds
- Threshold: 4 successes in 5 independent attempts
- Result: 5/5, `daily_ready=true`
- Raw root: `/private/tmp/pca-m3-release-eval.46EMiT/ollama-qwen3-4b`
- `summary.json` SHA-256:
  `006af53f6e27cf12d9e9e187eb131297a9f21fff41e8624f0d3f8d9036c8732a`

Every attempt passed process exit, structured result, verified completion, exact changed-file and
patch assertions, successful completed `replace_text`, and unchanged source HEAD/status/bytes.

## Attempts

| Attempt | Run ID | Seconds | Provider tokens | events SHA-256 | result SHA-256 |
|---:|---|---:|---:|---|---|
| 1 | `8dbbfb6e1f72450eb1704518a6081aa9` | 152.59 | 9117 | `fc0332115a320ba4f9a775937dcc9b2bee16870204b9ee352165c2e528cc4679` | `a543eb4cc0ff620bbcce65a6b07743083c554447ad0778256e0e98620d8e116d` |
| 2 | `c7aaad296f794180bf16e4277cfecf26` | 167.46 | 9505 | `bcda31f9183f5ac905c382280412e52e3420ac7b99f1a19e50c46c68a9aac92b` | `7ea72c2a7997f19d489e1add7282f78d10bbe9e8f690e6d229f418cf11c621d9` |
| 3 | `b5beaea5fc5f43f396c6b0e2660c7ee6` | 174.32 | 9711 | `7c33eb52a5579265f2317ff871661ba4ed614230eb7992cc0b28b51d83c55b21` | `9441f068bd60bb5ef4ac830ab7b9e476d73170085fa3863c889815baf83c6a86` |
| 4 | `e7ccae0a777142aabac4cbeb8df9bd09` | 227.52 | 11292 | `c8e842e626f6ef910a76308238d4671ce435b3cca3987b6a1bd6647ce28e3143` | `a1a108cbc03eb7ee13f0f8ee96372c5096efe75f78fefa7245c53d5027db294d` |
| 5 | `6c3f42dc481c48e5972e114292dab51c` | 158.54 | 9225 | `8bb36551c2cfc2ae2fb19c0ad161b9f5eb1a6a88e8fe807f7acfadb74ba75436` | `321b2885153f711b4e7b810363839ad7496a265141bf5c04678b689ab1ae4d14` |

All five `changes.patch` files were identical, with SHA-256
`aabb0491ca737152246996e0d9c1139acfc8fde2083b1c5aa3583f460124e35c`.
The successful completed tool sequence in every attempt was
`list_files, read_file, replace_text, run_check`.

## Evidence boundary

This is a repeatability result for one small fixture, one local model/configuration, and one
machine. It does not establish broad repository-level coding quality or hostile-code isolation.
The compact evidence intentionally omits raw model transcripts from Git.
