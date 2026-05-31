# Releasing

How to cut a release of the Telnyx IVR: build the image, tag it in lockstep
with a git tag, and push both to Docker Hub. Keep the **Docker image tag** and
the **git tag** on the **same commit** so an image is always traceable to source.

- Docker Hub repo: `docker.io/adrielleu/ivr-triage` (public)
- Versioning: [SemVer](https://semver.org/) — `MAJOR.MINOR.PATCH`
- Convention note: git tags use the `v` prefix (`v1.0.0`); the Docker image
  carries both the `vX.Y.Z` tag and a moving `latest`. `latest` does not
  auto-update — it is re-pointed each release.

## Prerequisites

- Logged in to Docker Hub as `adrielleu` with a **Read & Write** access token:
  ```bash
  docker login --get-login docker.io     # should print: adrielleu
  # if not, or if push is denied (read-only token):
  docker logout docker.io
  docker login docker.io -u adrielleu    # paste a Read & Write token
  ```
- Working tree clean and on the commit you intend to ship (`git status`).

## Release steps

Replace `X.Y.Z` with the new version (e.g. `1.0.0`).

```bash
VERSION=X.Y.Z

# 1. Build (OCI is fine; add --format docker to preserve the Dockerfile HEALTHCHECK)
docker build -t docker.io/adrielleu/ivr-triage:v$VERSION .

# 2. Point latest at the same image
docker tag docker.io/adrielleu/ivr-triage:v$VERSION docker.io/adrielleu/ivr-triage:latest

# 3. Push both tags (first push auto-creates the public repo)
docker push docker.io/adrielleu/ivr-triage:v$VERSION
docker push docker.io/adrielleu/ivr-triage:latest

# 4. Tag the source commit to match, and push the tag
git tag -a v$VERSION -m "v$VERSION"
git push origin v$VERSION
```

### Optional: bake in local Whisper transcription

The image ships without transcription by default. To include `faster-whisper`:

```bash
docker build --build-arg INSTALL_TRANSCRIBE=true \
  -t docker.io/adrielleu/ivr-triage:v$VERSION .
```

### Optional: preserve the HEALTHCHECK

Podman builds OCI images by default, which **drops the Dockerfile `HEALTHCHECK`**.
To keep it in the published image:

```bash
docker build --format docker -t docker.io/adrielleu/ivr-triage:v$VERSION .
```

## What ships in the image (and what doesn't)

`.dockerignore` keeps secrets and real operational data **out** of the image, so
it is safe to publish to a public registry:

- **Excluded:** `.env`, `.git/`, `*.md`, and real `data/*.csv` (caller PII,
  internal numbers, hours).
- **Included:** only `data/*.example.csv` placeholders.

Real operational data (`contacts.csv`, `hours.csv`, `holidays.csv`,
`routing.csv`, `companies.csv`) is supplied at runtime via the compose
bind-mount, never baked in. To verify before a public push:

```bash
docker run --rm --entrypoint sh docker.io/adrielleu/ivr-triage:vX.Y.Z \
  -c "ls -la /app/data && ls -la /app/.env 2>/dev/null || echo 'no .env (good)'"
```

## Verify the release

```bash
docker images docker.io/adrielleu/ivr-triage          # both tags, same IMAGE ID
git rev-parse --short vX.Y.Z; git rev-parse --short HEAD   # should match
git ls-remote --tags origin | grep vX.Y.Z             # tag is on the remote
```
