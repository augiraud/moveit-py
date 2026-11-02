# moveit-py

`moveit-py` is an unofficial command-line client for MOVEit Transfer. It copies folders to and from a MOVEit server, and
it sends and receives packages (files sent by email to other people).

It is not an official MOVEit tool. See [License and trademark](#license-and-trademark).

## What it does

The tool works with two kinds of things on a MOVEit server:

- **Folders** are places on the server, such as your home folder. `folder-upload.sh` copies a local directory into a
  folder, and `folder-download.sh` copies a folder to your computer. Nobody is notified.
- **Packages** are files sent by email. MOVEit notifies the recipients, who download the files from the server.
  `package-send.sh` sends a package, `package-list.sh` lists your packages, and `package-receive.sh` downloads the files
  of a package sent to you.

Transfers run in parallel (4 at a time by default). If one file fails, the others still go on. The command then lists
the failures and exits with code 1. Transfers can be resumed: run the same command again, and the files already
transferred are skipped. The [exit codes](#exit-codes) tell you what happened.

## Install

The tool supports Linux. You need [uv](https://docs.astral.sh/uv/), which installs Python and the other dependencies without administrator rights.

1. **Install uv.** Open a terminal and run:
   ```
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

   Then close the terminal and open a new one.

2. **Get the tool.** With Git, run:
   ```
   git clone https://github.com/augiraud/moveit-py.git
   cd moveit-py
   ```

   Without Git, download the project as a ZIP file from its GitHub page, and extract it.

3. **Set the server address.** Copy the example settings file:
   ```
   cp moveit.env.example moveit.env
   ```

   Open `moveit.env` in a text editor, and replace `https://moveit.example.com` with the address of your MOVEit server:
   ```
   MOVEIT_URL=https://moveit.example.com
   ```

   You can skip this step and give the address to each script with `--url`, for example
   `./scripts/login.sh --url https://moveit.example.com`.

The first time you run a script, uv downloads Python and the dependencies. This takes a moment.

## Usage

The examples assume that you are in the `moveit-py` folder, and call the scripts as `./scripts/NAME.sh`. Local paths such as `./results` are relative to the folder you run the script from. Each script accepts `--help`, which shows its usage:

```
./scripts/folder-upload.sh --help
```

In the usage lines, `[--url URL]` is optional. When you use it, it must come first, before the other arguments.

### Sign in and out

```
./scripts/login.sh
```

This signs you in (see [Single sign-on](#single-sign-on)) and shows your home folder:

```
Signed in as Alice Martin (alice@example.com).
Your home folder is /Home/abcdef123456
```

Use your home folder path in the folder examples below. You can also start with any other script: if you are not
signed in, it starts the same sign-in.

```
./scripts/logout.sh
```

This removes your sign-in from this computer.

### Folders

Copy a local directory to a folder on the server. Files already on the server are skipped:

```
./scripts/folder-upload.sh ./results /Home/abcdef123456/results
```

Copy a folder from the server to your computer. Files already downloaded are skipped:

```
./scripts/folder-download.sh /Data/project/results ./results
```

Add a third argument to download only the files whose path matches a regular expression. This example downloads only
the files ending in `.zip`:

```
./scripts/folder-download.sh /Data/project/results ./results '\.zip$'
```

### Packages

Send files to people by email. Directories are zipped. Separate several addresses with commas, without spaces:

```
./scripts/package-send.sh "Results" alice@example.com,bob@example.com ./results ./report.pdf
```

For files larger than 50 MB, use `package-send-large.sh`. It sends the file in several packages (see
[Large files](#large-files)):

```
./scripts/package-send-large.sh "Measurements" alice@example.com,bob@example.com ./measurements
```

List your packages, newest first. Unopened packages are marked with `*`:

```
./scripts/package-list.sh           # packages sent to you
./scripts/package-list.sh --sent    # packages you sent
```

Download the files of a package sent to you. Get the package ID from `package-list.sh`:

```
./scripts/package-receive.sh 379341516 ./received
```

### One transfer at a time

Run one command at a time. Two commands running at once share the sign-in on this computer, and one of them may fail
with "The sign-in expired" (exit code 3). If that happens, run `./scripts/login.sh`, then run the command again. Files
already transferred are skipped.

## Checksums

- **Uploads** are checked by the server. The tool sends a SHA-256 checksum with each file, and the server rejects a file
  that does not match it.
- **Downloads** are skipped when a file with the same name and size is already on your computer, and its SHA-1 checksum
  matches the one on the server. This detects identical files. It is not a security check. A download whose size does
  not match the server's is discarded and reported as an error.
- **Uploads skip files by path only.** A file already on the server under the same path is skipped, even if your local
  copy has changed. Upload to a new folder if you want to keep both versions.

## Packages in detail

- **If a send fails**, the error says whether anything was sent. If it says "nothing was sent", running the command
  again is safe. If the tool cannot tell whether the package went out, it says so and asks you to check your sent
  packages (`./scripts/package-list.sh --sent`) before you send again.
- **Running a successful send again sends the package again.** The tool does not check for duplicates.
- **Size and count limits**: each file can be up to 1 GB. The server also limits the number of files in a package,
  typically to 10.
- **Recipients** are notified by MOVEit. People without a MOVEit account can get the files only if your server allows
  external recipients. That depends on the server settings.

### Large files

Some servers reject uploads of ZIP archives larger than about 60 MB: the upload seems to finish, then the connection
closes without a reply. For such files, use `package-send-large.sh`. It:

- zips the file or directory into parts of 50 MB, kept in a folder named `NAME.parts` next to it;
- sends the parts as packages of at most 10 files each, with a counter in the subject, such as "Measurements (1/4)";
- keeps the parts until all packages are sent, so that it can resume. If a package fails, the script prints the command
  to run to send the rest.

The script needs the `zip` program. On Debian or Ubuntu, install it with `sudo apt install zip`.

Recipients must extract the parts with [7-Zip](https://www.7-zip.org/), not with `unzip` or Info-ZIP `zip`, which can
produce corrupt files. They download the files of all the packages into one folder, then run:

```
7z x measurements.zip
```

## Direct use of the tool

The scripts cover the common cases. For more options, run the `moveit` command itself, from the `moveit-py` folder:

```
uv run moveit --help
uv run moveit send --help
```

A few useful options:

- `--threads N`: the number of parallel transfers (default 4). `download`, `upload`, `send` and `receive` accept it.
- `-q`, `--quiet`: hide the progress. Errors and the final summary are still shown.
- `--cc EMAILS`, `--bcc EMAILS`, `--note TEXT`: copy recipients, and a message sent with the files.
- `--expires-days N`, `--max-downloads N`: how long the package stays available, and how many times each file can be
  downloaded. Without them, the server's defaults apply.
- `--classification NAME`: the classification of the package, if your server requires one.

For example, to send a package to Alice and Bob, with a copy to Carol, valid for 7 days and downloadable 3 times:

```
uv run moveit send "Contract" ./contract.pdf --to alice@example.com,bob@example.com --cc carol@example.com --expires-days 7 --max-downloads 3
```

## Single sign-on

The tool signs in through your organization's single sign-on, the same way as the MOVEit web pages. It borrows the
identity of the MOVEit Transfer add-in for Microsoft Outlook, which MOVEit accepts for this sign-in. The tool cannot
receive the browser's answer by itself, so you finish the sign-in by hand:

1. Run `./scripts/login.sh`, or any other script when you are not signed in. The tool prints a link and tries to open
   it in your browser.
2. Sign in with your usual company account.
3. The browser then goes to a page of the MOVEit add-in. Its address contains `authorization_code=`. The page may show
   an error. This is expected: the tool only needs the address.
4. Copy the full address from the browser's address bar, and paste it into the terminal at the prompt.

This flow is not documented by MOVEit, and it may stop working after a MOVEit upgrade. If that happens, use the
[password sign-in](#password-sign-in-for-service-accounts), if your administrator allows it.

**Expiry.** A sign-in expires after 20 minutes without use. While a command runs, the tool keeps the sign-in alive, so
long transfers keep working. If the sign-in is lost anyway (for example, the computer was asleep for a long time), the
command stops with exit code 3 and tells you to run `./scripts/login.sh`, then the same command again. Files already
transferred are skipped.

**Where the sign-in is stored.** The sign-in is kept in `~/.config/moveit-py/tokens.json`, or under `$XDG_CONFIG_HOME` if set. The file gives access to your MOVEit account, so keep it private. It is created readable and writable by you only (permissions 0600). To remove the sign-in from this computer, run `./scripts/logout.sh`.

## Password sign-in for service accounts

A service account, such as a shared account used by a script, can sign in with a username and password instead of the
browser. Put these settings in `moveit.env`:

```shell
MOVEIT_URL=https://moveit.example.com
MOVEIT_AUTH=password
MOVEIT_USERNAME=bot@example.com
```

The password is read from the `MOVEIT_PASSWORD` variable if it is set. Otherwise, the tool asks for it and does not show
what you type. The password is never stored. The tool asks for it again when the sign-in has expired (see
[Single sign-on](#single-sign-on)). Prefer the prompt over writing the password in `moveit.env`. If you run the tool
without a terminal, for example from a scheduled task, set `MOVEIT_PASSWORD` instead.

## Settings

| Setting         | Environment variable | Option             | Default                                              |
|-----------------|----------------------|--------------------|------------------------------------------------------|
| Server URL      | `MOVEIT_URL`         | `--url`            | none (required)                                      |
| Sign-in method  | `MOVEIT_AUTH`        | `--auth`           | `sso` (or `password`)                                |
| Username        | `MOVEIT_USERNAME`    | `--username`       | none (required for `password`)                       |
| Password        | `MOVEIT_PASSWORD`    | none               | asked when needed                                    |
| Identity provider | none               | `--idp`            | none (only if the server has several)                |
| Token cache     | none                 | `--token-file`     | `~/.config/moveit-py/tokens.json` |
| No token cache  | none                 | `--no-token-cache` | off (when set, the sign-in is never saved or read from disk) |
| Env file        | `MOVEIT_ENV_FILE`    | none               | `moveit.env` in the current folder (the scripts use the project's `moveit.env`) |

When a setting is given in several places, the first one wins: the command-line option, then the environment variable,
then the `moveit.env` file. An environment variable that is set to an empty value still counts as set, and it hides the
value from `moveit.env`.

The `moveit.env` file holds one `KEY=VALUE` setting per line. Lines starting with `#` are comments. Keep it out of
version control, which the project's `.gitignore` already does.

## Exit codes

| Code  | Meaning |
|-------|---------|
| 0     | Success. |
| 1     | Some files failed, or the command could not run: a server or sign-in error, invalid input, or no server URL. |
| 2     | Invalid command-line arguments. With the scripts, also: no MOVEit server configured, or invalid script arguments. |
| 3     | The sign-in expired during the command. Run `./scripts/login.sh`, then the same command again. |
| 130   | Interrupted with Ctrl+C. |

## Development

The tool is a Python project managed with [uv](https://docs.astral.sh/uv/), which installs Python 3.14 for you.

From the project folder, run this once after cloning. It needs `git` and `uv`. It installs the dependencies and the
[git hook](#git-hook):

```
./scripts/setup.sh
```

Then run the checks, the same ones as in CI:

```
./scripts/local_ci.sh         # all the checks
./scripts/local_ci.sh --fix   # format the code and fix what can be fixed automatically, then check
```

The checks are: formatting (`ruff format`), lint (`ruff check`), types (`ty`), the shell scripts (`shellcheck`, skipped if
it is not installed), and the tests with coverage (`pytest`).

The layout of the project:

```
src/moveit_py/   the tool: cli.py (commands), config.py, api.py (MOVEit API client), auth/ (sign-in),
                 engine.py (parallel transfers), folders.py, files.py, packages/
tests/           unit tests, with fake servers (no network access)
scripts/         the wrapper scripts, and setup.sh and local_ci.sh for developers
```

### Continuous integration

GitHub Actions runs `.github/workflows/ci.yml` on Ubuntu for pushes to `main` and version tags, on pull requests, and on demand. It runs `setup.sh` and `local_ci.sh`, then SonarCloud (see below).

### SonarCloud (optional)

The `ci` job runs a SonarCloud analysis when the repository has a `SONAR_TOKEN` secret. Without it, the job says so and
skips the analysis. To turn it on:

1. On [SonarQube Cloud](https://sonarcloud.io/projects/create), import the repository.
2. In the SonarQube Cloud project, go to Administration > Analysis Method, and turn off Automatic Analysis.
3. Create a token in My Account > Security.
4. In the GitHub repository, add the token as a secret named `SONAR_TOKEN`: Settings > Secrets and variables > Actions >
   New repository secret.

The project key and organization are set in `sonar-project.properties`. Edit it if yours are different.

### Git hook

`setup.sh` makes git use the hooks in `.githooks/`. Before each commit, the `pre-commit` hook runs `ruff format --check`
and `ruff check`.

## License and trademark

moveit-py is released under the MIT License. See [LICENSE](LICENSE).

MOVEit is a trademark of Progress Software Corporation. This project is unofficial: it is not affiliated with, nor endorsed by, Progress Software.
