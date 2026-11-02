# python-template

A ready-to-use Python project template. Run the setup script and start coding, with CI, linting, and quality checks already wired up.

## Getting started

1. Create a repository from this template (`Use this template` on GitHub), then clone it.
2. Install git, [uv](https://docs.astral.sh/uv/getting-started/installation/) and [Docker](https://docs.docker.com/get-docker/), preferably with your package manager. uv installs Python for you.
3. Run `./scripts/setup.sh`. The first time, it asks for the project name and your GitHub user or organization, then renames the project.
4. Write your code in `src/python_template/` and your tests in `tests/`.

## Commands

- `./scripts/run.sh [args]`: run the app
- `./scripts/run_docker.sh [args]`: run the app in Docker, building the image first
- `./scripts/docker_build.sh`: build the Docker image
- `./scripts/local_ci.sh`: run all the checks exactly like CI does (format, lint, types, tests, Docker image). With `--fix`, it first formats the code and fixes what can be fixed automatically.
- `uv add <package>`: add a dependency (`uv add --dev <package>` for a development tool)

A git hook checks formatting and lint before each commit.

## CI

GitHub Actions runs `./scripts/local_ci.sh` on every push to `main`, every `v*` tag and every pull request.

### Sonar

CI skips Sonar until you set it up:

1. On [SonarQube Cloud](https://sonarcloud.io/projects/create), import the repository.
2. In the SonarQube Cloud project, go to Administration > Analysis Method and turn off Automatic Analysis.
3. Create a token in My Account > Security.
4. In the GitHub repository, add the token as a secret named `SONAR_TOKEN`: Settings > Secrets and variables > Actions > New repository secret.

The setup script uses SonarQube Cloud's default names: your GitHub user or organization in lowercase for the organization, and `<owner>_<project>` for the project key. If yours differ, edit `sonar-project.properties`.

More details in [SonarQube Cloud's documentation](https://docs.sonarsource.com/sonarqube-cloud/analyzing-source-code/ci-based-analysis/github-actions-for-sonarcloud).

### Publishing the Docker image

Disabled by default. To enable it, uncomment the last block in `.github/workflows/ci.yml`.

Then every push to `main` publishes `ghcr.io/<owner>/<repository>:latest`, and every tag such as `v1.2.0` publishes `ghcr.io/<owner>/<repository>:v1.2.0`. Both also publish an image tagged with the commit hash.

## Configuration

- `pyproject.toml`: dependencies, lint and type-checking rules
- `.python-version`: Python version (also set in `sonar-project.properties`)
- `.github/workflows/ci.yml`: CI
- `.github/dependabot.yml`: weekly dependency updates
- `Dockerfile`: Docker image
- `.githooks/pre-commit`: checks before each commit
