# moveit-py

An unofficial Linux command-line client for MOVEit Transfer. Copy files to and from MOVEit folders, or send and receive packages by email.

## Installation

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/augiraud/moveit-py.git
cd moveit-py
cp moveit.env.example moveit.env
${EDITOR:-vi} moveit.env  # Set MOVEIT_URL
```

Restart your terminal if `uv` is not found. The first command you run will install Python and the dependencies.

## Usage

Sign in and find your home folder:

```text
$ ./scripts/login.sh
Signed in as Alice Martin (alice@example.com).
Your home folder is /Home/abcdef123456
```

Copy a local directory to a MOVEit folder, or download a folder:

```text
$ ./scripts/folder-upload.sh ./results /Home/abcdef123456/results
Found 2 files to upload.
Progress: 2 / 2 (2 transferred, 0 skipped, 0 errors)
Uploaded 2 files, skipped 0 files, 0 errors.

$ ./scripts/folder-download.sh /Data/project/results ./results
Found 2 files to download.
Progress: 2 / 2 (2 transferred, 0 skipped, 0 errors)
Downloaded 2 files, skipped 0 files, 0 errors.
```

Folder transfers can be resumed by running the same command again. Uploads skip files already on the server by path, even if the local file changed. Downloads skip identical files.

Send files to people, list received packages and download one:

```text
$ ./scripts/package-send.sh "Results" alice@example.com,bob@example.com ./results ./report.pdf
Progress: 2 / 2 (2 transferred, 0 skipped, 0 errors)
Sent package 379341515.

$ ./scripts/package-list.sh
*  379341516  2026-10-25 10:30  Alice Martin                    Measurements

$ ./scripts/package-receive.sh 379341516 ./received
Package 379341516: Measurements, 1 files.
Progress: 1 / 1 (1 transferred, 0 skipped, 0 errors)
Downloaded 1 files, skipped 0 files, 0 errors.
```

Directories sent in packages are zipped. `*` marks an unread package; use `./scripts/package-list.sh --sent` for sent packages. If a send has an uncertain outcome, check sent packages before trying again: sending twice creates two packages.

For large files, `./scripts/package-send-large.sh "Measurements" alice@example.com ./measurements` splits an archive into 50 MB parts and sends them in several packages. It needs `zip`; recipients should download all parts into one directory and extract the `.zip` file with `7z x measurements.zip`.

Each script accepts `--help`. For more options, such as `--threads`, `--cc` or `--expires-days`, use `uv run moveit --help` or `uv run moveit send --help`.

## Authentication and settings

By default, the tool opens your browser for single sign-on. After signing in, copy the full URL of the page you are redirected to and paste it into the terminal. That page may show an error; the URL is what the tool needs.

The sign-in expires after 20 minutes without use and is kept alive during transfers. If it expires during a command, run `./scripts/login.sh` and retry. Run one command at a time: concurrent commands can interfere with the shared sign-in. `./scripts/logout.sh` removes it from this computer.

For password authentication, for example with a service account, add to `moveit.env`:

```sh
MOVEIT_AUTH=password
MOVEIT_USERNAME=bot@example.com
```

The password is prompted without echo, or read from `MOVEIT_PASSWORD` if set. The token is stored in `~/.config/moveit-py/tokens.json` (or under `$XDG_CONFIG_HOME`) with permissions restricted to your user.

Settings are read from command-line options, environment variables, then `moveit.env`. Set `MOVEIT_ENV_FILE` to use another file. The scripts also accept `--url URL` as their first option.

| Setting | Environment variable | Option | Default |
| --- | --- | --- | --- |
| Server | `MOVEIT_URL` | `--url` | Required |
| Authentication | `MOVEIT_AUTH` | `--auth` | `sso` |
| Username | `MOVEIT_USERNAME` | `--username` | None |
| Password | `MOVEIT_PASSWORD` | — | Prompted |
| Identity provider | — | `--idp` | Automatic if there is only one |
| Token cache | — | `--token-file` | `~/.config/moveit-py/tokens.json` |
| Disable token cache | — | `--no-token-cache` | Off |

## Development

```sh
./scripts/setup.sh
./scripts/local_ci.sh
```

## License

Released under the [MIT License](LICENSE). MOVEit is a trademark of Progress Software Corporation. This project is not affiliated with or endorsed by Progress Software.
