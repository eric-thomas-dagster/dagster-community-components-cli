# Microsoft documentation used as test ground truth

These files are copied unmodified from Microsoft's Azure documentation source, <https://github.com/MicrosoftDocs/azure-docs> (`articles/data-factory/`), which is
licensed CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/). © Microsoft Corporation. They are test data, not part of the converter.

They are the only ground truth available without an Azure subscription: `tests/test_doc_conformance.py` runs every documented expression example through the
translator, `tests/test_trigger_conformance.py` checks the documented schedule recurrences, and several tests in `tests/test_convert.py` encode rules stated in
the activity pages (ExecutePipeline waits by default, Delete is not recursive by default, Until continues after an inner failure, the pipeline success rule, ...).
The documentation is not always right; where an example contradicts the page's own rules it is listed in `ERRATA` with the reason.

`tests/test_dataflow_expressions.py` and `tests/test_dataflow_script.py` run the data flow function reference and the transformation pages (aggregate, derived column, filter, join, select, sort, union, source, sink, exists, lookup, conditional split, surrogate key, rank, alter row, window, pivot, flatten; the unpivot page has no script section, so its format comes from the public corpus).

Fetched on 2026-11-20 from the `main` branch. Re-fetch and rerun the tests to see what changed.
