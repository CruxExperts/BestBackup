# Documentation assets

This directory stores GitHub-facing visual assets referenced by the project documentation.

| File | Purpose | Provenance and license |
|:--|:--|:--|
| `bbackup-banner.svg` | Editable README artwork: local encrypted snapshots, B2/S3 replicas, and recovery | Original vector artwork, 2026 Crux Experts LLC. Embedded copyright, company URL, and private-asset notice. Rendered and visually checked headlessly. |
| `bbackup-dashboard-preview.svg` | Real Textual headless screenshot of the preview empty state at 112 × 32 terminal cells | Generated from `bbackup/dashboard.py` with Textual `App.save_screenshot`; copyright and private-asset notice embedded. Empty fixture configuration, no host data. |
| `bbackup-hero.png` | README hero image showing the Docker, filesystem, database, encryption, verification, and remote-storage backup pipeline | Generated project asset; source prompt, generator, and license record are not present in the repository. Treat it as project-local until a complete asset record is added. |

Prefer Mermaid diagrams in Markdown for charts and process flows. Use bitmap assets when a visual overview improves first-read comprehension or when a diagram benefits from richer UI-style composition.
