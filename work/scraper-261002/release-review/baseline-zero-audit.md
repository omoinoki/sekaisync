# Released Baseline Zero Audit

## Conclusion

No harness defect causing the released `common_scalar` result of `0/200` is proven by the retained evidence. This run genuinely has no released scalar consumer outputs to match: all 50 `term_penetrate` responses are null, all 50 `term_lookup` results are empty, and all 50 generic-query term lists are empty. The score report records `consumer_absent` for every one of the 200 baseline common obligations. This establishes absence in this run, not a universal claim about the released product.

## Proven Evidence

The retained recovery `process.json` has return code 0 and empty stdout/stderr. The scalar results and consumer outputs in `report.json` are identical to those in `callable-result.json`; recovery adds provenance/binding fields, not output transformations.

| Source language | Actual candidates | Alignable | Layer-0 rejected | Channels rejected | Accepted terms |
| --- | ---: | ---: | ---: | ---: | ---: |
| en | 0 | 0 | 0 | 0 | 0 |
| ja | 17 | 8 | 9 | 8 | 0 |
| ko | 0 | 0 | 0 | 0 | 0 |
| zh_hans | 5 | 0 | 5 | 0 | 0 |
| zh_tw | 5 | 0 | 5 | 0 | 0 |

For all five languages, raw trunk/hub/translit/official channel candidate counts are zero, pending/conflicts are zero, and slot decisions are empty. Normal apply receipts have revision 1 but zero inserted/updated terms, applied slots, evidence submitted/written, and review items. Thus this is not a demonstrated failure to persist an accepted scrub result: no accepted result existed to persist. Revision 1 alone does not imply extraction success.

The inputs contain ten trust-B event-story pages: two story families in each of five languages, with nonempty text. Stores are newly initialized and populated with those pages only; the loaded glossary is empty. Scrub records its empty-glossary warning. This removes official-backed alignment from this workload; it is a configuration boundary, not evidence of a swallowed exception.

## Harness And Release Checks

- `baseline_callable.py:33-43` passes the actual source language to candidate discovery, pair indexing, scrub, and apply. Targets are the other four languages; vocab/IDF resources and the discovered set are supplied. Selected queries are read only after every scrub/apply loop, with an empty discovery seed.
- Released `trinity.py:467-508` describes and implements a JA-oriented pool: Katakana runs, Japanese quotation forms, and discovered words containing CJK ideographs or Katakana. Passing `en`/`ko` does not replace those extraction rules. Zero candidates in those languages is consistent with this released capability boundary, not proof that the harness ignored its language argument. Traditional Chinese aliases are normalized by released `termindex.py:189`, `990-1017`, and `1955-1969`.
- The consumer calls at `baseline_callable.py:50-53` match the released public core signatures (`core.py:926`, `953`, `1264`). No typed-output API is required. The common scorer (`runner.py:631-634`, `727-737`) uses the same scalar matcher for both implementations and immediately rejects absent responses before reference geometry or lexical alternatives are evaluated. Missing typed receipts are not the cause of these 200 baseline failures.
- Recovery invokes the released code directory as cwd with `PYTHONPATH` pointing to it (`baseline_recovery.py:47-50`). The callable prepends that directory, removes competing work-tree paths, and checks the imported package origin (`baseline_callable.py:17-23`). Released `sekaisync/__init__.py` is an ordinary package with no path extension. No import-contamination defect is demonstrated.
- Recovery's scoring adapter changes only the baseline report input route. `baseline-recovery-binding.json` explicitly retains `original_protocol_pass: false` and `metric_rules_or_denominators_modified: false`. The original and contract reports both retain current common `117/200`, released common `0/200`, and `58.5` percentage points. Both overall statuses are FAIL; the improvement gate passing does not establish overall success.

## Limitations And Interpretation

This is a product-level comparison, not an equal-compute experiment: the released deterministic scalar discovery/alignment path is compared with the current host-assisted source-discovery and occurrence-review workflow. It does not isolate the effect of code changes from assistance or workload changes. The empty-glossary, two-story sample also does not characterize a fully populated released store.

The audit did not run Git, scraper, semantic scoring, or product algorithms; did not read target reference labels, original host answers, or historical answer data; and did not mutate product, tools, references, or answers. Only this audit document was created. Existing hash bindings were inspected but not independently recomputed. Import isolation was checked structurally and via the successful retained guard, not by recording every submodule origin at runtime. These limits prevent a claim that every possible harness/environment defect has been disproved, but none is needed to explain the retained zero.
