# RedFetch

RedFetch is a tool for downloading software and scripts from RedGuides. You can use it from the command line, a terminal UI, or a web UI.

## Installation

Install the dependencies and run redfetch.py

### Command-Line Options

- `--logout`: Log out and clear cached tokens.
- `--download-resource <RESOURCE_ID>`: Download a specific resource by its ID.
- `--download-watched`: Download all watched and special resources.
- `--force-download`: Force download of all watched resources.

- `--list-resources`: List all resources in the cache.
- `--serve`: Run as a server to handle download requests.
- `--update-setting <SETTING_PATH> <VALUE>`: Update a configuration setting. The setting path should be dot-separated.
- `--switch-env <ENVIRONMENT>`: Change the server type (`LIVE`, `TEST`, `EMU`).


### Examples

- **Download all watched resources**

  ```bash
  python redfetch.py --download-watched
  ```

- **Force re-download all watched resources**

  ```bash
  python redfetch.py --force-download --download-watched
  ```