# BUSY Bar Apps Catalog

This is the public repository for community-developed apps for BUSY Bar. The Apps Catalog is available at [apps.busy.app](http://apps.busy.app/).

The repository contains app manifests with links to source code in the authors' GitHub repositories and the app details displayed in the Catalog. App packages are built from the authors' source code.

## How to submit an app

Submit app manifests in `applications/<app_id>/manifest.yml`.

Create one pull request per app. In the PR template, confirm that you're the app's author or have write access to its repository.

See the [Contribution Guide](CONTRIBUTING.md) for details.

## Check your manifest

Before opening a pull request you can validate the manifest locally. Use Python 3.13 or newer from the repository root:

```sh
python -m pip install -r tools/requirements.txt
python -m tools.validate_manifest applications/<app_id>/manifest.yml
```

Findings print as `::error` / `::warning` lines; exit code 0 means the manifest passes. The category check reads the public category list, so it needs network access.

## Authors

Read the [Contribution Guide](CONTRIBUTING.md) and [Terms for Authors](TERMS.md).

The [example manifest](example/com.example.weather/manifest.yml) is a complete example of a manifest.

Questions: [BUSY Support](https://support.busy.app/hc/en-us).

Maintaining the catalog: [documentation/Maintainers.md](documentation/Maintainers.md).
