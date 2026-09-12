# Golden differential fixtures

Store small, deterministic fixtures here and execute the same resolved feature plan across supported backends.
Normalize results to canonical Arrow-like semantics before comparison.

Include edge cases relevant to the changed features:

- nulls and missing values;
- empty and non-ASCII UTF-8 strings;
- unknown categories;
- integer minima/maxima and unsigned boundaries;
- NaN/infinity and floating-point tolerance cases;
- duplicate/event-boundary timestamps;
- late-arriving records;
- empty/fixed-size vectors;
- deterministic hash cases.

Each semantic feature or backend adapter should declare exact or tolerance equivalence.
