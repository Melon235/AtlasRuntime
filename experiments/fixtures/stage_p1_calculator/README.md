# Calculator project

A small Python package with three modules:

| Module | Provides | Notes |
|---|---|---|
| `app/calc.py` | `add(a, b)` | the public contract also requires `subtract(a, b)`, which is **missing** |
| `app/text.py` | `normalize(value)` | strips surrounding whitespace |
| `app/units.py` | `to_grams`, `total_grams`, `GRAMS_PER_KILOGRAM` | builds on `app.calc.add`; its interface must not change |

`tests/test_public.py` is the public contract. It currently fails because `subtract` is missing.

Run it with:

```sh
python3 -m pytest -q
```

Do not change the existing public behaviour of `add`, `normalize`, `to_grams` or `total_grams`.
