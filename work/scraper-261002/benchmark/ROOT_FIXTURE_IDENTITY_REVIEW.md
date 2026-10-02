# Metadata-Only Fixture Identity Review

Root decision: 2026-10-02, before formal registration or any fresh dialogue read.
Keep the original fixed-ranked choices event:2:4 and event:161:5. Do not rerank.
This does not waive any real prior acquisition/use of a content family.

The existing usage scanner matches event:2 identifiers in synthetic unit tests.
Root inspected the matched current test definitions and their surrounding fixture
construction. These are not actual event:2 dialogue or semantic reference data:

- tests/test_agent_subject_fallback.py:166 uses synthetic one-turn generic text.
- tests/test_census_scraper_area_talk.py:93 constructs episode/release metadata.
- tests/test_entity_region_db.py:47,77 constructs generic event database records.
- tests/test_integrity.py:29,68 uses generic placeholder body labels.
- tests/test_occurrence_public_query.py:78 uses "A: Unrelated corpus body.".
- tests/test_progress.py:124 uses a generic future-content placeholder.
- tests/test_scraper_corpus_census.py uses synthetic release metadata and generic
  "different localized body", "changed page" fixture values.
- tests/test_source_migrate.py:59,122 constructs a "hello world" migration page.
- tests/test_web_browse_sql.py:93 constructs a generic custom-instance body.

The scanner also reports archived copies of five of these fixture modules under
work/scraper-repair-20260929/baseline/tests: test_entity_region_db.py,
test_integrity.py, test_progress.py, test_source_migrate.py, test_web_browse_sql.py.
Their reported identifier uses are the same synthetic fixture roles, not a trial
or captured real body. The benchmark agent reports no actual trial/work semantic
use path for event:2, and event:161 has no matching prior-use path.

Registration may distinguish ONLY this reviewed synthetic-identity collision
from actual corpus use. Retain all original scanner matches, file hashes and this
decision in the registration audit. Any new or unexplained matching file remains
a blocker. The original prior-family exclusion list, seed, ranking, pages and
all release rechecks remain unchanged. No actual selected source or target body
was read in this review, and no source reference or host answer exists yet.
