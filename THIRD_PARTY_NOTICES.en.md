# Third-Party Notices — English Translation

> **Authoritative text:** The Turkish [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES)
> is the canonical and authoritative notice. This English translation is
> provided for convenience only. In the event of any discrepancy, the Turkish
> canonical text prevails.

This file separates notices for source-derived code from notices for packages
resolved at runtime. It does not change the license for MutalaaMCP source code;
see `LICENSE`.

## Bundled font software

The website's `docs/assets/fonts/` directory includes WOFF2 files for the
Newsreader font.

Copyright 2020 The Newsreader Project Authors
(<http://github.com/productiontype/Newsreader>)

The font software is licensed under the SIL Open Font License 1.1. The complete
copyright notice and license text are in `docs/assets/fonts/OFL.txt`. This font
license does not change the license for MutalaaMCP source code.

## Source-derived code

At the time this notice was prepared, no source-code file in this repository is
identified as copied from or substantially derived from a third-party source
repository. If a future change copies or substantially derives a file from a
third-party project, that source's required copyright and license notices must
be preserved.

## Direct runtime dependencies

`pyproject.toml` declares the following direct runtime dependencies. They are
installed as separate distributions, and their source is not vendored into this
repository. The license identifiers below describe recorded package metadata;
they are not a claim about every version permitted by the dependency ranges.

| Distribution | License identifier |
| --- | --- |
| FastMCP | Apache-2.0 |
| filelock | MIT |
| httpx | BSD-3-Clause |
| keyring | MIT |
| platformdirs | MIT |
| PaddleOCR (optional OCR extra) | Apache-2.0 |
| PaddlePaddle (optional OCR extra) | Apache-2.0 |
| pypdf | BSD-3-Clause |
| pydantic | MIT |
| pydantic-settings | MIT |
| typer | MIT |
