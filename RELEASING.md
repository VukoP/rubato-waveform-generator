# Releasing

Notes for the maintainer. Run the commands from the repository root.

## Repository metadata

GitHub description (under 350 characters):

```text
Free desktop app that turns audio into an animated waveform video with a transparent background (ProRes 4444 alpha). 10 audio-reactive layouts for podcasts and music, ready to overlay in DaVinci Resolve and other video editors.
```

Topics (20, the maximum):

```text
audio-waveform, waveform-generator, audio-visualizer, audio-visualization, audio-reactive, waveform, audiogram, podcast, podcasting, video-editing, davinci-resolve, premiere-pro, final-cut-pro, after-effects, prores, alpha-channel, transparent-video, ffmpeg, python, desktop-app
```

## First publish

Run these in order. Each needs the GitHub CLI (`gh`) signed in as VukoP.

```sh
gh repo create VukoP/rubato-waveform-generator --public --source . --push --description "Free desktop app that turns audio into an animated waveform video with a transparent background (ProRes 4444 alpha). 10 audio-reactive layouts for podcasts and music, ready to overlay in DaVinci Resolve and other video editors."

gh repo edit --add-topic audio-waveform,waveform-generator,audio-visualizer,audio-visualization,audio-reactive,waveform,audiogram,podcast,podcasting,video-editing,davinci-resolve,premiere-pro,final-cut-pro,after-effects,prores,alpha-channel,transparent-video,ffmpeg,python,desktop-app

gh repo edit --homepage https://vukop.github.io/rubato-waveform-generator/

gh api -X POST repos/VukoP/rubato-waveform-generator/pages -f 'source[branch]=main' -f 'source[path]=/docs'

gh api -X PUT repos/VukoP/rubato-waveform-generator/private-vulnerability-reporting
```

Then set the repository rules the workflows cannot enforce. Do this before the first tag.

```sh
# Fork PR workflows: require approval for all outside collaborators, and keep the fork token read-only.
gh api -X PUT repos/VukoP/rubato-waveform-generator/actions/permissions/fork-pr-contributor-approval -f approval_policy=all_external_contributors
gh api -X PUT repos/VukoP/rubato-waveform-generator/actions/permissions/workflow -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false

# Only GitHub-owned actions may run.
gh api -X PUT repos/VukoP/rubato-waveform-generator/actions/permissions -F enabled=true -f allowed_actions=selected
gh api -X PUT repos/VukoP/rubato-waveform-generator/actions/permissions/selected-actions -F github_owned_allowed=true -F verified_allowed=false

# Dependabot alerts and security updates. Secret scanning and push protection are already on for a public repository.
gh api -X PUT repos/VukoP/rubato-waveform-generator/vulnerability-alerts
gh api -X PUT repos/VukoP/rubato-waveform-generator/automated-security-fixes

# No unused surfaces.
gh api -X PATCH repos/VukoP/rubato-waveform-generator -F has_wiki=false -F has_projects=false

# Protect main: pull requests with green CI, no force pushes.
gh api -X PUT repos/VukoP/rubato-waveform-generator/branches/main/protection --input - <<'JSON'
{"required_status_checks": {"strict": true, "contexts": ["lint", "test (ubuntu-latest, py3.12)", "test (ubuntu-latest, py3.14)", "test (macos-latest, py3.14)", "test (windows-latest, py3.13)", "ui"]},
 "enforce_admins": false, "required_pull_request_reviews": null, "restrictions": null,
 "allow_force_pushes": false, "allow_deletions": false}
JSON

# Protect v* tags so a release tag cannot be moved or deleted.
gh api -X POST repos/VukoP/rubato-waveform-generator/rulesets --input - <<'JSON'
{"name": "release tags", "target": "tag", "enforcement": "active",
 "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
 "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}, {"type": "update"}]}
JSON
```

If a command is rejected, use the setting by hand:

- Settings > Actions > General > Fork pull request workflows from outside collaborators: Require approval for all outside collaborators.
- Settings > Actions > General > Workflow permissions: Read repository contents and packages permissions, with "Allow GitHub Actions to create and approve pull requests" off.
- Settings > Branches (or Rules > Rulesets) > main: require status checks to pass, block force pushes and deletion.
- Settings > Rules > Rulesets > New tag ruleset: target `v*`, restrict deletions and updates, block force pushes.

"Require green CI before tagging" is a habit plus a gate: tag only a commit whose `CI` run on `main` is green. The Release workflow also runs the whole `ci.yml` suite first and builds nothing if it fails.

Then, by hand in Settings > General:

- Upload `assets/brand/social-preview.png` under Social preview. GitHub has no API for it.
- Turn on Sponsorships under Features, so the Sponsor button shows the link from `.github/FUNDING.yml`.

## Cutting a release

1. Bump `__version__` in `rubato_waveform/__init__.py`.
2. Add a `## [X.Y.Z] - YYYY-MM-DD` section to `CHANGELOG.md`, dated today, under `### Added`, `### Changed` or `### Fixed`. The release workflow publishes this section as the release notes through `scripts/changelog_section.py`, and fails if it is missing. Check it with `python3 scripts/changelog_section.py vX.Y.Z`.
3. Update `version` and `date-released` in `CITATION.cff`.
4. Commit, then tag and push the tag:

   ```sh
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

   The workflow checks that the tag matches `__version__` and stops if it does not.
5. Wait for the Release workflow. It first runs the full CI suite, then builds the macOS and Windows zips, smoke-tests each frozen app, and creates a draft release. Nothing is published automatically.
6. Download both zips from the draft and test them. Open each app, load audio, export a clip, and import it into an editor.
7. Publish the draft.

A release must be published before the website's download links work, because they point to the latest published release.

## After the first CI run

Until CI has run on GitHub, these are unverified:

- The `ci.yml` jobs on the hosted runners (lint, tests on Linux, macOS and Windows, UI tests on Linux).
- The Windows zip itself: the author has not run the app on Windows.
- A real file drag on Windows. The smoke test cannot script one, and WebView2 refuses a scripted file, so the step that hands over the real path is stubbed there.
- The exact wording of the SmartScreen steps in the README.
- The README CI badge, which stays grey until `ci.yml` has run on `main`.
- Golden frame hashes. They are skipped in CI (the `CI` variable is set), because the hosted runner's libm or Python build may differ from the author's Mac. Run the full suite on the author's Mac before tagging, since CI only checks the constant records and behaviour.
- That the Pages site serves from `main:/docs`, and that the social preview was uploaded.

Never attach a self-hosted runner to this public repository. A pull request from a fork can run code on it. A repository test fails if a workflow mentions `self-hosted`, and CI and release builds must stay on GitHub-hosted runners.

## Search engines

Crawlers only read `robots.txt` at the root of a host, so `docs/robots.txt` does not announce the sitemap for a project site. Submit the sitemap by hand in Google Search Console and Bing Webmaster Tools, using a URL-prefix property for `https://vukop.github.io/rubato-waveform-generator/`.

To notify engines that use IndexNow after a site change, POST the site's URL to `https://api.indexnow.org/indexnow`, with the key from the `.txt` file in `docs/` named after that key and that file's URL as `keyLocation`.
