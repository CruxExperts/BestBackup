# Documentation assets

Active README artwork follows the bbackup forest-green identity and keeps all product claims in readable documentation. Generated artwork is illustrative; the dashboard preview is a real, sanitized headless capture.

## Active assets

- `bbackup-banner-v2.png`: wide README banner, generated with OpenAI's built-in image generation tool on 2026-10-17.
- `bbackup-social-v3.jpg`: current GitHub social upload, generated with OpenAI's built-in image generation tool on 2026-10-19 using the README banner as a brand reference. Center-cropped and resized to GitHub's recommended 1280×640 dimensions; 154,877 bytes, under 1 MB. EXIF and XMP retain ownership and usage terms.
- `bbackup-dashboard-preview.svg`: Textual headless dashboard capture, using fixture data only and illustrative capacity. Regenerate with
  `uv run python scripts/generate_dashboard_preview.py` (requires restic).
  The current capture uses a 112-column by 36-row terminal. Compact-layout
  checks use separate disposable fixtures; this overview image is a readable
  example rather than proof of every screen or a live host's recovery status.

[provenance.json](provenance.json) records generation briefs, references, dimensions, original hashes, final hashes, and ownership for the generated images and delivery encoding. Copyright, company URL, creator, usage terms, and generator are embedded in PNG metadata. Metadata insertion preserves the generated pixel data.

Validate active images with:

```bash
uv run python scripts/check_assets.py
```

## Set the GitHub social preview

GitHub does not automatically use a README hero or an image committed to the repository.
Without a separately uploaded social preview, shared repository links use GitHub's
generated card with the owner's avatar.

1. Open the repository's **Settings → General → Social preview**.
2. Choose **Edit → Upload an image…** and select [`bbackup-social-v3.jpg`](bbackup-social-v3.jpg).
3. Confirm the preview shows the bbackup wordmark and both headline lines without clipping.

Uploading is a separate repository-settings change; committing this file does not
perform it. Existing shared links may retain a platform's cached preview after upload.
The README continues to use its wider banner.

GitHub accepts PNG, JPG, or GIF under 1 MB and recommends 1280×640 for best display.
See [GitHub’s social preview guide](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/customizing-your-repositorys-social-media-preview).

## Historical assets

`bbackup-social-v2.png` and `bbackup-social-v2.jpg` are the previous social master and upload encoding, retained with their provenance. Use `bbackup-social-v3.jpg` for new uploads.

`bbackup-banner.svg` is the original editable vector banner. It is retained for provenance and is no longer the README hero. `bbackup-hero.png` is an older unused illustration whose source prompt and generation record are unavailable; do not reuse it as a qualified product diagram or infer a license from the code license.

## Artwork terms

Copyright © 2026 Crux Experts LLC. [https://www.cruxexperts.com/](https://www.cruxexperts.com/)

Private asset. Property of Crux Experts LLC. Use, reproduction, modification, distribution, or publication without prior written permission is prohibited.

These terms apply to original artwork. The repository's code retains its MIT license; third-party notices remain their owners' property.
