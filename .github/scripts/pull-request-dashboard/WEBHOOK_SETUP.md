# Pull Request Dashboard Webhook Setup

## 1. Netlify project

Create a Netlify project for this repository:

- Repository: `open-telemetry/semantic-conventions-genai`
- Base directory: `.github/scripts/pull-request-dashboard`

Save the Netlify project ID as a GitHub Actions variable named
`NETLIFY_PR_DASHBOARD_PROJECT_ID`.

Save a Netlify personal access token as a GitHub Actions secret named
`NETLIFY_AUTH_TOKEN`.

## 2. GitHub App

Create a GitHub App with the following webhook URL:

`https://<netlify-site-name>.netlify.app/.netlify/functions/github-webhook`

Generate and save a webhook secret:

```bash
openssl rand -hex 32
```

Set repository permissions:

- Pull requests: read-only
- Issues: read-only
- Actions: read and write

Subscribe to events:

- Pull request
- Issue comment
- Pull request review
- Pull request review comment
- Pull request review thread

Generate and download a private key for the GitHub App.

## 3. Install the app

Install the GitHub App on each OpenTelemetry repository that should use the
pull request dashboard webhook.

Copy the installation ID from the installation URL
(`https://github.com/settings/installations/<installation-id>`).

## 4. Netlify environment variables

Encode the private key as a single-line base64 string (Git Bash):

```bash
base64 < /path/to/github-app-private-key.pem | tr -d '\n' | clip
```

Add the following environment variables to the Netlify project.

Secrets:

- `GITHUB_APP_PRIVATE_KEY_BASE64` — base64-encoded GitHub App private key PEM
- `GITHUB_WEBHOOK_SECRET` — same webhook secret as the GitHub App

Non-secrets:

- `GITHUB_APP_ID` — GitHub App ID
- `GITHUB_APP_INSTALLATION_ID` — GitHub App installation ID
- `GITHUB_OWNER` — `open-telemetry`
- `GITHUB_WORKFLOW_ID` — `pull-request-dashboard.yml`
- `GITHUB_WORKFLOW_REF` — `main`

The GitHub App installation is the repository opt-in signal. The webhook only
dispatches workflows for repositories owned by `GITHUB_OWNER`.

## 5. Deploy

Once the Netlify project ID variable and auth token secret are configured,
run the `.github/workflows/deploy-pull-request-dashboard-webhook.yml` workflow.

## 6. Smoke test

In the GitHub App settings, navigate to **Advanced -> Recent deliveries**,
select a ping delivery, and click **Redeliver**. Expect a 202 response.

Then trigger a pull request event and confirm that the pull request dashboard workflow starts
via `workflow_dispatch`.
