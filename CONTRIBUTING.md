# BUSY Bar Apps Catalog: Contribution Guide

Thanks for building for BUSY Bar. We're glad to have you contribute to the Apps Catalog. 

This guide covers how to prepare your app, submit it for review, and publish it in the Catalog.

## Terms

By submitting an app, you agree to the [Terms for Authors](TERMS.md). Read them before opening a pull request.

## Publishing your app

### Step 1. Prepare your app

1. Host the app's source code in a public GitHub repository.
2. Make sure it builds with `busy-cli build` ([@busy-app/cli](https://www.npmjs.com/package/@busy-app/cli)).
3. Keep `appmeta/` in `src/`, as in the template, or directly in the app folder next to `main.js`. If both locations contain `appmeta/`, the one in `src/` takes precedence.
4. Include a LICENSE file with an open-source license (MIT, Apache-2.0, GPL, and similar) in the repository root or app folder. Make sure it's included in the commit you submit. See the [Terms for Authors](TERMS.md).
5. Commit a lock file in the app folder: `package-lock.json` (npm) or `pnpm-lock.yaml` (pnpm). We use it to install the dependencies and determine the busy-cli version for the build.
6. Test your app on a BUSY Bar running the latest release firmware: run `busy-cli build`, then `busy-cli app install`. Alternatively, test it using the official [BUSY Bar Emulator](https://github.com/busy-app/busybar-emulator).
7. Add screenshots to your repository: PNG, 72×16, captured from BUSY Bar or [BUSY Bar Emulator](https://github.com/busy-app/busybar-emulator). Back screen screenshots are optional: PNG, 160×80.
8. Add icons (optional): `appmeta/icon_front_8x8.png` (8×8) and `appmeta/icon_back_11x11.png` (11×11), both 8-bit PNG. Without a front icon, the Apps Catalog shows a default icon and the checks show a warning.

### Step 2. Write `manifest.yml`

Follow the [manifest format](#manifest-format). Set `commit_sha` to the commit you want to publish.

### Step 3. Open a pull request

1. Fork this repository.
2. Add your manifest as `applications/<id>/manifest.yml`, where `<id>` is the `id` from your app's `appmeta/manifest.json`. Don't change any other files.
3. Open a pull request and fill in the template. Before opening it, tick "I'm the author of the app or have write access to its repository". Editing the description doesn't rerun the checks. If you forget to tick the box, push a new commit or close and reopen the pull request.

### Step 4. Automatic checks

Checks run on every commit in your pull request. If a check fails, you'll see the reason in the pull request before review.

### Step 5. Review

A moderator reviews your app, usually within 1–2 business days. They can approve it, request changes, or reject it.

### Step 6. Publication

Once your pull request is merged, your app is published in the Apps Catalog. We publish the build from the exact commit approved by the moderator. If publication fails, the bot explains why in the pull request.

## Updating your app

To publish an update, open a new pull request with changes to your `manifest.yml`:

- Set `commit_sha` to the new source commit
- Increase `version` in `appmeta/manifest.json`
- Add a `changelog` describing the new version

The description and screenshots are updated only when a new version is published.

Keep your app in the same repository and under the same GitHub account. Updates are accepted only from the repository the app was originally published from. If you need to move it, open an issue and the maintainers will help.

If a new firmware release isn't compatible with your app, the Apps Catalog marks it as Unsupported until you publish a compatible update.

## Removing your app

To remove your app, open a pull request that deletes its entire `applications/<id>/` folder. Don't include any other changes. Deleting only part of the folder or combining the removal with other changes fails the scope check.

A few things to know about the process:

- **Who can remove an app:** The app's author (the GitHub account that owns its repository) or Catalog maintainers. For anyone else, the `Who removes the app` check fails. If you've lost access to your app, open an issue in this repository, and the maintainers will help.
- **The checks are short:** No build is run, and your source code isn't downloaded. If the app isn't in the Catalog (it was never published or has already been removed), the check `App in the catalog` only shows a warning, and merging just cleans this repository.
- **Review and publication:** A moderator reviews and approves the pull request, just as with a new app submission. After the merge, the app disappears from the Catalog and search, and its versions are no longer available for download. Copies installed on BUSY Bars remain unchanged. The pull request receives a `Removed from the catalog` comment.
- **Restoring an app:** Removed apps can be restored as their IDs remain reserved. Publishing a new version with that ID fails with `application_removed`. To restore the app, open an issue asking the maintainers to restore it.

## Manifest format

```yaml
sourcecode:
  type: git
  location:
    # Public GitHub repository of your app
    origin: https://github.com/<owner>/<repo>.git
    # Full commit SHA. Tags and branches aren't accepted, you can keep the tag in a comment
    commit_sha: <40 characters>  # v1.0.0
    # App folder in the repository, with its own lock file. Omit it if the app is in the root
    subdir: <folder>

# Category ID from the list in Categories below
category: <category-id>

# Short description for the app card: plain text, 10 to 120 characters, one line
short_description: <text>

# Full description for the app page, 50 to 4000 characters
description: |
  <text>

# What's new in this version: inline text, 10 to 4000 characters (no file references)
changelog: |
  <text>

# Screenshots from your app's repository: PNG 72×16 up to 2 MB, 1 to 10. The first one is the card cover
screenshots:
  - screenshots/1.png

# Optional: screenshots of the back screen, PNG 160×80 up to 2 MB, 0 to 10
back_screenshots:
  - screenshots/back-1.png
```

- All fields are required except `subdir` and `back_screenshots`.
- Don't add `name`, `id`, `version`, `author` or `icon`: we take them from your app package. The `description` field in `appmeta/manifest.json` isn't used by the Catalog.
- The app author is the owner of the GitHub repository.
- Write all texts in the manifest. File references aren't supported.
- `description` and `changelog` support basic Markdown: level 1–2 headings, bold, italic, lists, and links. `short_description` is plain text.
- Texts over the limit fail the check. We don't cut them.
- Screenshot and back screenshot paths are relative to the app folder at `commit_sha` and can't point outside it.
- Each app can have only one category. The first publication sets it, changes to `category` in later versions are ignored. We may change your app's category.

### Content limits

| Field | Limit |
|---|---|
| `name` (in `appmeta/manifest.json`) | 1–32 characters: Latin letters, digits, single spaces between words and `! ? & ' ( ) + , - . / : # $ % @ _` |
| `short_description` | 10–120 characters, one line |
| `description` | 50–4000 characters (counted after `@file` is read) |
| `changelog` | 10–4000 characters, inline text |
| `screenshots` | 1–10 PNG, 72×16, up to 2 MB |
| front icon | PNG 8×8, optional |
| `category` | exactly one, required |

Every limit is checked on a new pull request. Apps that are already published are not rechecked until their next version.

### Categories

| ID | Name | What goes there |
|---|---|---|
| `productivity` | Productivity | Focus, Pomodoro, timers, Do Not Disturb, tasks, habits, deadlines, meeting and call status |
| `clocks` | Clock & Time | Clocks, world time, countdowns, dates |
| `info-feeds` | Info & Feeds | Weather, crypto, stocks, exchange rates, news, transport, sports, quotes |
| `social` | Social | Followers, unread messages in messaging apps, streams, "come over" status, birthdays |
| `dev-monitoring` | Dev & Monitoring | Builds, pull requests, uptime, load, KPIs, sales |
| `music` | Music | Now Playing, music apps, equalizers, radio, volume, players |
| `art` | Art | Animations, pixel art, generative art, quotes, decorative displays |
| `games` | Games | Games on the buttons and the wheel, pixel pets, mini-games |
| `smart-home` | Smart Home | Home Assistant, sensors, lights, climate, energy |
| `utilities` | Utilities | Apps that don't fit any other category |

## Application ID

The `id` in `appmeta/manifest.json` is also the name of your directory, `applications/<id>/`.

**Rule.** Up to 63 characters: lowercase Latin letters, digits, `_`, `-`, `.`. At least two dots, not at the start or end, not in a row. For example, `com.example.weather`, `io.github.alice.clock_2`, `ru.malk-it.chess_clock`. These don't pass: `app.busy` (one dot), `App.Busy.X` (upper case), `.app.busy.x`.

**How to choose an ID**
- If you have a website: use the reverse domain, lower case, e.g. `com.example.weather`.
- If you don't have a website: use your GitHub account, `io.github.<github-user>.<app>`, e.g. `io.github.imalkav.chess_clock`.

`app.busy.*` is only for official BUSY apps.

An ID that doesn't start with a domain-like part (`com`, `org`, `io`, `net`, `dev`, `app` or a two-letter country code) still passes, but the checks show a recommendation.

## Requirements

Any failed check fails the pull request checks.

**Pull request**
- Changes only `applications/<id>/manifest.yml`.
- The authorship box in the template is ticked.
- An app `id` belongs to the repository it was first published from. Updates of that `id` come from the same repository.

**`manifest.yml`**
- Follows the [manifest format](#manifest-format).
- The commit exists in the repository. `subdir` and the screenshots exist at that commit.

**`appmeta/manifest.json`**
- Up to 512 bytes.
- Has `format_version`, `id`, `name`, `version` and `api_version`.
- `id`: follows the [application ID rule](#application-id).
- `name`: up to 32 ASCII characters: Latin letters, digits, spaces and punctuation.
- `version` and `api_version`: three numbers `MAJOR.MINOR.PATCH` without leading zeros, e.g. `1.2.0`. Suffixes such as `1.0.0-beta` or `1.0.0+1` don't pass. `version` is higher than the published one.
- `api_version` is the minimum [HTTP API](https://docs.busy.app/bar/dev/http-api) version your app needs. It isn't newer than the API of the latest release firmware: with that firmware, your bar shows it as `api_semver` at `http://10.0.4.20/api/version` over USB.
- `heap_size_kib`, if set: 1 to 512.
- `debug`: `false` or not set.

**Build**
- The repository has a `LICENSE`, `LICENCE` or `COPYING` file at the submitted commit. Without it the check fails. If the text isn't recognised (a modified or unusual license), the checks show a warning and the moderator reads it: use the unmodified text of a standard license.
- The app folder has exactly one lock file: `package-lock.json` (npm) or `pnpm-lock.yaml` (pnpm). Yarn and Bun lock files aren't supported. A workspace lock file outside the app folder isn't used.
- The lock file pins `@busy-app/cli`.
- With pnpm, `package.json` pins the pnpm version: `"packageManager": "pnpm@<version>"`.
- `busy-cli build` succeeds. We build without network access and secrets, your own scripts don't run.
- The entry point has no exports. The build output has `scripts/main.js`.
- The app folder at `commit_sha` is up to 100 MB.

**Package**
- Archive up to 100 MB (104,857,600 bytes). Unpacked, also up to 100 MB and 10,000 files and folders.
- Each `.js` file up to 250 KiB (256,000 bytes): the firmware doesn't load larger ones.
- `appmeta/icon_front_8x8.png`, if present: PNG, 8×8. `appmeta/icon_back_11x11.png`, if present: PNG, 11×11. Up to 64 KiB each, 8 bits per channel (16-bit and other formats that can't be re-saved without loss are rejected). A missing front icon is only a warning.
