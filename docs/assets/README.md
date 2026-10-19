# Documentation assets

Active README artwork follows the bbackup forest-green identity and keeps all product claims in readable documentation. Generated artwork is illustrative; the dashboard preview is a real, sanitized headless capture.

## Active assets

- `bbackup-banner-v2.png`: wide README banner, generated with OpenAI's built-in image generation tool on 2026-10-17.
- `bbackup-social-v2.png`: matching 2:1 master image, generated from the banner reference.
- `bbackup-social-v2.jpg`: 1774×887 delivery encoding for GitHub social upload, under 1 MB. EXIF and XMP retain ownership and usage terms.
- `bbackup-dashboard-preview.svg`: Textual headless dashboard capture, using fixture data only and illustrative capacity. Regenerate with
  `uv run python scripts/generate_dashboard_preview.py` (requires restic).

[provenance.json](provenance.json) records generation briefs, references, dimensions, original hashes, final hashes, and ownership for the generated images and delivery encoding. Copyright, company URL, creator, usage terms, and generator are embedded in PNG metadata. Metadata insertion preserves the generated pixel data.

Validate active images with:

```bash
uv run python scripts/check_assets.py
```

GitHub requires a PNG, JPG, or GIF under 1 MB; the JPEG is the prepared upload asset.
See [GitHub’s social preview guide](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/customizing-your-repositorys-social-media-preview).

## Historical assets

`bbackup-banner.svg` is the original editable vector banner. It is retained for provenance and is no longer the README hero. `bbackup-hero.png` is an older unused illustration whose source prompt and generation record are unavailable; do not reuse it as a qualified product diagram or infer a license from the code license.

## Artwork terms

Copyright © 2026 Crux Experts LLC. [https://www.cruxexperts.com/](https://www.cruxexperts.com/)

Private asset. Property of Crux Experts LLC. Use, reproduction, modification, distribution, or publication without prior written permission is prohibited.

These terms apply to original artwork. The repository's code retains its MIT license; third-party notices remain their owners' property.
