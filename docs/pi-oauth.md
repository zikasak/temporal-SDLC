# Pi OAuth in Coolify

The Pi adapter supports `openai-codex/*` models through Pi's ChatGPT OAuth login.
Rebuild and redeploy `docker-compose.coolify.yml` before signing in.

## Sign in once

Open a terminal in the **api** container as **app** (UID/GID `10001:10001`) and run:

```sh
pi --no-session
```

Inside Pi, enter:

```text
/login openai-codex
```

Complete the login method Pi offers. For browser login on a remote server, open
the displayed URL on your computer and paste the final redirect URL into Pi when
prompted. Exit Pi after it confirms the login.

From the Docker host, replace `YOUR_API_CONTAINER` with the API container's name:

```sh
docker exec -it --user 10001:10001 YOUR_API_CONTAINER pi --no-session
```

Then open **Settings**, click **Refresh provider options**, choose **Pi**, and
select an `openai-codex/...` model and one of its reported thinking levels. Save
the settings. No `OPENAI_API_KEY` is needed for this Pi provider.

## Credential persistence and refresh

The `pi_credentials` named volume is mounted at `/pi/agent` in the API, workers,
and sandbox daemon. `PI_CREDENTIALS_DIR` and Pi's `PI_CODING_AGENT_DIR` both point
there. The initialization service gives UID 10001 ownership before these
services start; run interactive logins as that user too.

`PI_CREDENTIALS_SHARED=true` makes isolated Pi agents mount the same dedicated
directory at `/home/agent/.pi/agent`. The bind source is resolved by the sandbox
daemon, which also has `/pi/agent` mounted. The API, workers, and agents see the
same `auth.json` and sibling lock files, so Pi's refreshes and atomic credential
replacements are visible to all of them. Credentials are not copied into agent
images, workspaces, or environment variables.

The volume survives container replacement and ordinary redeploys. Preserve it
when migrating the application; deleting Compose volumes also deletes the login.
To sign out, run Pi as `app` in the API container and use `/logout` for the provider.

## Local development

Existing local `PI_CREDENTIALS_DIR` login files continue to work. Leave
`PI_CREDENTIALS_SHARED=false` for a personal Pi home that also contains extensions
or sessions. Enable shared mode only for a dedicated credential directory visible
at the same absolute path to both workers and their Docker daemon, as configured
in the Coolify deployment.
